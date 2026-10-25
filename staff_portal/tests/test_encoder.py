"""encoder.py against encoder_sim (SN Web Socket and a card encoder on 127.0.0.1): the card job
state machine (cards in order, blank cards set up, a card never written twice in one job, needs
attention -> try again / skip / stop, a job nobody answers), the encoder let go after every job,
look-ups, Test encoder, and the WebSocket protocol itself."""

import asyncio
import json
import time

import pytest
from aiohttp import WSMsgType, web

import encoder
from encoder import (BUSY, NO_REPLY, UNREACHABLE, EncoderError, Encoders, SNWebSocket,
                     card_cancellations, card_records)
from encoder_sim import EncoderSim

MAC = "C540E09C8CC1"
POOL = {"level": "door", "building": 1, "floor": 2, "mac": MAC, "door": 7, "label": "Pool Gate"}
FLOOR = {"level": "floor", "building": 1, "floor": 2, "mac": "000000000000", "door": None,
         "label": "Main, Floor 2"}
EXPIRES = int(time.time()) + 8 * 3600
NOT_ONLINE = "TT Hotel can't be reached"


class Offline(Exception):
    needs_admin = False


class Harness:
    """A simulator, a registry with fake hotelInfo, and the database's view of each job, kept
    from the runner's on_update calls."""

    def __init__(self, *, auto=False, attention=10.0, url=None):
        self.sim = EncoderSim(auto=auto, place_after=0.05, remove_after=0.05)
        self.url = url
        self.attention = attention
        self.online = True
        self.hotel_calls: list[bool] = []
        self.expired = 0
        self.jobs: dict[int, dict] = {}
        self.next_id = 1

    async def hotel_info(self, *, fresh: bool) -> str:
        self.hotel_calls.append(fresh)
        if self.online:
            return "fresh-key"
        if not fresh:
            return "old-key"
        raise Offline(NOT_ONLINE)

    def hotel_info_expired(self):
        self.expired += 1

    def on_update(self, job_id, job_fields, card_fields):
        job = self.jobs[job_id]
        job.update(job_fields)
        if card_fields:
            fields = dict(card_fields)
            job["cards"][fields.pop("seq") - 1].update(fields)

    async def __aenter__(self):
        if self.url is None:
            self.url = await self.sim.start()
        self.encoders = Encoders(self.hotel_info, self.on_update, poll=0.01,
                                 attention_timeout=self.attention,
                                 hotel_info_expired=self.hotel_info_expired)
        self.runner = self.encoders.get(self.url)
        return self

    async def __aexit__(self, *exc):
        await self.encoders.close_all()
        await self.sim.stop()

    def submit(self, count=1, records=(POOL,), card_type="day_pass", card_wait=5.0, port=None):
        job_id = self.next_id
        self.next_id += 1
        row = {"id": job_id, "card_type": card_type, "name": "Smith", "count": count,
               "records": json.dumps(list(records)), "expires_at": EXPIRES,
               "allow_lockout": 0, "status": "queued"}
        self.jobs[job_id] = {"status": "queued", "message": None, "finished_at": None,
                             "cards": [{"seq": s, "status": "pending", "card_number": None,
                                        "error": None} for s in range(1, count + 1)]}
        self.runner.submit(row, port=port, sectors="0000000000011111", card_wait=card_wait)
        return job_id

    def submit_kind(self, kind, card_wait=5.0, **fields):
        """A revoke or notice job (or a write job with extra fields), for one staff card."""
        job_id = self.next_id
        self.next_id += 1
        row = {"id": job_id, "card_type": "staff", "name": "Smith", "count": 1, "kind": kind,
               "status": "queued", **fields}
        self.jobs[job_id] = {"status": "queued", "message": None, "finished_at": None,
                             "cards": [{"seq": 1, "status": "pending", "card_number": None,
                                        "error": None}]}
        self.runner.submit(row, sectors="0000000000011111", card_wait=card_wait)
        return job_id

    def statuses(self, job_id):
        return [c["status"] for c in self.jobs[job_id]["cards"]]

    async def until(self, check, what, timeout=3.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            result = check()
            if result:
                return result
            await asyncio.sleep(0.003)
        raise AssertionError(f"timed out waiting for {what}: {self.runner.live(1)}")

    async def live(self, job_id, phase, seq=None, message=None, timeout=3.0):
        def check():
            live = self.runner.live(job_id)
            return (live and live["phase"] == phase and (seq is None or live["current"] == seq)
                    and (message is None or live["message"] == message) and live)
        return await self.until(check, f"job {job_id} {phase} {seq} {message or ''}", timeout)

    async def status(self, job_id, status, timeout=3.0):
        await self.until(lambda: self.jobs[job_id]["status"] == status,
                         f"job {job_id} to be {status}", timeout)
        return self.jobs[job_id]

    async def released(self):
        """The encoder was let go: USB port closed and no WebSocket left open."""
        await self.until(lambda: not self.runner.status()["busy"] and self.sim.sockets == 0
                         and not self.sim.connected, "the encoder to be let go")


def run(scenario, **kwargs):
    async def main():
        async with Harness(**kwargs) as h:
            await scenario(h)
    asyncio.run(main())


# ---------------------------------------------------------------- making cards

def test_happy_path_four_wristbands():
    async def scenario(h):
        job_id = h.submit(count=4)
        numbers = []
        for seq in range(1, 5):
            await h.live(job_id, "waiting_for_card", seq,
                         f"Place wristband {seq} of 4 on the encoder")
            numbers.append(h.sim.place_card(initialized=True))
            if seq < 4:
                await h.live(job_id, "remove_card", seq, f"Take wristband {seq} off the encoder")
                assert h.statuses(job_id)[seq - 1] == "written"   # saved before it's lifted
                h.sim.remove_card()
        job = await h.status(job_id, "done")
        assert job["message"] == "All 4 wristbands are ready" and job["finished_at"]
        assert [c["card_number"] for c in job["cards"]] == numbers and len(set(numbers)) == 4
        assert all(c["written_at"] for c in job["cards"])
        for number in numbers:
            (record,) = h.sim.cards[number]["records"]
            assert (record["buildNo"], record["floorNo"], record["mac"], record["endDate"],
                    record["allowLockOut"]) == (1, 2, MAC, EXPIRES, 0)
        assert h.sim.beeps == 4 and h.sim.sectors == "0000000000011111"
        assert h.sim.hotel_info == "fresh-key"                        # the encoder was set up
        assert h.hotel_calls == [True, False, False, False, False]   # set-up, then door records
        live = h.runner.live(job_id)
        assert (live["status"], live["phase"], live["current"]) == ("done", "done", None)
        await h.released()
        assert h.sim.calls[-1] == "CE_DisconnectComm" and h.sim.connections == 1
        assert h.runner.status()["connected"] is True
    run(scenario)


def test_auto_simulator_runs_a_job_unattended():
    async def scenario(h):
        job_id = h.submit(count=3)
        job = await h.status(job_id, "done", timeout=5)
        numbers = [c["card_number"] for c in job["cards"]]
        assert len(set(numbers)) == 3
        assert all(h.sim.cards[n]["hotel"] == "this hotel" for n in numbers)
    run(scenario, auto=True)


def test_blank_card_is_set_up_with_a_fresh_key():
    async def scenario(h):
        job_id = h.submit()
        await h.live(job_id, "waiting_for_card", 1, "Place the wristband on the encoder")
        number = h.sim.place_card()   # blank
        await h.status(job_id, "done")
        assert h.sim.cards[number]["hotel"] == "this hotel"
        assert len(h.sim.cards[number]["records"]) == 1
        assert "CE_InitCard" in h.sim.calls
        assert h.hotel_calls == [True, False, True]   # set-up, door record, blank card
    run(scenario)


def test_written_card_is_never_reused_in_the_same_job():
    async def scenario(h):
        job_id = h.submit(count=2)
        await h.live(job_id, "waiting_for_card", 1)
        first = h.sim.place_card(initialized=True)
        await h.live(job_id, "remove_card", 1)
        h.sim.remove_card()
        await h.live(job_id, "waiting_for_card", 2, "Place wristband 2 of 2 on the encoder")
        h.sim.place_card(first)   # the first wristband goes back on the encoder
        await h.live(job_id, "waiting_for_card", 2, "Take wristband 1 off the encoder")
        await asyncio.sleep(0.05)   # many polls
        assert h.sim.beeps == 1 and h.statuses(job_id) == ["written", "pending"]
        second = h.sim.place_card(initialized=True)
        job = await h.status(job_id, "done")
        assert [c["card_number"] for c in job["cards"]] == [first, second]
    run(scenario)


def test_card_left_from_the_last_job_is_not_overwritten():
    async def scenario(h):
        first = h.submit()
        await h.live(first, "waiting_for_card")
        left = h.sim.place_card(initialized=True)
        await h.status(first, "done")
        await h.released()
        second = h.submit()
        await h.live(second, "waiting_for_card", 1, "Take the wristband you just made off the "
                                                    "encoder")
        await asyncio.sleep(0.05)
        assert h.sim.beeps == 1
        h.sim.remove_card()
        await h.live(second, "waiting_for_card", 1, "Place the wristband on the encoder")
        h.sim.place_card(left)   # lifted and put back on purpose: fine to rewrite
        job = await h.status(second, "done")
        assert job["cards"][0]["card_number"] == left
    run(scenario)


def test_card_left_before_a_restart_is_not_overwritten():
    async def scenario(h):
        left = h.sim.place_card(initialized=True)
        # A new registry, as after a restart, told which card was made last on this encoder.
        await h.encoders.close_all()
        h.encoders = Encoders(h.hotel_info, h.on_update, poll=0.01,
                              last_card=lambda url: left if url == h.url else None)
        h.runner = h.encoders.get(h.url)
        job = h.submit()
        await h.live(job, "waiting_for_card", 1, "Take the wristband you just made off the "
                                                 "encoder")
        h.sim.remove_card()
        await h.live(job, "waiting_for_card", 1, "Place the wristband on the encoder")
        h.sim.place_card(left)
        assert (await h.status(job, "done"))["cards"][0]["card_number"] == left
        # A failing look-up just means nothing lingering.
        def broken(url):
            raise RuntimeError("database is locked")
        other = Encoders(h.hotel_info, h.on_update, last_card=broken)
        assert other.get(h.url)._lingering is None
        await other.close_all()
    run(scenario)


def test_staff_cards_say_card_and_need_a_fresh_key():
    async def scenario(h):
        job_id = h.submit(count=2, records=(FLOOR,), card_type="staff")
        await h.live(job_id, "waiting_for_card", 1, "Place card 1 of 2 on the encoder")
        h.sim.place_card(initialized=True)
        await h.live(job_id, "remove_card", 1, "Take card 1 off the encoder")
        h.sim.remove_card()
        await h.live(job_id, "waiting_for_card", 2, "Place card 2 of 2 on the encoder")
        number = h.sim.place_card(initialized=True)
        job = await h.status(job_id, "done")
        assert job["message"] == "All 2 cards are ready"
        assert h.hotel_calls == [True, True, True]
        (record,) = h.sim.cards[number]["records"]
        assert (record["buildNo"], record["floorNo"], record["mac"]) == (1, 2, "000000000000")
    run(scenario)


# ---------------------------------------------------------------- when something goes wrong

def test_timeout_then_try_again_skip_and_stop():
    async def scenario(h):
        job_id = h.submit(count=4, card_wait=0.2)
        await h.live(job_id, "needs_attention", 1, "No wristband detected")
        job = await h.status(job_id, "needs_attention")
        assert job["message"] == "No wristband detected"
        assert (job["cards"][0]["status"], job["cards"][0]["error"]) == (
            "failed", "No wristband detected")
        first = h.sim.place_card(initialized=True)
        assert h.runner.retry(job_id)
        await h.live(job_id, "remove_card", 1)
        assert h.jobs[job_id]["status"] == "running"
        assert (job["cards"][0]["status"], job["cards"][0]["card_number"],
                job["cards"][0]["error"]) == ("written", first, None)
        h.sim.remove_card()
        await h.live(job_id, "needs_attention", 2)
        assert h.runner.skip(job_id)
        await h.live(job_id, "needs_attention", 3)
        assert h.statuses(job_id) == ["written", "skipped", "failed", "pending"]
        assert h.runner.cancel(job_id)
        job = await h.status(job_id, "cancelled")
        assert h.statuses(job_id) == ["written", "skipped", "cancelled", "cancelled"]
        assert job["cards"][2]["error"] == "No wristband detected"   # kept for the record
        assert job["message"] == "Cancelled after making 1 of 4 wristbands"
        assert h.runner.live(job_id)["phase"] == "cancelled"
        await h.released()
    run(scenario)


def test_try_again_and_skip_only_while_a_job_needs_attention():
    async def scenario(h):
        job_id = h.submit(count=2)
        await h.live(job_id, "waiting_for_card", 1)
        assert not h.runner.retry(job_id) and not h.runner.skip(job_id)
        assert not h.runner.retry(12345) and not h.runner.cancel(12345)
        await asyncio.sleep(0.03)
        assert h.statuses(job_id) == ["pending", "pending"]
        assert h.jobs[job_id]["status"] == "running"
    run(scenario)


def test_stop_takes_effect_within_a_poll():
    async def scenario(h):
        job_id = h.submit(count=2)
        await h.live(job_id, "waiting_for_card", 1)
        started = time.monotonic()
        h.runner.cancel(job_id)
        job = await h.status(job_id, "cancelled")
        assert time.monotonic() - started < 0.5
        assert h.statuses(job_id) == ["cancelled", "cancelled"]
        assert job["message"] == "Cancelled before any wristbands were made"
        # While waiting for a card to be lifted off, too.
        job_id = h.submit(count=3)
        await h.live(job_id, "waiting_for_card", 1)
        h.sim.place_card(initialized=True)
        await h.live(job_id, "remove_card", 1)
        h.runner.cancel(job_id)
        await h.status(job_id, "cancelled")
        assert h.statuses(job_id) == ["written", "cancelled", "cancelled"]
        await h.released()
    run(scenario)


def test_stop_during_the_last_write_still_finishes_the_job():
    async def scenario(h):
        job_id = h.submit()
        write = h.sim._fn_CE_WriteCard

        def write_then_stop(request):
            result = write(request)
            h.runner.cancel(job_id)   # Stop tapped while the encoder is writing
            return result
        h.sim._fn_CE_WriteCard = write_then_stop
        h.sim.place_card(initialized=True)
        await h.until(lambda: h.jobs[job_id]["status"] in ("done", "cancelled"), "the end")
        assert h.jobs[job_id]["status"] == "done" and h.statuses(job_id) == ["written"]
        assert h.runner.live(job_id)["message"] == "The wristband is ready"
    run(scenario)


def test_queued_jobs_run_in_order_and_stop_at_once():
    async def scenario(h):
        first = h.submit()
        await h.live(first, "waiting_for_card")
        second, third = h.submit(), h.submit()
        assert h.runner.live(second)["message"] == "Waiting for another job to finish…"
        assert h.runner.status()["busy"]
        assert h.runner.cancel(second)   # not started: stopped straight away
        assert h.jobs[second]["status"] == "cancelled" and h.statuses(second) == ["cancelled"]
        h.sim.place_card(initialized=True)
        await h.status(first, "done")
        await h.live(third, "waiting_for_card", 1, "Take the wristband you just made off the "
                                                   "encoder")
        h.sim.remove_card()
        h.sim.place_card(initialized=True)
        await h.status(third, "done")
        assert h.jobs[second]["status"] == "cancelled"
        await h.released()
        assert h.sim.connections == 2   # a fresh connection per job
    run(scenario)


def test_job_nobody_answers_is_stopped_and_the_next_one_runs():
    async def scenario(h):
        first = h.submit(count=2, card_wait=0.1)
        await h.live(first, "needs_attention", 1, "No wristband detected")
        second = h.submit()
        assert h.runner.live(second)["phase"] == "queued"
        await h.status(first, "cancelled", timeout=2)
        assert h.statuses(first) == ["cancelled", "cancelled"]
        await h.live(second, "waiting_for_card", 1)
        h.sim.place_card(initialized=True)
        await h.status(second, "done")
    run(scenario, attention=0.3)


def test_encoder_computer_unreachable():
    async def scenario(h):
        job_id = h.submit()
        live = await h.live(job_id, "needs_attention", 1)
        assert live["message"].startswith("Can't reach the card encoder's computer")
        assert h.jobs[job_id]["cards"][0]["status"] == "failed"
        status = h.runner.status()
        assert status["connected"] is False and status["error"] == live["message"]
        h.runner.cancel(job_id)
        await h.status(job_id, "cancelled")
    run(scenario, url="ws://127.0.0.1:9")   # the discard port: nothing answers


def test_unplugged_encoder_needs_attention_then_recovers():
    async def scenario(h):
        h.sim.unplug()
        job_id = h.submit()
        live = await h.live(job_id, "needs_attention", 1)
        assert live["message"] == ("The card encoder wasn't found. Check its USB cable, and that "
                                   "its driver (Silicon Labs CP210x) is installed on that computer, "
                                   "then try again. Encoder error 1003.")
        assert h.runner.status()["connected"] is False
        h.sim.plug_in()
        h.sim.place_card(initialized=True)
        h.runner.retry(job_id)
        await h.status(job_id, "done")
        assert h.runner.status() == {"connected": True, "busy": False, "error": None,
                                     "checked_at": h.runner.checked_at}
    run(scenario)


def test_dead_usb_port_reported_by_the_card_reader_reconnects_on_retry():
    async def scenario(h):
        job_id = h.submit()
        await h.live(job_id, "waiting_for_card", 1)
        h.sim.unplug(card_id_code=4)
        live = await h.live(job_id, "needs_attention", 1)
        assert live["message"] == ("The card encoder stopped responding. Check the USB cable, "
                                   "then try again. Encoder error 4.")
        connects = h.sim.calls.count("GetDeviceInfo")
        h.sim.plug_in()
        h.sim.place_card(initialized=True)
        h.runner.retry(job_id)
        await h.status(job_id, "done")
        assert h.sim.calls.count("GetDeviceInfo") == connects + 1
    run(scenario)


def test_dead_usb_port_that_looks_like_no_card_reconnects_on_retry():
    async def scenario(h):
        job_id = h.submit(card_wait=0.2)
        await h.live(job_id, "waiting_for_card", 1)
        h.sim.unplug()   # still "connected", and every poll looks like an empty encoder
        h.sim.place_card(initialized=True)
        await h.live(job_id, "needs_attention", 1, "No wristband detected")
        connects = h.sim.calls.count("GetDeviceInfo")
        h.sim.plug_in()
        h.runner.retry(job_id)
        await h.status(job_id, "done")
        assert h.sim.calls.count("GetDeviceInfo") == connects + 1
    run(scenario)


def test_card_left_on_the_encoder_ends_in_needs_attention():
    async def scenario(h):
        job_id = h.submit(count=2, card_wait=0.2)
        h.sim.place_card(initialized=True)
        await h.live(job_id, "remove_card", 1)
        # Never lifted off: the job doesn't keep running.
        await h.live(job_id, "needs_attention", 2, "Take wristband 1 off the encoder, then try "
                                                   "again.")
        # While it waits for staff, the encoder is free for TT Hotel's own program.
        await h.until(lambda: not h.sim.connected and h.sim.sockets == 0, "the encoder let go")
        assert h.runner.status()["busy"]
        h.sim.place_card(initialized=True)
        h.runner.retry(job_id)
        await h.status(job_id, "done")
    run(scenario)


def test_expired_key_is_dropped_and_try_again_works():
    async def scenario(h):
        h.sim.fail_next("CE_ClearCard", 13)
        job_id = h.submit()
        h.sim.place_card(initialized=True)
        await h.live(job_id, "needs_attention", 1, "The authorization from TT Hotel expired. "
                                                   "Try again. Encoder error 13.")
        assert h.expired == 1
        h.runner.retry(job_id)
        await h.status(job_id, "done")
    run(scenario)


def test_offline_single_doors_use_the_old_key_but_floors_need_a_fresh_one():
    async def scenario(h):
        h.online = False
        room = h.submit()
        h.sim.place_card(initialized=True)
        await h.status(room, "done")
        assert h.hotel_calls == [True, False]   # set-up failed (a warning), the old key wrote it
        assert h.runner.status()["error"] == encoder.CONFIGURE_OFFLINE
        await h.released()
        h.sim.remove_card()
        floor = h.submit(records=(FLOOR,), card_type="staff")
        h.sim.place_card(initialized=True)
        message = ("Cards with floor, building or all-door access need the internet. Check the "
                   "connection, then try again.")
        await h.live(floor, "needs_attention", 1, message)
        h.online = True
        h.runner.retry(floor)
        await h.status(floor, "done")
    run(scenario)


def test_offline_blank_card_cannot_be_set_up():
    async def scenario(h):
        h.online = False
        job_id = h.submit()
        h.sim.place_card()   # blank
        live = await h.live(job_id, "needs_attention", 1)
        assert live["message"].startswith("This wristband is blank, and setting it up needs the "
                                          "internet.")
        h.sim.place_card(initialized=True)   # swap in one used here before
        h.runner.retry(job_id)
        await h.status(job_id, "done")
    run(scenario)


def test_tt_hotel_needing_a_manager_says_so():
    async def scenario(h):
        class Stopped(Exception):
            needs_admin = True

        async def stopped(*, fresh):
            raise Stopped("TT Hotel didn't accept the Client ID and Client Secret.")
        h.runner._hotel_info = stopped
        job_id = h.submit()
        h.sim.place_card(initialized=True)
        live = await h.live(job_id, "needs_attention", 1)
        assert "manager" in live["message"] and "Client" not in live["message"]
        assert h.runner.status()["error"] == encoder.TTHOTEL_NOT_SET_UP
    run(scenario)


def test_skipped_card_is_not_used_for_the_next_one():
    async def scenario(h):
        job_id = h.submit(count=2)
        await h.live(job_id, "waiting_for_card", 1)
        foreign = h.sim.place_card(foreign=True)
        live = await h.live(job_id, "needs_attention", 1)
        assert live["message"] == ("This wristband belongs to another hotel or can't be set up. "
                                   "Try a different wristband. Encoder error 106.")
        h.runner.skip(job_id)
        await h.live(job_id, "waiting_for_card", 2, "Take that wristband off the encoder and use "
                                                    "a new one")
        await asyncio.sleep(0.03)
        assert h.sim.present == foreign and h.statuses(job_id) == ["skipped", "pending"]
        good = h.sim.place_card(initialized=True)
        job = await h.status(job_id, "done")
        assert [c["card_number"] for c in job["cards"]] == [None, good]
        assert job["message"] == "1 of 2 wristbands is ready (1 skipped)"
    run(scenario)


def test_an_unexpected_error_does_not_stop_the_runner(monkeypatch):
    async def scenario(h):
        original = SNWebSocket.clear_card
        calls = []

        async def flaky(self, hotel_info):
            calls.append(hotel_info)
            if len(calls) == 1:
                raise RuntimeError("bug")
            return await original(self, hotel_info)
        monkeypatch.setattr(SNWebSocket, "clear_card", flaky)
        job_id = h.submit()
        h.sim.place_card(initialized=True)
        await h.live(job_id, "needs_attention", 1, "Something went wrong while making this "
                                                   "wristband. Try again.")
        h.runner.retry(job_id)
        await h.status(job_id, "done")
    run(scenario)


def test_explicit_port_and_jobs_that_cannot_be_written():
    async def scenario(h):
        job_id = h.submit(port="COM3")
        h.sim.place_card(initialized=True)
        await h.status(job_id, "done")
        assert "CE_ConnectComm" in h.sim.calls and "GetDeviceInfo" not in h.sim.calls
        for bad in ({"records": "[]"}, {"count": 0}, {"card_type": "vip"}, {"expires_at": 0},
                    {"records": json.dumps([{**POOL, "mac": "nope"}])},
                    {"records": json.dumps([{**POOL, "building": 255}])}):
            row = {"id": 99, "card_type": "day_pass", "count": 1, "records": json.dumps([POOL]),
                   "expires_at": EXPIRES, "allow_lockout": 0, **bad}
            with pytest.raises(ValueError):
                h.runner.submit(row)
    run(scenario)


# ---------------------------------------------------------------- look-up and test

def test_look_up_a_wristband():
    async def scenario(h):
        assert h.runner.lookup_state() is None
        job_id = h.submit()
        number = h.sim.place_card(initialized=True)
        await h.status(job_id, "done")
        await h.released()
        h.runner.start_lookup(card_wait=1)
        await h.until(lambda: h.runner.lookup_state()["state"] == "done", "the look-up")
        found = h.runner.lookup_state()
        assert found["card_number"] == number and found["message"] is None
        assert found["card"] == [{"level": "door", "building": 1, "floor": 2, "mac": MAC,
                                  "starts_at": found["card"][0]["starts_at"],
                                  "expires_at": EXPIRES, "allow_lockout": False}]
        await h.released()
        # A blank card has a number but nothing on it.
        h.sim.place_card()
        h.runner.start_lookup(card_wait=1)
        await h.until(lambda: h.runner.lookup_state()["state"] == "done", "the look-up")
        assert h.runner.lookup_state()["card"] == []
        assert "blank" in h.runner.lookup_state()["message"]
        # Nothing on the encoder.
        h.sim.remove_card()
        h.runner.start_lookup(card_wait=0.1)
        await h.until(lambda: h.runner.lookup_state()["state"] == "error", "the look-up")
        assert h.runner.lookup_state()["message"].startswith("No wristband detected")
        await h.released()
        # A card just looked up can be written straight away, without lifting it off first.
        h.sim.place_card(number)
        h.runner.start_lookup(card_wait=1)
        await h.until(lambda: h.runner.lookup_state()["state"] == "done", "the look-up")
        job_id = h.submit()
        await h.until(lambda: h.runner.live(job_id)["cards"][0]["status"] == "written",
                      "the looked-up card written")
    run(scenario)


def test_look_up_can_be_stopped_or_replaced():
    async def scenario(h):
        h.runner.start_lookup(card_wait=5)
        await h.until(lambda: "Place" in h.runner.lookup_state()["message"], "waiting")
        h.runner.start_lookup(card_wait=5)   # tapped again: the old one ends
        h.runner.cancel_lookup()
        await h.until(lambda: h.runner.lookup_state()["state"] == "error", "stopped")
        await h.released()
    run(scenario)


def test_test_encoder_reads_versions_and_lets_go():
    async def scenario(h):
        version = await h.runner.test()
        assert "SN Web Socket 2.5.1" in version and "simulator" in version
        await h.released()
        job_id = h.submit()
        await h.live(job_id, "waiting_for_card")
        with pytest.raises(EncoderError) as info:
            await h.runner.test()
        assert info.value.code == BUSY and "busy" in info.value.message
        h.runner.cancel(job_id)
    run(scenario)


def test_test_encoder_unreachable():
    async def scenario(h):
        with pytest.raises(EncoderError) as info:
            await h.runner.test("COM3")
        assert info.value.code == UNREACHABLE
        assert info.value.message.startswith("Can't reach SN Web Socket")
    run(scenario, url="ws://127.0.0.1:9")


def test_status_checks_that_the_computer_answers():
    async def scenario(h):
        first = h.runner.status()
        assert first["connected"] is False and first["busy"] is False
        await h.until(lambda: h.runner.status()["connected"], "the check")
        assert list(h.sim.calls) == []   # only the WebSocket: the encoder wasn't touched
        assert h.encoders.status(None) == {"connected": False, "busy": False, "error": None,
                                           "checked_at": None}
        assert h.encoders.find("ws://elsewhere:8102") is None
        assert h.encoders.get(h.url) is h.runner
        unreachable = h.encoders.get("ws://127.0.0.1:9")
        unreachable.status()
        await h.until(lambda: unreachable.status()["error"], "the failed check")
        assert unreachable.status()["connected"] is False
        assert unreachable.status()["error"].startswith("Can't reach the card encoder's computer")
    run(scenario)


# ---------------------------------------------------------------- the protocol itself

async def serve(handler):
    app = web.Application()

    async def socket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"errorCode": 0, "description": "you are connection"})
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await handler(ws, json.loads(msg.data))
        return ws
    app.router.add_get("/", socket)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner, f"ws://127.0.0.1:{runner.addresses[0][1]}"


