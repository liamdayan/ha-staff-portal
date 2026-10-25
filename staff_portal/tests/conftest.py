"""Shared test setup: the add-on's modules import each other as siblings (as main.py does), so
the app folder goes on the import path. Tests use asyncio.run and aiohttp's own servers on
127.0.0.1; nothing here talks to TT Hotel, Connecteam or Home Assistant."""

import os
import sys

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
if APP not in sys.path:
    sys.path.insert(0, APP)
