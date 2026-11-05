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
import re
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
    folded = text.casefold()
    if folded.startswith("#") or folded.startswith("lock") or folded.startswith("keep"):
        return True
    if folded.startswith("ops:") or folded.startswith("house:"):
        return True
    return False


def is_header(line: str) -> bool:
    first = line.split("|", 1)[0].strip().casefold()
    return first == "name"


def _parentheticals(value: str) -> list[str]:
    found: list[str] = []
    start = 0
    while True:
        open_at = value.find("(", start)
        if open_at < 0:
            break
        close_at = value.find(")", open_at + 1)
        if close_at < 0:
            break
        inner = value[open_at + 1 : close_at].strip()
        if inner:
            found.append(inner)
        start = close_at + 1
    return found


def _aliases(name: str, reach: str) -> list[str]:
    """Nicknames in the name, or an explicit alias written in the reach field.

    PRINT and the other trailing columns are not aliases.
    """

    found = _parentheticals(name)
    found.extend(_parentheticals(reach))
    for match in re.finditer(r"(?i)\b(?:aka|alias)\s*:?\s*([^,;|]+)", reach):
        alias = match.group(1).strip()
        if alias:
            found.append(alias)
    seen: set[str] = set()
    unique: list[str] = []
    for alias in found:
        key = fold(alias)
        if key and key not in seen:
            seen.add(key)
            unique.append(alias)
    return unique


def reach_text(reach: str) -> str:
    """HOLE and ``reach HOLE`` are unknown. Anything else is kept."""

    folded = fold(reach)
    if folded in {"", "hole", "reach hole"}:
        return "reach unknown"
    return reach.strip()


def parse_people(text: str) -> tuple[list[dict], list[str]]:
    """Return record bodies and a list of skip reasons. Raises on a bad line."""

    people: list[dict] = []
    notes: list[str] = []
    seen_keys: dict[str, str] = {}
    for line_no, raw in enumerate(text.splitlines(), start=1):
        if skipped(raw):
            notes.append(f"{line_no}: skipped")
            continue
        if is_header(raw):
            notes.append(f"{line_no}: header")
            continue
        parts = [part.strip() for part in raw.split("|")]
        # Prose notes in the archivist file have no Name|reach|role columns.
        if len(parts) < 3:
            notes.append(f"{line_no}: prose")
            continue
        name, reach, role = parts[0], parts[1], parts[2]
        if not name:
            raise ValueError(f"line {line_no}: name is empty")
        # name|reach|role|P#|LANG|PRINT...  P# may be an em dash.
        priority = parts[3] if len(parts) > 3 else ""
        language = parts[4] if len(parts) > 4 else ""
        printed = [part for part in parts[5:] if part]
        aliases = _aliases(name, reach)
        identity = "|".join((fold(name), fold(role), fold(reach)))
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        key = f"archivist-{digest}"
        signature = "|".join((name, reach, role, priority, language, ",".join(printed)))
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
            printed=printed,
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
    printed: list[str],
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
    lines.append(f"Reach: {reach_text(reach)}")
    if role:
        lines.append(f"Role: {role}")
    if priority:
        lines.append(f"Priority: {priority}")
    if language:
        lines.append(f"Language: {language}")
    if printed:
        lines.append("Print: " + " | ".join(printed))
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
        "skipped_prose": sum(1 for note in notes if note.endswith(": prose")),
        "activation": "candidate",
        "visibility": "private",
        "source_claim": "archivist-file",
        "reviewed": False,
        "keys": [person["key"] for person in people],
        "inserted": 0,
        "duplicates": 0,
        "skipped_reviewed": 0,
    }


def omit_reviewed_copies(people: list[dict], reviewed_keys: set[str]) -> tuple[list[dict], int]:
    """Drop candidates whose name or alias copies a reviewed person.

    ``reviewed_keys`` are folded full forms from people already on file.
    The caller does not delete those rows.
    """

    if not reviewed_keys:
        return list(people), 0
    from memseek.search.names import name_identity

    kept: list[dict] = []
    skipped = 0
    for person in people:
        content = {
            "name": person.get("name") or "",
            "aliases": person.get("aliases") or [],
            "text": person.get("text") or "",
        }
        if name_identity(content) & reviewed_keys:
            skipped += 1
            continue
        kept.append(person)
    return kept, skipped


