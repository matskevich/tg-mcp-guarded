#!/usr/bin/env python3
"""Create/refresh a Telegram session using QR login."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

# Resolve imports from repository root.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHONPATH_ENTRIES = (str(PROJECT_ROOT), str(PROJECT_ROOT / "tganalytics"))
for entry in PYTHONPATH_ENTRIES:
    if entry not in sys.path:
        sys.path.insert(0, entry)

# Allow auth-only bootstrap requests for this helper.
os.environ.setdefault("TG_AUTH_BOOTSTRAP", "1")

from telethon.errors import SessionPasswordNeededError

from tganalytics.infra.limiter import safe_call
from tganalytics.infra.tele_client import get_client_for_session


async def main() -> int:
    parser = argparse.ArgumentParser(description="Create Telegram session with QR login")
    parser.add_argument(
        "--session-name",
        required=True,
        help="Session name (file will be <session-dir>/<session-name>.session)",
    )
    parser.add_argument(
        "--session-dir",
        default="data/sessions",
        help="Directory to store session files (default: data/sessions)",
    )
    parser.add_argument(
        "--timeout-sec",
        type=int,
        default=180,
        help="QR login wait timeout in seconds (default: 180)",
    )
    parser.add_argument(
        "--terminal-qr",
        action="store_true",
        help="render the login token as a scannable terminal QR without printing its URL",
    )
    args = parser.parse_args()

    session_path = (PROJECT_ROOT / args.session_dir / args.session_name).resolve()
    print("🔐 creating telegram session via qr")
    print(f"   session file: {session_path}.session")
    print()

    client = get_client_for_session(str(session_path))
    try:
        await client.connect()
        if await client.is_user_authorized():
            me = await safe_call(client.get_me, operation_type="api")
            print(f"✅ already authorized: @{me.username} (id={me.id})")
            return 0

        qr_login = await client.qr_login()
        if args.terminal_qr:
            import qrcode

            qr = qrcode.QRCode(border=2)
            qr.add_data(qr_login.url)
            qr.make(fit=True)
            print("scan this qr in the Telegram app:")
            qr.print_ascii(invert=True)
        else:
            print("scan this qr login url in Telegram app:")
            print(qr_login.url)
            print()
        print("where to scan:")
        print("  Telegram -> Settings -> Devices -> Link Desktop Device")
        print(f"waiting up to {args.timeout_sec}s...")

        try:
            await qr_login.wait(timeout=args.timeout_sec)
        except SessionPasswordNeededError:
            print("2FA enabled: enter your Telegram password in the prompt.")
            password = getpass.getpass("Telegram 2FA password: ")
            await client.sign_in(password=password)
        except (asyncio.TimeoutError, TimeoutError):
            print("❌ qr login timed out.")
            print("   retry command and scan the new qr quickly.")
            return 2

        if not await client.is_user_authorized():
            print("❌ login not completed.")
            return 3

        me = await safe_call(client.get_me, operation_type="api")
        print(f"✅ logged in as: @{me.username} (id={me.id})")
        return 0
    finally:
        await client.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
