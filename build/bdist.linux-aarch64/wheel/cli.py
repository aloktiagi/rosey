"""REPL for testing the agent locally. Run: python -m cli +14155551234"""
from __future__ import annotations

import sys

from dotenv import load_dotenv

from agent import handle_message


def main() -> None:
    load_dotenv()
    from_phone = sys.argv[1] if len(sys.argv) > 1 else "+15555550100"
    print(f"chatting as {from_phone} — Ctrl-C to quit")
    while True:
        try:
            body = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not body:
            continue
        try:
            print(handle_message(from_phone, body))
        except Exception as e:
            print(f"[error] {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