def test_answers_are_matched_by_request_id_and_strays_ignored():
    async def handler(ws, request):
        await ws.send_str("not json")
        await ws.send_json([1, 2])
        await ws.send_json({"requestId": "someone-else", "errorCode": 0, "function": "CE_Beep"})
        await ws.send_json({"function": request["function"], "errorCode": 0})   # no requestId
        await ws.send_json({**request, "errorCode": 0, "data": {"ok": True}})

    async def scenario():
        runner, url = await serve(handler)
        import aiohttp
        async with aiohttp.ClientSession() as session:
            socket = SNWebSocket(url, session, timeout=2)
            reply = await socket.call("CE_Beep", voiceLen=120)
            assert reply["data"] == {"ok": True} and reply["voiceLen"] == 120
            await socket.close()
        await runner.cleanup()
    asyncio.run(scenario())


def test_helper_errors_without_request_id_and_silence():
    async def scenario():
        sim = EncoderSim()
        url = await sim.start()
        import aiohttp
        async with aiohttp.ClientSession() as session:
            socket = SNWebSocket(url, session, timeout=2)
            started = time.monotonic()
            with pytest.raises(EncoderError) as info:
                await socket.call("CE_SomethingNew")   # SN Web Socket: -3, without the requestId
            assert info.value.code == -3 and time.monotonic() - started < 1
            sim.fail_next("CE_Beep", 5)
            with pytest.raises(EncoderError) as info:
                await socket.beep()
            assert (info.value.code, info.value.operation) == (5, "CE_Beep")
            # GetDeviceInfo keeps its errorCode inside data.
            sim.unplug()
            with pytest.raises(EncoderError) as info:
                await socket.connect_encoder()
            assert info.value.code == 1003
            sim.plug_in()
            assert (await socket.connect_encoder())["dll"] == "simulator"
            assert await socket.card_id() is None
            sim.place_card("0012345678")
            assert await socket.card_id() == "0012345678"
            await socket.close()
        await sim.stop()

        async def silent(ws, request):
            pass
        runner, url = await serve(silent)
        async with aiohttp.ClientSession() as session:
            socket = SNWebSocket(url, session, timeout=0.2)
            with pytest.raises(EncoderError) as info:
                await socket.beep()
            assert info.value.code == NO_REPLY and not socket.is_open
        await runner.cleanup()
    asyncio.run(scenario())


