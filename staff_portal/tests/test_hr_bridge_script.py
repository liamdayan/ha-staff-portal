"""The HR portal's Google Drive script (app/hr/bridge.gs) under Node, with stand-ins for the
Apps Script services (tests/bridge_test.js), and the script text Staff Access hands the owner.
Skipped when node isn't installed. Nothing here talks to Google."""

import json
import os
import shutil
import subprocess

import pytest

from hr import gdrive

HERE = os.path.dirname(os.path.abspath(__file__))
HR_DRIVE = "0AHrDriveAAAAAAAAAAAA"


def test_script_source_fills_in_the_secret_and_version():
    secret = gdrive.new_secret()
    text = gdrive.script_source(secret)
    assert f'var SECRET = "{secret}";' in text
    assert f"var VERSION = {gdrive.SCRIPT_VERSION};" in text
    assert f"script version {gdrive.SCRIPT_VERSION}" in text
    assert "{{" not in text
    assert f"var PROTOCOL = {gdrive.PROTOCOL};" in text
    # Each install gets its own secret; a secret that could break out of the text is refused.
    assert gdrive.new_secret() != secret and len(secret) >= 43
    for bad in ("", "short", 'abc"; doEvil(); var x="' + "a" * 40, "a" * 40 + "\n"):
        with pytest.raises(ValueError):
            gdrive.script_source(bad)
    # It says what it is and that it only touches the HR drives, near the top.
    head = text[:1500]
    assert "Staff Access" in head and "only inside the shared drive(s) you picked" in head


@pytest.mark.skipif(shutil.which("node") is None, reason="node isn't installed")
def test_bridge_script_under_node(tmp_path):
    secret = gdrive.new_secret()
    script = tmp_path / "bridge.js"
    script.write_text(gdrive.script_source(secret), encoding="utf-8")
    subprocess.run(["node", "--check", str(script)], check=True)

    # Requests signed here, as the add-on signs them, with text that's hard to make the same in
    # two languages: accents, an emoji, control characters, quotes, a line separator.
    odd = {"note": "\u00fcn\u00efcode \u2713 \U0001F600 \x01\x1f\x7f \"q\" \\ / \u2028 \t\n",
           "nested": {"b": [1, -2, True, None, 2 ** 53], "a": "x", "\u00e9": ""}}
    name = "M\u00fcller, Zo\u00eb (CT 7) \u2713 \"Q\" 'S'"
    ping = gdrive.signed_request(secret, "ping", odd)
    folder = gdrive.signed_request(secret, "ensureFolder", {"parent": HR_DRIVE, "name": name,
                                                            "props": {"ct_user": "7"}})
    tampered = json.loads(json.dumps(gdrive.signed_request(secret, "ping", odd)))
    tampered["args"]["note"] += "!"
    vectors = [{"name": "odd text", "body": json.dumps(ping), "expect": "ok"},
               {"name": "odd folder name", "body": json.dumps(folder), "expect": "ok",
                "folder_name": name},
               {"name": "sent as UTF-8", "body": json.dumps(
                   gdrive.signed_request(secret, "ping", odd), ensure_ascii=False), "expect": "ok"},
               {"name": "tampered", "body": json.dumps(tampered), "expect": "auth"},
               {"name": "replayed", "body": json.dumps(ping), "expect": "auth"}]
    (tmp_path / "vectors.json").write_text(json.dumps(vectors), encoding="utf-8")
    done = subprocess.run(["node", os.path.join(HERE, "bridge_test.js"), str(script), secret,
                           str(tmp_path / "vectors.json")],
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stdout + done.stderr
    assert " 0 failed" in done.stdout
