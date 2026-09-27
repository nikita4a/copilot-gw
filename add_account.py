"""Add a GitHub Copilot account to accounts.json via OAuth device flow.

    python add_account.py [--label acc1] [--accounts accounts.json]

Prints the one-time code + verification URL, polls until the user
authorizes, then stores the gho_ token. Never prints tokens to history:
prompt for --label if it may leak, or pass it explicitly.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import webbrowser

from aiohttp import ClientSession  # noqa: F401  (documented dep)

from copilot import CopilotClient, make_session, new_device_id


async def run(label: str, accounts_path: str, open_browser: bool) -> int:
    async with make_session() as session:
        client = CopilotClient(session, new_device_id())
        dc = await client.device_code()

        print("=" * 60)
        print(f"  Login to:   {dc.verification_uri}")
        print(f"  Enter code: {dc.user_code}")
        print(f"  Expires in: {dc.expires_in}s (poll interval {dc.interval}s)")
        print("=" * 60)
        if open_browser:
            try:
                webbrowser.open(dc.verification_uri)
            except Exception:
                pass

        last_status = [None, ""]

        def poll_hook(status, body):
            if status != last_status[0] or body != last_status[1]:
                last_status[0], last_status[1] = status, body
                if status == 200:
                    print(f"[poll] {body[:120]}")
                elif status != 200:
                    print(f"[poll] http {status} (retrying)")

        token = await client.poll_access_token(dc, poll_hook=poll_hook)

    # merge into accounts.json (preserve other accounts)
    accounts = []
    if os.path.exists(accounts_path):
        with open(accounts_path, encoding="utf-8") as f:
            accounts = json.load(f)
    accounts = [a for a in accounts if a.get("label") != label]
    accounts.append({"label": label, "gh_token": token})
    tmp = accounts_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(accounts, f, indent=2)
    os.replace(tmp, accounts_path)
    # best-effort: keep the file owner-private on POSIX
    try:
        os.chmod(accounts_path, 0o600)
    except OSError:
        pass
    print(f"OK: account '{label}' saved to {accounts_path} "
          f"({len(accounts)} total). Token NOT printed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--label", default=None,
                    help="account label (default: acc<timestamp>)")
    ap.add_argument("--accounts", default=os.environ.get("ACCOUNTS_PATH",
                                                         "accounts.json"))
    ap.add_argument("--no-browser", action="store_true",
                    help="do not auto-open the verification URL")
    args = ap.parse_args()

    label = args.label or f"acc{os.getpid()}"
    try:
        return asyncio.run(run(label, args.accounts, not args.no_browser))
    except KeyboardInterrupt:
        print("\nInterrupted; nothing saved.")
        return 130
    except Exception as e:  # noqa: BLE001 - CLI must always exit cleanly
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())