def test_records_read_from_cards_in_one_shape():
    raw = [{"buildNo": 1, "floorNo": 2, "mac": "c540e09c8cc1", "startDate": 1700000000000,
            "endDate": "1760500000", "allowLockOut": 1},
           {"buildNo": 0, "floorNo": 0, "mac": "000000000000", "endDate": 1760500000},
           {"building": 3, "floor": 0, "lockMac": ""},
           {"BuildingNumber": "2", "FloorNumber": "4"},
           {"something": "else"}]
    assert card_records(raw) == [
        {"level": "door", "building": 1, "floor": 2, "mac": MAC, "starts_at": 1700000000,
         "expires_at": 1760500000, "allow_lockout": True},
        {"level": "all", "building": 0, "floor": 0, "mac": "000000000000", "starts_at": None,
         "expires_at": 1760500000, "allow_lockout": None},
        {"level": "building", "building": 3, "floor": 0, "mac": None, "starts_at": None,
         "expires_at": None, "allow_lockout": None},
        {"level": "floor", "building": 2, "floor": 4, "mac": None, "starts_at": None,
         "expires_at": None, "allow_lockout": None},
        {"level": None, "building": None, "floor": None, "mac": None, "starts_at": None,
         "expires_at": None, "allow_lockout": None},
    ]


