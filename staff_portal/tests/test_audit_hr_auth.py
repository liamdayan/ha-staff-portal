"""Audit fixes for HR sign-in: wrong authenticator codes sent at the same time all count toward
the lock, and turning on an authenticator from My account honours locks and the address
throttle.

Nothing here talks to a real Connecteam, Google, TT Hotel or Home Assistant.
"""

import asyncio
import time

from hr_helpers import HR, ct_user

import main  # noqa: E402
from hr import signin  # noqa: E402

ANA = ct_user(101, "Ana", "Lee", code="4821", groups=[501], mobile="+1 541-555-0101")
MAX = ct_user(103, "Max", "Ray", code="99001", groups=[501], mobile="541-555-0103")


def run(coro):
    return asyncio.run(coro)


def test_wrong_codes_at_the_same_time_all_count(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            await hr.add_people(MAX)
            hr.enrol("103")
            body = {"mobile": "5415550103", "pin": "99001", "code": "000000"}
            replies = await asyncio.gather(*[hr.call("POST", "/api/signin", body)
                                             for _ in range(9)])
            assert all(r[0] == 401 for r in replies)
            p = hr.hdb.person("103")
            assert p["day_failed"] == 9
            assert (p["locked_until"] or 0) > time.time()
    run(scenario())


def test_turning_on_an_authenticator_honours_the_lock(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            await hr.add_people(ANA)
            token = await hr.sign_in("101")
            for _ in range(signin.LOCK_AFTER):
                status, _, _ = await hr.call("POST", "/api/account/authenticator",
                                             {"pin": "0000"}, token)
                assert status == 403
            assert signin.locked(hr.hdb.person("101"))
            # Still signed in, but the right PIN doesn't get past the lock.
            status, data, _ = await hr.call("POST", "/api/account/authenticator",
                                            {"pin": "4821"}, token)
            assert status == 403 and "Too many wrong tries" in data["error"]
            assert "secret" not in data
    run(scenario())


def test_turning_on_an_authenticator_counts_against_the_address(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            await hr.add_people(ANA)
            token = await hr.sign_in("101")
            for _ in range(signin.ADDRESS_LIMIT):
                status, _, _ = await hr.call("POST", "/api/account/authenticator",
                                             {"pin": "0000"}, token)
                assert status == 403
                signin.unlock(hr.hdb, "101")   # Ana's own lock aside: this is about the address
            status, _, _ = await hr.call("POST", "/api/account/authenticator",
                                         {"pin": "4821"}, token)
            assert status == 429
    run(scenario())


def test_confirming_again_counts_against_the_address(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            await hr.add_people(ANA)
            token = await hr.sign_in("101")
            for _ in range(signin.ADDRESS_LIMIT):
                status, _, _ = await hr.call("POST", "/api/reconfirm", {"pin": "0000"}, token)
                assert status == 403
                signin.unlock(hr.hdb, "101")   # Ana's own lock aside: this is about the address
            status, _, _ = await hr.call("POST", "/api/reconfirm", {"pin": "4821"}, token)
            assert status == 429
    run(scenario())
