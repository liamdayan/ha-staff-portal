"""The HR portal's parts working together, as they run for real: certificates and documents (a
confirmed card reaches Google Drive and Connecteam through the delivery queue the service ticks),
discipline and documents (a signed write-up's PDF through the documents part's PDF queue, once),
reviews with probation and documents (the end-of-probation recommendation reaches HR's decision,
and the final PDF reaches Drive), the review side panel with real certificate statuses and the
discipline summary, and the wiring itself (each part once, in one place).

The point system is the real one (policy.py, points.py) with a small published policy
(tests/point_system.py): from an incident to its write-up's end date reaching the ledger, the
totals dropping that day, a repeat's step, and the review side panel's real points.

Google is the fake script (tests/fake_bridge.py) and Connecteam a fake server. Nothing here
talks to a real Google, Connecteam, TT Hotel or Home Assistant.
"""

import asyncio
import copy
import json
import time
from datetime import date, timedelta

import point_system
from fake_bridge import html_text
from hr_helpers import HR, ct_user, queue_done
from test_hr_certificates import DocsConnecteam, add, jpeg
from test_hr_documents import google

from hr import (certificates, delivery, discipline, documents, notify, parts, points, policy,  # noqa: E402
                probation, reviews, server, service, signin)

ANA = ct_user(201, "Ana", "Lee", code="4821", groups=[501], mobile="541-555-0201")
MAX = ct_user(203, "Max", "Ray", code="9900", groups=[501], mobile="541-555-0203")
HANA = ct_user(205, "Hana", "Ito", code="7788", groups=[502], mobile="541-555-0205")
IVY = ct_user(206, "Ivy", "Park", code="3344", groups=[502], mobile="541-555-0206")
WHO = {"ana": "201", "max": "203", "hana": "205", "ivy": "206"}
FORM_KEYS = ["guest_service", "teamwork", "reliability", "job_knowledge", "cleanliness",
             "communication", "policy"]


def run(coro):
    return asyncio.run(coro)


async def setup(hr: HR) -> dict:
    """Ana works in the Pub (501), which Max manages (primary); Hana and Ivy are HR admins.
    Every session has just typed its code. Returns a session for each."""
    await hr.add_people(ANA, MAX, HANA, IVY)
    hr.give("203", "manager", {"501": True})
    hr.give("205", "hr_admin")
    hr.give("206", "hr_admin")
    sessions = {name: signin.new_session(hr.hdb, pid, "127.0.0.1", "test") for name, pid in WHO.items()}
    hr.hdb.conn.execute("UPDATE sessions SET confirmed_at = ?", (time.time() + 3600,))
    hr.hdb.conn.commit()
    hr.svc.sync_state["tried"] = time.time()      # the service's ticks don't read Connecteam
    return sessions


def today(hr: HR) -> date:
    return probation.today_of(hr.hdb)


def kinds(hr: HR, name: str) -> list[str]:
    return [i["kind"] for i in notify.inbox(hr.hdb, WHO[name], 200)]


async def get(hr: HR, url: str, cookie: str):
    resp = await hr.client.get(url, headers={"Host": "hr.example.com", "Cookie": f"{signin.COOKIE}={cookie}"})
    return resp.status, await resp.read()


# ---------------------------------------------------------------- certificates and documents