# ---------------------------------------------------------------- revoke, notice, replacement

LOST = "3661413478"
LOST_UNTIL = int(time.time()) + 30 * 86400


def test_revoke_clears_only_the_card_asked_for():
    async def scenario(h):
        made = h.submit(card_type="staff")
        target = h.sim.place_card(initialized=True)
        await h.status(made, "done")
        await h.released()
        h.sim.remove_card()
        job_id = h.submit_kind("revoke", card_number=target)
        await h.live(job_id, "waiting_for_card", 1, f"Place card {target} on the encoder")
        other = h.sim.place_card(initialized=True)
        await h.live(job_id, "waiting_for_card", 1, f"That's card {other}, not {target}. Place "
                                                    f"card {target} on the encoder")
        await asyncio.sleep(0.05)
        assert "CE_ClearCard" not in list(h.sim.calls)[-5:]
        h.sim.place_card(target)
        job = await h.status(job_id, "done")
        assert job["cards"][0]["status"] == "cleared"
        assert job["cards"][0]["card_number"] == target and job["cards"][0]["written_at"]
        assert job["message"] == f"Card {target} was cleared. It no longer opens any doors."
        assert h.sim.cards[target]["records"] == [] and h.sim.cards[other]["records"] == []
        assert h.runner.live(job_id)["kind"] == "revoke"
        await h.released()
        # The card just cleared isn't written over unseen, but can be made again once lifted
        # off and put back.
        again = h.submit(card_type="staff")
        await h.live(again, "waiting_for_card", 1, "Take the card you just cleared off the "
                                                   "encoder")
        h.sim.remove_card()
        await h.live(again, "waiting_for_card", 1, "Place the card on the encoder")
        h.sim.place_card(target)
        await h.status(again, "done")
    run(scenario)


