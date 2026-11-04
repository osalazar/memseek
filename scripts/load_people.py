#!/usr/bin/env python3
"""Load an archivist people file as unreviewed private candidates.

The file is one person per line::

    Name|reach (emails/phones or HOLE)|role/context|P#|LANG|...

Blank lines and lines starting with ``#``, ``LOCK``, or ``KEEP`` are skipped.
A header whose first column is ``Name`` is skipped. Nothing in the file is
embedded in this script; pass the path at run time.

Each person is written through ``POST /records`` as a keyed ``people`` record:

* ``activation=candidate`` (not owner-reviewed, so people search hides it)
* ``visibility=private``
* ``biography_source_claim=archivist-file``
* text that says the row is an unreviewed candidate

Re-running the same file is idempotent: the record key is the folded
name, role, and reach, and the dedupe key is the canonical content. An
unchanged line comes back as a duplicate instead of a second current person.
``--dry-run`` parses and reports counts without calling the API.

Credentials come from ``MEMSEEK_API_KEY`` or ``MEMSEEK_KEY_FILE``. The key is
never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

FOREVER = 253402300799
TEXT_LIMIT = 4000


def fold(value: str) -> str:
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.casefold().split())


def skipped(line: str) -> bool:
    text = line.strip()
    if not text:
        return True
    if text.startswith("#") or text.startswith("LOCK") or text.startswith("KEEP"):
        return True
    return False


def is_header(line: str) -> bool:
    first = line.split("|", 1)[0].strip().casefold()
    return first == "name"


def _aliases(name: str, extra: list[str]) -> list[str]:
    found: list[str] = []
    start = 0
    while True:
        open_at = name.find("(", start)
        if open_at < 0:
            break
        close_at = name.find(")", open_at + 1)
        if close_at < 0:
            break
        inner = name[open_at + 1 : close_at].strip()
        if inner:
            found.append(inner)
        start = close_at + 1
    for part in extra:
        if "@" in part or part.isdigit():
            continue
        if any(ch.isalpha() for ch in part):
            found.append(part)
    seen: set[str] = set()
    unique: list[str] = []
    for alias in found:
        key = fold(alias)
        if key and key not in seen:
            seen.add(key)
            unique.append(alias)
    return unique


def parse_people(text: str) -> tuple[list[dict], list[str]]:
    """Return record bodies and a list of skip reasons. Raises on a bad line."""

    people: list[dict] = []
    notes: list[str] = []
    seen_keys: dict[str, str] = {}
    header_open = True
    for line_no, raw in enumerate(text.splitlines(), start=1):
        if skipped(raw):
            notes.append(f"{line_no}: skipped")
            continue
        if header_open and is_header(raw):
            header_open = False
            notes.append(f"{line_no}: header")
            continue
        header_open = False
        parts = [part.strip() for part in raw.split("|")]
        if len(parts) < 3:
            raise ValueError(f"line {line_no}: expected Name|reach|role, got {len(parts)} fields")
        name, reach, role = parts[0], parts[1], parts[2]
        if not name:
            raise ValueError(f"line {line_no}: name is empty")
        priority = parts[3] if len(parts) > 3 else ""
        language = parts[4] if len(parts) > 4 else ""
        extra = [part for part in parts[5:] if part]
        aliases = _aliases(name, extra)
        identity = "|".join((fold(name), fold(role), fold(reach)))
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        key = f"archivist-{digest}"
        signature = "|".join((name, reach, role, priority, language, ",".join(extra)))
        if key in seen_keys:
            if seen_keys[key] == signature:
                notes.append(f"{line_no}: duplicate")
                continue
            key = f"{key}-{line_no}"
        seen_keys[key] = signature
        body = _record(
            name=name,
            reach=reach,
            role=role,
            priority=priority,
            language=language,
            extra=extra,
            aliases=aliases,
            key=key,
            line_no=line_no,
            raw=raw.strip(),
        )
        people.append(body)
    return people, notes


def _record(
    *,
    name: str,
    reach: str,
    role: str,
    priority: str,
    language: str,
    extra: list[str],
    aliases: list[str],
    key: str,
    line_no: int,
    raw: str,
) -> dict:
    lines = [
        "(unreviewed) UNREVIEWED CANDIDATE from archivist-file; not owner-reviewed.",
        f"Name: {name}",
    ]
    if aliases:
        lines.append("Aliases: " + ", ".join(aliases))
    lines.append(f"Reach: {reach or 'HOLE'}")
    if role:
        lines.append(f"Role: {role}")
    if priority:
        lines.append(f"Priority: {priority}")
    if language:
        lines.append(f"Language: {language}")
    if extra:
        lines.append("Notes: " + " | ".join(extra))
    text = "\n".join(lines)
    if len(text) > TEXT_LIMIT:
        text = text[: TEXT_LIMIT - 1].rstrip() + "…"
    content = {
        "text": text,
        "kind": "relationship" if role else "fact",
        "activation": "candidate",
        "visibility": "private",
        "valid_until": FOREVER,
        "source_line": line_no,
        "source_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "biography_source_claim": "archivist-file",
        "reviewed": False,
        "source": "archivist-file",
    }
    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    dedupe = "archivist:" + hashlib.sha256(f"{key}\n{canonical}".encode("utf-8")).hexdigest()
    return {
        "key": key,
        "text": text,
        "content": content,
        "dedupe_key": dedupe,
        "name": name,
    }


def _api_key() -> str:
    key = os.environ.get("MEMSEEK_API_KEY", "").strip()
    if key:
        return key
    path = os.environ.get("MEMSEEK_KEY_FILE", "")
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    raise SystemExit("Set MEMSEEK_API_KEY or MEMSEEK_KEY_FILE. The key is not read from the file you are loading.")


def _post(url: str, key: str, records: list[dict]) -> dict:
    payload = json.dumps({"records": records}).encode("utf-8")
    request = urllib.request.Request(
        url.rstrip("/") + "/records",
        data=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"POST /records failed ({exc.code}): {detail}") from exc


def _summary(people: list[dict], notes: list[str], *, owner: str, source: str, dry_run: bool) -> dict:
    return {
        "dry_run": dry_run,
        "owner": owner,
        "source": source,
        "parsed": len(people),
        "skipped_or_header": len(notes),
        "activation": "candidate",
        "visibility": "private",
        "source_claim": "archivist-file",
        "reviewed": False,
        "keys": [person["key"] for person in people],
        "inserted": 0,
        "duplicates": 0,
    }


def record_bodies(people: list[dict], owner: str) -> list[dict]:
    return [
        {
            "entity": owner,
            "collection": "people",
            "collection_version": 2,
            "type": "context",
            "key": person["key"],
            "text": person["text"],
            "content": person["content"],
            "dedupe_key": person["dedupe_key"],
        }
        for person in people
    ]


def load_text(
    text: str,
    *,
    owner: str,
    source: str,
    dry_run: bool,
    batch_size: int,
    pause: float,
    url: str,
) -> dict:
    people, notes = parse_people(text)
    summary = _summary(people, notes, owner=owner, source=source, dry_run=dry_run)
    if dry_run:
        return summary
    key = _api_key()
    inserted = 0
    duplicates = 0
    bodies = record_bodies(people, owner)
    for start in range(0, len(bodies), batch_size):
        chunk = bodies[start : start + batch_size]
        result = _post(url, key, chunk)
        inserted += len(result.get("inserted") or [])
        duplicates += len(result.get("duplicates") or [])
        if start + batch_size < len(bodies) and pause:
            time.sleep(pause)
    summary["inserted"] = inserted
    summary["duplicates"] = duplicates
    return summary


def load(path: Path, *, owner: str, dry_run: bool, batch_size: int, pause: float, url: str) -> dict:
    return load_text(
        path.read_text(encoding="utf-8"),
        owner=owner,
        source=str(path),
        dry_run=dry_run,
        batch_size=batch_size,
        pause=pause,
        url=url,
    )


def load_direct(
    text: str,
    *,
    workspace: str,
    owner: str,
    source: str,
    dry_run: bool,
    batch_size: int,
    pause: float,
) -> dict:
    """Write candidates through the database. Dry-run never opens it.

    The text is the people file itself. Callers streaming over ssh pass stdin
    and this function does not create a file.
    """

    import asyncio

    people, notes = parse_people(text)
    summary = _summary(people, notes, owner=owner, source=source, dry_run=dry_run)
    if dry_run:
        return summary

    from direct_runtime import prepare_engine, workspace_name

    prepare_engine()
    name = workspace_name(workspace)
    bodies = record_bodies(people, owner)

    async def run() -> tuple[int, int]:
        from memseek.config import Settings
        from memseek.db import pool_lifespan
        from memseek.definitions import load_definition_catalog
        from memseek.records import PublicRecordInput, RecordBatchRequest, insert_public_records
        from memseek.workspace_catalog import WorkspaceCatalogRegistry

        settings = Settings()
        inserted = 0
        duplicates = 0
        async with pool_lifespan(settings) as pool:
            registry = WorkspaceCatalogRegistry(pool, settings, load_definition_catalog(settings))
            catalog = await registry.get(name)
            for start in range(0, len(bodies), batch_size):
                chunk = bodies[start : start + batch_size]
                request = RecordBatchRequest(
                    records=tuple(PublicRecordInput.model_validate(body) for body in chunk)
                )
                result = await insert_public_records(
                    pool,
                    workspace=name,
                    request=request,
                    catalog=catalog,
                    settings=settings,
                )
                inserted += len(result.inserted)
                duplicates += len(result.duplicates)
                if start + batch_size < len(bodies) and pause:
                    await asyncio.sleep(pause)
        return inserted, duplicates

    inserted, duplicates = asyncio.run(run())
    summary["inserted"] = inserted
    summary["duplicates"] = duplicates
    return summary


def read_source(path: str | None) -> tuple[str, str]:
    """Return ``(text, label)``. ``-`` or an omitted path reads stdin and writes no file."""

    if path in (None, "-"):
        return sys.stdin.read(), "stdin"
    return Path(path).read_text(encoding="utf-8"), path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        default=None,
        help="people file, or - / omit to read stdin (never written to disk)",
    )
    parser.add_argument("--owner", required=True, help="entity the candidates belong to")
    parser.add_argument("--dry-run", action="store_true", help="parse and report; do not write")
    parser.add_argument("--direct", action="store_true", help="use DATABASE_URL, no API key")
    parser.add_argument("--workspace", default=os.environ.get("MEMSEEK_WORKSPACE", "personal"))
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--pause", type=float, default=3.0, help="seconds between batches, so the worker is not flooded")
    parser.add_argument("--url", default=os.environ.get("MEMSEEK_URL", "http://127.0.0.1:8000"))
    args = parser.parse_args(argv)
    if not 1 <= args.batch_size <= 100:
        parser.error("--batch-size must be from 1 to 100")
    if args.pause < 0:
        parser.error("--pause must be >= 0")
    text, source = read_source(args.path)
    if args.direct:
        summary = load_direct(
            text,
            workspace=args.workspace,
            owner=args.owner,
            source=source,
            dry_run=args.dry_run,
            batch_size=args.batch_size,
            pause=args.pause,
        )
    else:
        summary = load_text(
            text,
            owner=args.owner,
            source=source,
            dry_run=args.dry_run,
            batch_size=args.batch_size,
            pause=args.pause,
            url=args.url,
        )
    print(json.dumps({key: value for key, value in summary.items() if key != "keys"}, indent=2))
    print(f"keys: {len(summary['keys'])}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
