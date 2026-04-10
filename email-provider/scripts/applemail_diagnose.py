from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.applemail_diagnostics import AppleMailDiagnosticClient


def parse_iso8601(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise argparse.ArgumentTypeError("invalid ISO8601 datetime")
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO8601 datetime: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def entry_to_dict(entry):
    return {
        "mailbox": entry.mailbox,
        "date": entry.date,
        "sender": entry.sender,
        "subject": entry.subject,
        "preview": entry.preview,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect AppleMail inbox and junk folders.")
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--refresh-token", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--proxy")
    parser.add_argument("--api-base", default="https://www.appleemail.top")
    parser.add_argument("--mode", choices=("latest", "all"), default="latest")
    parser.add_argument("--mailbox", action="append", dest="mailboxes")
    parser.add_argument("--subject-filter")
    parser.add_argument("--sender-filter")
    parser.add_argument("--content-filter")
    parser.add_argument("--after", type=parse_iso8601)
    parser.add_argument("--before", type=parse_iso8601)
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    client = AppleMailDiagnosticClient(
        client_id=args.client_id,
        refresh_token=args.refresh_token,
        email=args.email,
        proxy=args.proxy,
        api_base=args.api_base,
        log_fn=print,
    )
    entries = client.inspect_mailboxes(
        mailboxes=args.mailboxes or ("INBOX", "Junk"),
        mode=args.mode,
        subject_filter=args.subject_filter,
        sender_filter=args.sender_filter,
        content_filter=args.content_filter,
        after=args.after,
        before=args.before,
    )
    if args.json_output:
        print(json.dumps([entry_to_dict(entry) for entry in entries], ensure_ascii=False, indent=2))
        return 0

    if not entries:
        print("No matching mail found.")
        return 1

    for idx, entry in enumerate(entries, start=1):
        print(f"[{idx}] mailbox={entry.mailbox}")
        print(f"date: {entry.date}")
        print(f"sender: {entry.sender}")
        print(f"subject: {entry.subject}")
        print(f"preview: {entry.preview[:200]}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