def test_revoke_wrong_card_left_on_needs_attention_and_blank_cards_are_not_cleared():
    async def scenario(h):
        job_id = h.submit_kind("revoke", card_number="0012345", card_wait=0.1)
        h.sim.place_card("999", initialized=True)
        job = await h.status(job_id, "needs_attention")
        assert job["message"] == ("That's card 999, not 0012345. Place card 0012345 on the "
                                  "encoder, then try again.")
        h.sim.place_card("12345")   # the same number without the leading zeros, but blank
        h.runner.retry(job_id)
        await h.until(lambda: h.jobs[job_id]["message"].startswith(
            "Card 0012345 couldn't be cleared"), "the blank card to be refused")
        h.runner.skip(job_id)
        job = await h.status(job_id, "done")
        assert job["message"] == "The card wasn't cleared"
        assert h.sim.cards["12345"]["hotel"] is None   # never set up as ours
        stopped = h.submit_kind("revoke", card_number="777")
        await h.live(stopped, "waiting_for_card")
        h.runner.cancel(stopped)
        assert (await h.status(stopped, "cancelled"))["message"] == ("Stopped before the card "
                                                                     "was cleared")
    run(scenario)


def test_lost_card_notice_on_a_blank_spare_card():
    async def scenario(h):
        job_id = h.submit_kind("notice", cancel_card=LOST, cancel_until=LOST_UNTIL)
        await h.live(job_id, "waiting_for_card", 1, "Place a spare card on the encoder for the "
                                                    "lost card notice")
        h.sim.place_card(LOST, initialized=True)   # the lost card turned up on the encoder
        await h.live(job_id, "waiting_for_card", 1, f"That's card {LOST}, the lost card itself. "
                                                    "Take it off and use a different card")
        spare = h.sim.place_card()   # blank
        job = await h.status(job_id, "done")
        assert job["message"] == ("The lost card notice is ready. Tap it on each door the lost "
                                  "card opens.")
        card = h.sim.cards[spare]
        assert card["hotel"] == "this hotel" and card["records"] == []
        assert card["cancels"] == [{"cardNo": LOST, "timestamp": LOST_UNTIL}]
        assert job["cards"][0] == {"seq": 1, "status": "written", "card_number": spare,
                                   "error": None, "written_at": job["cards"][0]["written_at"]}
        assert "CE_ReadCancellationInfo" in h.sim.calls
        # Look up reads it back.
        await h.released()
        h.runner.start_lookup(card_wait=1)
        await h.until(lambda: h.runner.lookup_state()["state"] == "done", "the look-up")
        found = h.runner.lookup_state()
        assert found["card_number"] == spare and found["card"] == []
        assert found["cancellations"] == [{"card_number": LOST, "expires_at": LOST_UNTIL}]
    run(scenario)