def plain_name(name: str) -> str:
    """Folded name with parenthetical nicknames removed."""

    return fold(re.sub(r"\([^)]*\)", " ", name))


def name_from_content(content: dict) -> str:
    text = str(content.get("text") or "")
    for line in text.splitlines():
        if line.casefold().startswith("name:"):
            return line.split(":", 1)[1].strip()
    name = content.get("name")
    return name.strip() if isinstance(name, str) else ""


def plan_remap(people: list[dict], rows: list[dict]) -> dict:
    """Match file people to archivist rows by folded name. Nothing is deleted.

    ``not_found`` is the count of stored archivist rows whose name is not in
    the file, including junk header lines such as ``OPS:``. ``missing`` is
    file people with no stored row.
    """

    by_name: dict[str, dict] = {}
    for row in rows:
        content = row.get("content") or {}
        if content.get("source") != "archivist-file":
            continue
        folded = plain_name(name_from_content(content))
        if folded:
            by_name.setdefault(folded, row)
    file_names: set[str] = set()
    updated = unchanged = missing = 0
    changes: list[tuple[dict, dict]] = []
    for person in people:
        folded = plain_name(person["name"])
        file_names.add(folded)
        row = by_name.get(folded)
        if row is None:
            missing += 1
            continue
        if person["text"] == str((row.get("content") or {}).get("text") or ""):
            unchanged += 1
        else:
            updated += 1
            changes.append((person, row))
    not_found = sorted(name for name in by_name if name not in file_names)
    return {
        "updated": updated,
        "unchanged": unchanged,
        "not_found": len(not_found),
        "missing": missing,
        "not_found_names": not_found,
        "changes": changes,
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
    """Write candidates through the database. Dry-run reads names and does not write.

    The text is the people file itself. Callers streaming over ssh pass stdin
    and this function does not create a file. A candidate whose name or alias
    copies a reviewed person for this owner is skipped.
    """

    import asyncio

    people, notes = parse_people(text)
    summary = _summary(people, notes, owner=owner, source=source, dry_run=dry_run)

    from direct_runtime import prepare_engine, workspace_name

    prepare_engine()
    name = workspace_name(workspace)

    async def run() -> tuple[int, int]:
        from check_people_duplicates import reviewed_name_keys
        from memseek.config import Settings
        from memseek.db import pool_lifespan
        from memseek.definitions import load_definition_catalog
        from memseek.records import PublicRecordInput, RecordBatchRequest, insert_public_records
        from memseek.workspace_catalog import WorkspaceCatalogRegistry

        settings = Settings()
        inserted = 0
        duplicates = 0
        async with pool_lifespan(settings) as pool:
            reviewed = await reviewed_name_keys(pool, workspace=name, owner=owner)
            kept, skipped = omit_reviewed_copies(people, reviewed)
            summary["skipped_reviewed"] = skipped
            if dry_run:
                return 0, 0
            registry = WorkspaceCatalogRegistry(pool, settings, load_definition_catalog(settings))
            catalog = await registry.get(name)
            bodies = record_bodies(kept, owner)
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


def remap_direct(
    text: str,
    *,
    workspace: str,
    owner: str,
    source: str,
    dry_run: bool,
    batch_size: int,
    pause: float,
) -> dict:
    """Rewrite archivist people@2 rows from the file. people@1 is not touched.

    Rows are matched by the folded name. Reach, role, priority, language,
    print and aliases are replaced. reviewed stays false, visibility stays
    private, and source stays archivist-file. A new current version is what
    the worker re-embeds. Dry-run only counts.
    """

    import asyncio

    people, notes = parse_people(text)
    summary = _summary(people, notes, owner=owner, source=source, dry_run=dry_run)
    summary["remap"] = True

    from direct_runtime import prepare_engine, workspace_name

    prepare_engine()
    name = workspace_name(workspace)

    async def run() -> dict:
        from memseek.config import Settings
        from memseek.db import pool_lifespan
        from memseek.definitions import load_definition_catalog
        from memseek.records import PublicRecordInput, RecordBatchRequest, insert_public_records
        from memseek.workspace_catalog import WorkspaceCatalogRegistry

        settings = Settings()
        async with pool_lifespan(settings) as pool:
            async with pool.connection() as conn:
                result = await conn.execute(
                    """
                    select row.key, row.content
                    from record row
                    where row.workspace = %s
                      and row.entity = %s
                      and row.collection = 'people'
                      and row.status = 'active'
                      and row.content->>'source' = 'archivist-file'
                      and not coalesce((row.content->>'tombstone')::boolean, false)
                      and (
                        row.key is null or not exists (
                          select 1 from record newer
                          where newer.workspace = row.workspace
                            and newer.entity = row.entity
                            and newer.collection = row.collection
                            and newer.key = row.key
                            and newer.status = row.status
                            and newer.seq > row.seq
                        )
                      )
                    """,
                    [name, owner],
                )
                loaded = await result.fetchall()
            rows = [
                {"key": item["key"], "content": item["content"] if isinstance(item["content"], dict) else {}}
                for item in loaded
            ]
            plan = plan_remap(people, rows)
            summary["updated"] = plan["updated"]
            summary["unchanged"] = plan["unchanged"]
            summary["not_found"] = plan["not_found"]
            summary["missing"] = plan["missing"]
            summary["not_found_names"] = plan["not_found_names"]
            if dry_run or not plan["changes"]:
                return summary
            registry = WorkspaceCatalogRegistry(pool, settings, load_definition_catalog(settings))
            catalog = await registry.get(name)
            bodies = []
            for person, row in plan["changes"]:
                revised = dict(person)
                revised["key"] = row["key"]
                canonical = json.dumps(
                    revised["content"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                revised["dedupe_key"] = (
                    "archivist:"
                    + hashlib.sha256(f"{revised['key']}\n{canonical}".encode("utf-8")).hexdigest()
                )
                bodies.extend(record_bodies([revised], owner))
            for start in range(0, len(bodies), batch_size):
                chunk = bodies[start : start + batch_size]
                request = RecordBatchRequest(
                    records=tuple(PublicRecordInput.model_validate(body) for body in chunk)
                )
                await insert_public_records(
                    pool,
                    workspace=name,
                    request=request,
                    catalog=catalog,
                    settings=settings,
                )
                if start + batch_size < len(bodies) and pause:
                    await asyncio.sleep(pause)
        return summary

    return asyncio.run(run())


def read_source(path: str | None) -> tuple[str, str]:
    """Return ``(text, label)``. ``-`` or an omitted path reads stdin to EOF.

    A terminal is refused. ``railway ssh`` allocates a tty when nothing is
    piped, and reading that tty waits forever. A real pipe ends at EOF and
    is never written to a file in the container.
    """

    if path in (None, "-"):
        if getattr(sys.stdin, "isatty", lambda: False)():
            raise SystemExit(
                "Refusing to read people from a terminal. "
                "Pipe the file on stdin so the read stops at EOF."
            )
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
    parser.add_argument(
        "--remap",
        action="store_true",
        help="update existing archivist people@2 rows from this file; requires --direct",
    )
    parser.add_argument("--workspace", default=os.environ.get("MEMSEEK_WORKSPACE", "personal"))
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--pause", type=float, default=3.0, help="seconds between batches, so the worker is not flooded")
    parser.add_argument("--url", default=os.environ.get("MEMSEEK_URL", "http://127.0.0.1:8000"))
    args = parser.parse_args(argv)
    if not 1 <= args.batch_size <= 100:
        parser.error("--batch-size must be from 1 to 100")
    if args.pause < 0:
        parser.error("--pause must be >= 0")
    if args.remap and not args.direct:
        parser.error("--remap requires --direct")
    text, source = read_source(args.path)
    if args.remap:
        summary = remap_direct(
            text,
            workspace=args.workspace,
            owner=args.owner,
            source=source,
            dry_run=args.dry_run,
            batch_size=args.batch_size,
            pause=args.pause,
        )
    elif args.direct:
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
