"""Convert a WhatsApp text export to attributed Memseek observations; no model calls.

Supports bracketed iOS and dash-separated Android timestamps. Date order, timezone,
chat identity and owner display names must be supplied explicitly. Media is not read.
"""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import zipfile
from zoneinfo import ZoneInfo

HEADER = re.compile(
    r"^\[?(?P<date>\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}),?\s+"
    r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?(?:\s*[APap][Mm])?)"
    r"(?:\]\s*|\s+-\s+)(?P<body>.*)$"
)
CONTROLS = dict.fromkeys(map(ord, "\u200e\u200f\u202a\u202b\u202c\u202d\u202e"))


def clean(value):
    return value.translate(CONTROLS).replace("\u202f", " ").replace("\xa0", " ").strip()


def read_export(path):
    path = Path(path)
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            members = [i for i in archive.infolist() if i.filename.endswith(".txt")
                       and not i.filename.startswith("__MACOSX/")]
            if len(members) != 1 or members[0].file_size > 20_000_000:
                raise ValueError("Expected exactly one text export, under 20 MB, inside ZIP")
            return archive.read(members[0]).decode("utf-8-sig")
    if path.stat().st_size > 20_000_000:
        raise ValueError("Text export exceeds 20 MB; select a bounded export")
    return path.read_text(encoding="utf-8-sig")


def parse(text, date_order, timezone):
    messages = []
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = clean(raw)
        match = HEADER.match(line)
        if not match:
            if re.match(r"^\[?\d{1,2}[/.-]\d{1,2}[/.-]\d", line):
                raise ValueError(f"Unrecognized timestamp at line {line_number}")
            if line and not messages:
                raise ValueError(f"Unrecognized export preamble at line {line_number}")
            if messages:
                messages[-1]["body"] += "\n" + raw
            continue
        parts = list(map(int, re.split(r"[/.-]", match["date"])))
        day, month = parts[:2] if date_order == "dmy" else parts[:2][::-1]
        year = parts[2] + (2000 if parts[2] < 100 else 0)
        clock = match["time"].upper().strip()
        format_ = "%I:%M" if clock.endswith(("AM", "PM")) else "%H:%M"
        if clock.count(":") == 2:
            format_ += ":%S"
        if clock.endswith(("AM", "PM")):
            format_ += " %p"
            clock = re.sub(r"\s*([AP]M)$", r" \1", clock)
        parsed_time = datetime.strptime(clock, format_).time()
        stamp = datetime.combine(datetime(year, month, day), parsed_time, ZoneInfo(timezone))
        messages.append({"timestamp": stamp.isoformat(), "body": match["body"], "line": line_number})
    if not messages:
        raise ValueError("No timestamped messages found")
    return messages


def convert(text, *, date_order, timezone, chat_id, owner_names, since=None, until=None):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", chat_id):
        raise ValueError("chat-id must be 1–80 letters, digits, underscores or hyphens")
    owners = {clean(name) for name in owner_names}
    records, counts, duplicates = [], Counter(), Counter()
    for message in parse(text, date_order, timezone):
        day = message["timestamp"][:10]
        if (since and day < since) or (until and day >= until):
            counts["outside_window"] += 1
            continue
        speaker, separator, body = message["body"].partition(": ")
        if not separator:
            counts["system"] += 1
            continue
        speaker, body = clean(speaker), body.strip()
        if not body or "<attached:" in body or body.lower() in {
            "image omitted", "video omitted", "audio omitted", "sticker omitted", "gif omitted",
            "<media omitted>", "this message was deleted.", "you deleted this message."
        }:
            counts["media_or_deleted"] += 1
            continue
        if body.startswith("Messages and calls are end-to-end encrypted.") or body == "You pinned a message" or (
            body.startswith(speaker + " ") and any(fragment in body for fragment in
                (" created group “", " changed this group's icon", " pinned a message", " added you"))
        ):
            counts["system"] += 1
            continue
        authority = "owner" if speaker in owners else "external"
        identity = json.dumps([chat_id, message["timestamp"], speaker, body], ensure_ascii=False)
        occurrence = duplicates[identity]
        duplicates[identity] += 1
        event_id = "wa:" + hashlib.sha256((identity + f"#{occurrence}").encode()).hexdigest()
        observation = json.dumps({"channel": "WhatsApp", "chat_id": chat_id,
            "timestamp": message["timestamp"], "timezone": timezone,
            "speaker": speaker, "message": body}, ensure_ascii=False)
        if len(observation) > 12000:
            raise ValueError(f"Message at line {message['line']} exceeds observation limit; review manually")
        records.append({"text": observation, "source": f"whatsapp:{chat_id}",
                        "authority": authority, "event_id": event_id})
        counts[authority] += 1
    if not counts["owner"]:
        raise ValueError("No owner messages in selected window; verify exact display names")
    return records, dict(counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument("--output", required=True)
    parser.add_argument("--owner-name", action="append", required=True)
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--date-order", choices=["dmy", "mdy"], required=True)
    parser.add_argument("--timezone", required=True)
    parser.add_argument("--since", help="Inclusive YYYY-MM-DD")
    parser.add_argument("--until", help="Exclusive YYYY-MM-DD")
    args = parser.parse_args()
    for date in (args.since, args.until):
        if date:
            datetime.strptime(date, "%Y-%m-%d")
    rows, counts = convert(read_export(args.path), date_order=args.date_order,
        timezone=args.timezone, chat_id=args.chat_id, owner_names=args.owner_name,
        since=args.since, until=args.until)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(output, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({"records": len(rows), "counts": counts, "output": str(output)}))


if __name__ == "__main__":
    main()