def test_a_notice_that_does_not_stay_on_the_card_needs_attention(monkeypatch):
    async def scenario(h):
        monkeypatch.setattr(EncoderSim, "_fn_CE_ReadCancellationInfo", lambda self, r: (0, []))
        job_id = h.submit_kind("notice", cancel_card=LOST, cancel_until=LOST_UNTIL)
        h.sim.place_card(initialized=True)
        job = await h.status(job_id, "needs_attention")
        assert job["message"].startswith(f"The cancellation of card {LOST} didn't stay")
        # An unreadable but non-empty answer is trusted (the driver's format isn't documented).
        monkeypatch.setattr(EncoderSim, "_fn_CE_ReadCancellationInfo",
                            lambda self, r: (0, [{"weird": 1}]))
        h.runner.retry(job_id)
        await h.status(job_id, "done")
        # An older SN Web Socket that doesn't know CE_CancelCard.
        h.sim.fail_next("CE_CancelCard", -3)
        second = h.submit_kind("notice", cancel_card=LOST, expires_at=LOST_UNTIL)
        h.sim.place_card(initialized=True)
        job = await h.status(second, "needs_attention")
        assert "SN Web Socket" in job["message"]
        h.runner.cancel(second)
        assert (await h.status(second, "cancelled"))["message"] == ("Stopped before the lost "
                                                                     "card notice was made")
    run(scenario)