def test_a_confirmed_card_reaches_drive_and_connecteam_by_the_services_own_ticks(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr, google(hr, monkeypatch) as fake, DocsConnecteam() as ct:
            s = await setup(hr)
            olcc = certificates.type_by_key(hr.hdb, "olcc")
            certificates.update_type(hr.hdb, olcc["id"], {"groups": json.dumps(["501"]), "groups_confirmed": 1,
                                                          "ct_document": 52114})
            hr.db.update_settings({"hr_connecteam_documents": "1"})
            status, data = await add(hr, s["ana"], "201", "olcc", jpeg())
            assert status == 200
            await certificates.settle(hr.svc)
            cid = data["card"]["id"]
            status, r, _ = await hr.call("POST", f"/api/people/201/certificates/{cid}/confirm",
                                         {"expires": "2029-06-14"}, s["hana"])
            assert status == 200, r
            # One document, made in one call, with its Connecteam copy and expiry: no second
            # activation, so its Drive and Connecteam work was queued once each.
            card = certificates.get(hr.hdb, cid)
            doc = documents.get(hr.hdb, card["document_id"])
            assert (doc["ct_document"], doc["ct_expires"]) == (52114, "2029-06-14")
            assert sorted(d["target"] for d in documents.deliveries_of(hr.hdb, doc["id"])) == ["connecteam", "drive"]
            # Nothing calls the queue by hand: the service's own tick (documents' tick job) runs it.
            await delivery.settle(hr.svc)
            await hr.svc.tick(time.time() + 1)
            await delivery.settle(hr.svc)
            name = f"{today(hr).isoformat()} OLCC Service Permit - expires 2029-06-14.jpg"
            assert fake.names_in("Certificates") == [name]
            [folder] = [f for f in fake.files.values() if f["name"] == "Certificates"]
            assert fake.files[folder["parents"][0]]["name"] == "Lee, Ana (CT 201)"
            assert [(u, d) for u, d, _ in ct.entries] == [(201, 52114)]
            assert date.fromtimestamp(ct.entries[0][2]["expiresAt"]).year == 2029
            assert [d["status"] for d in documents.deliveries_of(hr.hdb, doc["id"])] == ["done", "done"]
            # Ana opens her card's photo as the document it became, from here and, once this
            # computer's copy has gone, from Drive; her manager sees dates only.
            _, mine, _ = await hr.call("GET", "/api/people/201/certificates", cookie=s["ana"])
            [shown] = [c for c in mine["cards"] if c["id"] == cid]
            assert shown["photo"] == documents.view_url(doc)
            status, first = await get(hr, shown["photo"], s["ana"])
            assert status == 200 and first[:3] == b"\xff\xd8\xff"
            documents.drop_copy(hr.hdb, hr.svc.folder, documents.get(hr.hdb, doc["id"]))
            status, again = await get(hr, shown["photo"], s["ana"])
            assert (status, again) == (200, first)
            assert (await get(hr, shown["photo"], s["max"]))[0] == 404
            # The certificates part made no tables of documents' (documents' part started first).
            assert "documents.setup" not in open(certificates.__file__).read()
    run(scenario())


# ---------------------------------------------------------------- discipline and documents

async def signed_writeup(hr: HR, s: dict) -> int:
    """An incident with its write-up, approved by HR (Hana, then Ivy), sent to Ana and signed by
    her in the portal: the write-up's ID."""
    status, data, _ = await hr.call("POST", "/api/people/201/incidents", {
        "offence": "rude", "what": "Shouted at a guest.", "points": "",
        "happened": (today(hr) - timedelta(days=1)).isoformat(),
        "writeup": {"improvement": "Stay calm.", "consequences": "A final written warning.",
                    "support": "Guest service refresher.", "manager_note": "MGR-ONLY-NOTE"}}, s["max"])
    assert status == 200, data
    wid = data["writeup"]
    assert (await hr.call("POST", f"/api/incidents/{data['incident']}/approve", {}, s["hana"]))[0] == 200
    assert (await hr.call("POST", f"/api/writeups/{wid}/approve", {}, s["ivy"]))[0] == 200
    assert (await hr.call("POST", f"/api/writeups/{wid}/send", {}, s["max"]))[0] == 200
    status, r, _ = await hr.call("POST", f"/api/writeups/{wid}/sign",
                                 {"name": "Ana Lee", "comment": "", "pin": "4821"}, s["ana"])
    assert status == 200, r
    return wid


def test_a_signed_writeups_pdf_goes_through_the_documents_queue_once(tmp_path, monkeypatch):
    pol, pts = point_system.install(monkeypatch)
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr, google(hr, monkeypatch) as fake:
            s = await setup(hr)
            pts.add("201", 4, today(hr) - timedelta(days=30))
            # The first time, filing it on the write-up fails after the PDF was made: the retry
            # uses that same PDF (the queue's upload_id), and Google makes it only once.
            maker, done = documents.PDF_MAKERS["writeups"]
            calls = []

            def flaky(svc, row, doc):
                calls.append(doc["id"])
                if len(calls) == 1:
                    raise RuntimeError("crashed while filing it")
                return done(svc, row, doc)
            monkeypatch.setitem(documents.PDF_MAKERS, "writeups", (maker, flaky))
            wid = await signed_writeup(hr, s)
            await queue_done(hr)
            work = documents.pdf_work(hr.hdb, "writeups", wid)
            assert (work["status"], work["tries"]) == ("waiting", 1) and len(fake.pdfs) == 1
            await queue_done(hr, time.time() + 600)
            assert documents.pdf_work(hr.hdb, "writeups", wid)["status"] == "done"
            assert len(set(calls)) == 1 and len(fake.pdfs) == 1
            w = discipline.get_writeup(hr.hdb, wid)
            pdf = documents.get(hr.hdb, w["pdf_doc_id"])
            assert documents.kind(hr.hdb, pdf["kind_id"])["key"] == "writeups"
            title = f"{w['step_name']} - Conduct (signed)"
            assert fake.names_in("Discipline") == [f"{today(hr).isoformat()} {title}.pdf"]
            # Plain, escaped HTML from pdfdoc: what Ana saw, never the manager's note.
            page = fake.pdfs[0]
            assert "MGR-ONLY-NOTE" not in page and "Hot Lake" in html_text(page)[1]
            assert "Stay calm." in " ".join(html_text(page)) and "<script" not in page
            # Asked again (a double click, a restart): nothing new is made or filed.
            documents.queue_pdf(hr.hdb, "writeups", "201", wid)
            await queue_done(hr, time.time() + 1200)
            assert len(fake.pdfs) == 1 and len(fake.names_in("Discipline")) == 1
            assert hr.hdb.conn.execute("SELECT COUNT(*) FROM documents WHERE kind_id = ?",
                                       (pdf["kind_id"],)).fetchone()[0] == 1
            # Ana and Max open it through the documents part; it's never in her Documents section.
            _, view, _ = await hr.call("GET", f"/api/writeups/{wid}", cookie=s["ana"])
            assert view["pdf_url"] == documents.view_url(pdf)
            assert (await get(hr, view["pdf_url"], s["ana"]))[0] == 200
            assert (await get(hr, view["pdf_url"], s["max"]))[0] == 200
            _, page_, _ = await hr.call("GET", "/api/people/201", cookie=s["hana"])
            [docs_section] = [x for x in page_["sections"] if x["name"] == "documents"]
            assert docs_section["documents"] == []
    run(scenario())


# ---------------------------------------------------------------- reviews, probation and documents

def test_an_end_of_probation_review_reaches_hrs_decision_and_its_pdf_reaches_drive(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr, google(hr, monkeypatch) as fake:
            s = await setup(hr)
            db = hr.hdb
            day = today(hr)
            # Ana started 69 days ago: the last day of her 90-day introductory period is in 20 days,
            # its review is due 14 days before. The nightly run (every part's job) starts the
            # period and opens the review for Max, her department's primary manager.
            db.update_person("201", start_date=(day - timedelta(days=69)).isoformat())
            await hr.svc.nightly()
            prob = probation.current(db, "201", day)
            end = date.fromisoformat(prob["end"])
            assert end == day + timedelta(days=20)
            [due] = probation.end_review_due(db, day + timedelta(days=14))
            assert (due["probation_id"], due["due"]) == (prob["id"], (end - timedelta(days=14)).isoformat())
            [rid] = [r[0] for r in db.conn.execute("SELECT id FROM reviews WHERE kind = 'probation'")]
            review = reviews.get(db, rid)
            assert (review["probation_id"], review["period_end"], review["due"]) == (prob["id"], prob["end"], due["due"])
            assert "review_due" in kinds(hr, "max")
            # Its due day: the review's own reminders tell Max, not probation's as well.
            probation.daily(db, date.fromisoformat(due["due"]))
            assert "probation_due" not in kinds(hr, "max") and "probation_due" not in kinds(hr, "hana")
            # Max recommends extending by 30 days; HR approves the review, and the recommendation
            # reaches the probation (from the review, by Max) for HR to decide.
            path = f"/api/reviews/{rid}"
            for key in FORM_KEYS:
                assert (await hr.call("PUT", f"{path}/answer", {"section": key, "rating": 3}, s["max"]))[0] == 200
            new_end = end + timedelta(days=30)
            await hr.call("PUT", f"{path}/overall", {"recommendation": "extend", "extend_to": new_end.isoformat()}, s["max"])
            status, r, _ = await hr.call("POST", f"{path}/submit", {}, s["max"])
            assert (status, r["status"]) == (200, "approval")
            assert probation.get(db, prob["id"])["recommendation"] is None
            assert (await hr.call("POST", f"{path}/approve", {}, s["hana"]))[0] == 200
            row = probation.get(db, prob["id"])
            assert (row["recommendation"], row["recommend_to"], row["recommend_review"], row["recommended_by"]) == \
                ("extend", new_end.isoformat(), str(rid), "203")
            assert reviews.get(db, rid)["recommendation_sent_at"]
            assert "probation_recommended" in kinds(hr, "ivy")
            _, page, _ = await hr.call("GET", "/api/people/201", cookie=s["ivy"])
            [shown] = [x for x in page["sections"] if x["name"] == "probation"][0]["probations"]
            assert shown["recommendation"]["outcome"] == "extend" and shown["recommendation"]["from_review"]
            # HR decides: extended. The review that recommended it doesn't move.
            status, r, _ = await hr.call("POST", f"/api/probations/{prob['id']}/decide",
                                         {"outcome": "extend", "extend_to": new_end.isoformat(),
                                          "reason": "As the review recommends."}, s["ivy"])
            assert status == 200, r
            assert probation.get(db, prob["id"])["ends"] == new_end.isoformat()
            reviews.follow_probation(db, day)
            assert reviews.get(db, rid)["due"] == due["due"]
            # Max shares it and Ana acknowledges: closed, and its PDF (without the recommendation,
            # which is HR's) reaches her Reviews folder through the service's own tick.
            assert (await hr.call("POST", f"{path}/share", {"meeting_date": day.isoformat()}, s["max"]))[0] == 200
            status, view, _ = await hr.call("GET", path, cookie=s["ana"])
            assert "recommendation" not in view
            assert (await hr.call("POST", f"{path}/acknowledge", {"answer": "received"}, s["ana"]))[0] == 200
            await delivery.settle(hr.svc)
            await hr.svc.tick(time.time() + 1)
            await delivery.settle(hr.svc)
            title = "End of introductory period review"
            assert fake.names_in("Reviews") == [f"{day.isoformat()} {title}.pdf"]
            text = " ".join(html_text(fake.pdfs[0]))
            assert "Ana Lee" in text and "Extend" not in text and "extend" not in text
            assert documents.title_of(documents.get(db, reviews.get(db, rid)["pdf_doc_id"])) == title
            # Extended after its review closed: another end-of-probation review opens for the new end.
            new_due = new_end - timedelta(days=14)
            reviews.follow_probation(db, new_due - timedelta(days=14))
            later = [dict(r) for r in db.conn.execute("SELECT * FROM reviews WHERE kind = 'probation' AND id != ?", (rid,))]
            assert [(r["due"], r["period_end"], r["status"]) for r in later] == [
                (new_due.isoformat(), new_end.isoformat(), "writing")]
            reviews.follow_probation(db, new_due - timedelta(days=13))
            assert db.conn.execute("SELECT COUNT(*) FROM reviews WHERE kind = 'probation'").fetchone()[0] == 2
    run(scenario())


def test_the_review_side_panel_shows_real_certificates_and_discipline(tmp_path, monkeypatch):
    pol, pts = point_system.install(monkeypatch)
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            db = hr.hdb
            # The Pub needs an OLCC permit; Ana's ran out last week.
            olcc = certificates.type_by_key(db, "olcc")
            certificates.update_type(db, olcc["id"], {"groups": json.dumps(["501"]), "groups_confirmed": 1})
            db.update_person("201", start_date="2020-01-01", start_estimated=0, start_confirmed=1)
            db.conn.execute(
                "INSERT INTO certificates (person_id, type_key, status, mime, size, upload_id, added_at, "
                "read_state, expires, stored) VALUES ('201', 'olcc', 'confirmed', 'image/jpeg', 1, 'x', ?, "
                "'done', ?, 0)", (time.time(), (today(hr) - timedelta(days=7)).isoformat()))
            db.conn.commit()
            pts.add("201", 4, today(hr) - timedelta(days=30))
            wid = await signed_writeup(hr, s)
            step = discipline.get_writeup(db, wid)["step_name"]
            rid = reviews.create(db, db.person("201"), "adhoc", today(hr) + timedelta(days=10),
                                 period=((today(hr) - timedelta(days=90)).isoformat(),
                                         (today(hr) + timedelta(days=10)).isoformat()))
            status, data, _ = await hr.call("GET", f"/api/reviews/{rid}", cookie=s["max"])
            side = data["side"]
            assert side["certificates"] == [{"name": "OLCC Service Permit", "status": "Expired",
                                             "expires": (today(hr) - timedelta(days=7)).isoformat(),
                                             "flag": "May not serve alcohol"}]
            assert side["discipline"]["points"] == [{"track": "discipline", "total": 6.0}]
            assert side["discipline"]["writeups"] == [{"date": today(hr).isoformat(), "step": step,
                                                       "status": "Active"}]
            assert side["discipline"]["next_drop"]["points"] == 4.0
            assert side["period"]["end"] == today(hr).isoformat()      # points now, not on the due day
            # The same as the parts give for themselves.
            assert certificates.status_of(db, "201")[0]["flag"] == "May not serve alcohol"
            assert discipline.summary_for_review(db, "201", today(hr) - timedelta(days=90), today(hr))["points"] == \
                {"discipline": 6.0}
            # Ana never sees the panel.
            assert "side" not in (await hr.call("GET", f"/api/reviews/{rid}", cookie=s["ana"]))[1]
    run(scenario())


# ---------------------------------------------------------------- the wiring

def test_every_part_is_wired_once_in_one_place(tmp_path):
    # Each part once; the documents part before those keeping files there (they import it).
    names = [p.__module__ for p in service.PARTS]
    assert sorted(names) == ["hr.certificates", "hr.discipline", "hr.documents", "hr.points", "hr.policy",
                             "hr.probation", "hr.reviews"]
    assert all(names.index("hr.documents") < names.index(f"hr.{p}") for p in ("certificates", "discipline", "reviews"))
    assert set(parts.__dict__) >= {"documents", "policy", "points", "certificates", "probation",
                                   "discipline", "reviews"}
    assert discipline.points is points and discipline.policy is policy and probation.policy is policy
    # Other parts' kinds of document and PDFs.
    assert documents.OWNED == {"certificates": "certificates", "writeups": "discipline", "reviews": "reviews"}
    assert set(documents.PDF_MAKERS) == {"writeups", "reviews"}
    assert documents.CAN_OPEN == {"writeups": discipline.can_open_file, "reviews": reviews.can_open}
    # One set of message kinds: each part's own, none of them a PDF retry of its own any more.
    for kind in ("upload_stuck", "document_waiting", "cert_lapsed", "probation_due", "step_due",
                 "writeup_to_sign", "review_due", "review_overdue"):
        assert kind in notify.KINDS
    assert "writeup_pdf_stuck" not in notify.KINDS
    assert reviews.reminding in probation.review_reminds and len(probation.review_reminds) == 1
    assert len(probation.on_pass_clear) == 1
    # Person-page sections and HR settings, in order.
    assert [n for _, n, _ in server.SECTIONS] == ["details", "documents", "certificates", "probation",
                                                  "discipline", "reviews", "signin"]
    assert [n for _, n, _ in server.SETTINGS_SECTIONS] == ["roles", "documents", "certificates", "discipline",
                                                           "discipline_workflow", "reviews"]

    async def scenario():
        async with HR(tmp_path) as hr:
            svc = hr.svc
            for hooks in (svc.on_start, svc.tick_jobs, svc.daily_jobs, svc.migration_blockers, svc.on_stop):
                assert len(hooks) == len(set(hooks))
            assert len(svc.daily_jobs) == 6 and len(svc.migration_blockers) == 2
            assert points.nightly in svc.daily_jobs
            assert delivery.tick in svc.tick_jobs and probation.hourly in svc.tick_jobs
            assert documents.migration_blocker in svc.migration_blockers
            assert certificates.migration_blocker in svc.migration_blockers
            assert documents.kind_by_key(hr.hdb, "writeups")["folder"] == "Discipline"
            assert documents.kind_by_key(hr.hdb, "reviews")["folder"] == "Reviews"
            # A migration waits for every part's reason at once.
            svc.migration_blockers.append(lambda _: "One more reason.")
            hr.hdb.conn.execute("INSERT INTO certificates (person_id, type_key, status, mime, size, upload_id, "
                                "added_at, read_state, stored) VALUES ('1', 'olcc', 'waiting', 'image/jpeg', 1, "
                                "'y', 0, 'done', 1)")
            hr.hdb.conn.commit()
            reason = svc.migration_blocker()
            assert "waiting for HR to confirm" in reason and reason.endswith("One more reason.")
    run(scenario())


# ---------------------------------------------------------------- the real point system

def first_rule_body() -> dict:
    """The test policy with points stopping on the write-up's end date or after 12 months,
    whichever comes first (the design's recommended rule), and a second no call / no show within
    12 months going to a termination review."""
    body = copy.deepcopy(point_system.BODY)
    body["expiry"] = {"rule": "first", "rolling": {"months": 12}, "clean_reset": None}
    ncns = next(o for o in body["offences"] if o["key"] == "ncns")
    ncns["repeat"] = {"within": {"months": 12}, "tiers": [{"nth": 2, "add": 0, "step": "termination_review"}]}
    return body


def test_from_an_incident_to_its_points_dropping_on_the_write_ups_end_date(tmp_path, monkeypatch):
    pol, pts = point_system.install(monkeypatch)
    pol.publish(first_rule_body())

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            db, day = hr.hdb, today(hr)
            assert policy.set_up(db)
            # Max records it (with its write-up); HR approves: 2 points reach Coaching.
            wid = await signed_writeup(hr, s)
            w = discipline.get_writeup(db, wid)
            inc = discipline.get_incident(db, w["incident_id"])
            assert (inc["status"], inc["step_key"], w["step_key"], w["status"]) == ("approved", "coaching", "coaching", "active")
            assert w["step_name"] == "Coaching" and w["issued_on"] == day.isoformat()
            # The ledger has the incident, with its offence and scoring (so it can be re-scored).
            [line] = pts.lines("201", "incident")
            assert (line["offence"], line["q"], line["incident_id"], line["happened"]) == \
                ("rude", 8, str(inc["id"]), day - timedelta(days=1))
            assert line["scoring"]["final"] == 2.0 and line["version_id"] == int(inc["version_id"])
            # Signed: the write-up's end date (Coaching lasts 90 days) reached the points.
            stops = policy.step_end(first_rule_body(), "coaching", day)
            assert w["ends_on"] == (stops - timedelta(days=1)).isoformat()
            [end] = [ln for ln in pts.lines("201", "adjustment") if ln["end_kind"] == "writeup"]
            assert (end["ends"], end["target_id"]) == (stops, line["id"])
            # The totals drop on that day, not before (the 12-month window would be later).
            assert points.totals(db, "201", day) == {"discipline": 2.0}
            assert points.totals(db, "201", stops - timedelta(days=1)) == {"discipline": 2.0}
            assert points.totals(db, "201", stops) == {"discipline": 0.0}
            assert discipline.next_drop(db, "201", day) == {"date": stops.isoformat(), "points": 2.0}
            # Ana's statement: her incident, with its end date; no bookkeeping lines, no reasons.
            _, mine, _ = await hr.call("GET", "/api/people/201/discipline", cookie=s["ana"])
            assert [(x["label"], x["ends"], x["state"]) for x in mine["lines"]] == [
                ("Rude to a guest", stops.isoformat(), "counts")]
            assert all("reason" not in x for x in mine["lines"])
            _, theirs, _ = await hr.call("GET", "/api/people/201/discipline", cookie=s["hana"])
            assert any(x["label"].startswith("End date changed") and x["reason"] for x in theirs["lines"])
            # HR moves the end date earlier: the points follow.
            new_last = day + timedelta(days=29)
            status, r, _ = await hr.call("POST", f"/api/writeups/{wid}/ends",
                                         {"ends_on": new_last.isoformat(), "reason": "Agreed at the meeting."}, s["ivy"])
            assert status == 200, r
            assert points.totals(db, "201", new_last) == {"discipline": 2.0}
            assert points.totals(db, "201", new_last + timedelta(days=1)) == {"discipline": 0.0}
            # The review side panel shows the real points and the write-up.
            rid = reviews.create(db, db.person("201"), "adhoc", day + timedelta(days=10),
                                 period=((day - timedelta(days=30)).isoformat(), (day + timedelta(days=10)).isoformat()))
            status, data, _ = await hr.call("GET", f"/api/reviews/{rid}", cookie=s["max"])
            side = data["side"]["discipline"]
            assert side["points"] == [{"track": "discipline", "total": 2.0}]
            assert side["writeups"] == [{"date": day.isoformat(), "step": "Coaching", "status": "Active"}]
            assert side["next_drop"] == {"date": (new_last + timedelta(days=1)).isoformat(), "points": 2.0}
    run(scenario())


def test_repeats_probation_and_protected_leave_use_the_real_rules(tmp_path, monkeypatch):
    pol, pts = point_system.install(monkeypatch)
    pol.publish(first_rule_body())

    async def record(hr, s, offence, happened, **extra):
        status, data, _ = await hr.call("POST", "/api/people/201/incidents", {
            "offence": offence, "what": "It happened.", "points": "", "protected": "no",
            "happened": happened.isoformat(), **extra}, s["max"])
        assert status == 200, data
        status, r, _ = await hr.call("POST", f"/api/incidents/{data['incident']}/approve", {}, s["hana"])
        assert status == 200, r
        return discipline.get_incident(hr.hdb, data["incident"])

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            db, day = hr.hdb, today(hr)
            # A third late arrival within 90 days scores more (the repeat needs the day it happened).
            for n, back in enumerate((40, 30, 20)):
                inc = await record(hr, s, "late", day - timedelta(days=back))
                assert inc["final_points"] == (1.5 if n == 2 else 1.0), n
            # Protected leave: 0 points, no step.
            inc = await record(hr, s, "late", day - timedelta(days=10), protected="sick")
            assert (inc["final_points"], inc["step_key"]) == (0, None)
            assert pts.contexts[-1]["protected"] == "sick"
            # A second no call / no show within 12 months: its repeat's step, a termination review.
            first = await record(hr, s, "ncns", day - timedelta(days=5))
            assert first["step_key"] != "termination_review"
            second = await record(hr, s, "ncns", day - timedelta(days=2))
            assert second["step_key"] == "termination_review" and "2nd" in second["step_reason"]
            assert [t["kind"] for t in db.conn.execute(
                "SELECT kind FROM discipline_tasks WHERE incident_id = ?", (second["id"],))] == ["termination"]
            # During an introductory period, the probation ladder applies (3 points: Final written).
            db.update_person("203", start_date=(day - timedelta(days=20)).isoformat())
            probation.ensure_introductory(db, db.person("203"))
            assert probation.current(db, "203", day)["policy_key"] == "intro"
            assert points.required_step(first_rule_body(), {"on_probation": "intro"}, {"discipline": 3},
                                        "rude", None)["step"] == "final"
    run(scenario())