def test_replacement_card_carries_the_doors_and_the_cancellation():
    async def scenario(h):
        job_id = h.submit_kind("write", records=json.dumps([POOL, FLOOR]), expires_at=EXPIRES,
                               cancel_card=LOST, cancel_until=LOST_UNTIL)
        await h.live(job_id, "waiting_for_card", 1, "Place the card on the encoder")
        number = h.sim.place_card(initialized=True)
        job = await h.status(job_id, "done")
        assert job["message"] == "The card is ready"
        card = h.sim.cards[number]
        assert len(card["records"]) == 2
        assert card["cancels"] == [{"cardNo": LOST, "timestamp": LOST_UNTIL}]
        calls = list(h.sim.calls)
        assert calls.index("CE_CancelCard") > max(i for i, c in enumerate(calls)
                                                  if c == "CE_WriteCard")
        # The cancellation's expiry defaults to the job's.
        second = h.submit_kind("write", records=json.dumps([POOL]), expires_at=EXPIRES,
                               cancel_card=LOST)
        h.sim.remove_card()
        other = h.sim.place_card(initialized=True)
        await h.status(second, "done")
        assert h.sim.cards[other]["cancels"] == [{"cardNo": LOST, "timestamp": EXPIRES}]
    run(scenario)


def test_replacement_that_loses_its_doors_is_not_written(monkeypatch):
    async def scenario(h):
        original = EncoderSim._fn_CE_CancelCard

        def wipes(self, request):   # an encoder that can't keep both on one card
            code, data = original(self, request)
            self.cards[self.present]["records"] = []
            return code, data
        monkeypatch.setattr(EncoderSim, "_fn_CE_CancelCard", wipes)
        job_id = h.submit_kind("write", records=json.dumps([POOL]), expires_at=EXPIRES,
                               cancel_card=LOST, cancel_until=LOST_UNTIL)
        h.sim.place_card(initialized=True)
        job = await h.status(job_id, "needs_attention")
        assert "Make a lost card notice instead" in job["message"]
        h.runner.cancel(job_id)
    run(scenario)


def test_revoke_and_notice_jobs_that_cannot_be_done():
    async def scenario(h):
        base = {"id": 99, "card_type": "staff", "count": 1}
        for bad in ({"kind": "erase"}, {"kind": "revoke"}, {"kind": "revoke", "card_number": ""},
                    {"kind": "revoke", "card_number": "1" * 65},
                    {"kind": "revoke", "card_number": "5", "count": 2},
                    {"kind": "notice", "expires_at": LOST_UNTIL},
                    {"kind": "notice", "cancel_card": "12AB", "expires_at": LOST_UNTIL},
                    {"kind": "notice", "cancel_card": "1" * 21, "expires_at": LOST_UNTIL},
                    {"kind": "notice", "cancel_card": LOST},
                    {"kind": "write", "records": json.dumps([POOL]), "expires_at": EXPIRES,
                     "cancel_card": "x"}):
            with pytest.raises(ValueError):
                h.runner.submit({**base, **bad})
        h.runner.submit({**base, "kind": "revoke", "card_number": 1234})   # a number is fine
        assert h.runner.live(99)["kind"] == "revoke"
        h.runner.cancel(99)
    run(scenario)


def test_simulated_cancellations_behave_like_cards():
    sim = EncoderSim()
    sim.connected = True
    ask = lambda function, **p: sim.answer(json.dumps({"function": function, "requestId": "r",
                                                       "hotelInfo": "k", **p}))
    blank = sim.place_card()
    assert ask("CE_CancelCard", cardNo=LOST, timestamp=LOST_UNTIL)["errorCode"] == 106
    assert ask("CE_ReadCancellationInfo")["errorCode"] == 106
    assert ask("CE_InitCard")["errorCode"] == 0
    assert ask("CE_CancelCard", cardNo=int(LOST), timestamp=LOST_UNTIL)["errorCode"] == 2
    assert ask("CE_CancelCard", cardNo=LOST, timestamp="soon")["errorCode"] == 2
    for n in range(4):
        assert ask("CE_CancelCard", cardNo=str(n), timestamp=LOST_UNTIL)["errorCode"] == 0
    assert ask("CE_CancelCard", cardNo="9", timestamp=LOST_UNTIL)["errorCode"] == 104   # full
    assert ask("CE_CancelCard", cardNo="2", timestamp=1)["errorCode"] == 0  # same number: once
    reply = ask("CE_ReadCancellationInfo")
    assert reply["errorCode"] == 0 and len(reply["data"]) == 4
    assert {"cardNo": "2", "timestamp": 1} in reply["data"]
    assert ask("CE_ClearCard")["errorCode"] == 0
    assert ask("CE_ReadCancellationInfo")["data"] == []
    # Cancellations take a sector of their own: with 5 sectors, 16 records still fit.
    for n in range(20):
        assert ask("CE_WriteCard", buildNo=1, floorNo=1, mac=MAC,
                   timestamp=EXPIRES)["errorCode"] == 0
    assert ask("CE_CancelCard", cardNo=LOST, timestamp=LOST_UNTIL)["errorCode"] == 104
    assert ask("CE_ClearCard")["errorCode"] == 0
    assert ask("CE_CancelCard", cardNo=LOST, timestamp=LOST_UNTIL)["errorCode"] == 0
    results = [ask("CE_WriteCard", buildNo=1, floorNo=1, mac=MAC, timestamp=EXPIRES)["errorCode"]
               for _ in range(17)]
    assert results == [0] * 16 + [104]
    assert sim.cards[blank]["hotel"] == "this hotel"


def test_cancellations_read_from_cards_in_one_shape():
    assert card_cancellations(encoder._raw_cancellations(
        json.dumps({"infoArray": [{"cardNo": LOST, "timestamp": LOST_UNTIL * 1000}]}))) == [
        {"card_number": LOST, "expires_at": LOST_UNTIL}]
    assert card_cancellations(encoder._raw_cancellations([123, {"cardNumber": "45"}, None])) == [
        {"card_number": "123", "expires_at": None}, {"card_number": "45", "expires_at": None}]
    assert encoder._raw_cancellations("") == [] and encoder._raw_cancellations(None) == []
    assert card_cancellations([{"cardNo": True}, {"cardNo": "x" * 65}]) == [
        {"card_number": None, "expires_at": None}, {"card_number": None, "expires_at": None}]
    with pytest.raises(EncoderError):
        encoder._raw_cancellations("not json")
    with pytest.raises(EncoderError):
        encoder._raw_cancellations(42)


def test_lookup_that_cant_connect_says_so_until_it_can():
    """A look-up that can't open the encoder shows the encoder's code, and the status keeps the
    problem (as a card does) until the encoder is next reached."""
    async def scenario(h):
        h.sim.fail_next("GetDeviceInfo", 2)
        h.runner.start_lookup(card_wait=1)
        await h.until(lambda: h.runner.lookup_state()["state"] == "error", "the look-up")
        message = h.runner.lookup_state()["message"]
        assert message == ("The card encoder reported a problem. Try again, or use a different "
                           "wristband. Encoder error 2.")
        assert h.runner.status()["error"] == message and h.runner.status()["connected"] is False
        await h.released()
        await h.runner.test()   # Reconnect
        assert h.runner.status()["error"] is None and h.runner.status()["connected"] is True
    run(scenario)
