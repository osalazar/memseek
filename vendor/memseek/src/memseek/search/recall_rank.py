"""Ordering for task-relevant rules, notes, and people.

These functions are pure so the ranking can be tested without a database.
SQL retrieval only supplies rows; it does not decide who outranks whom.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from memseek.search.names import NAME_ACCEPT, lexical_overlap, name_score

# Cosine similarity on text-embedding-3-small. Below this, a vector neighbor
# is not evidence the row is about the task. Keyword and name hits do not
# need to clear it.
SEMANTIC_ACCEPT = 0.28
# Always included, ahead of task matches: the highest-priority hard rules.
CORE_SIZE = 12
# When an owner has at most this many visible notes, matching returns them
# instead of an empty block. Above it, only notes that match the task.
SMALL_NOTE_SET = 6


@dataclass(frozen=True, slots=True)
class RecallPage:
    ids: tuple[str, ...]
    total: int
    offset: int
    has_more: bool
    next_offset: int | None
    core_ids: tuple[str, ...] = ()

    def as_meta(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "offset": self.offset,
            "has_more": self.has_more,
            "next_offset": self.next_offset,
            "core": len(self.core_ids),
        }


def _page(ordered: Sequence[Mapping[str, Any]], offset: int, limit: int, core_ids: Sequence[str]) -> RecallPage:
    offset = max(0, offset)
    limit = max(1, limit)
    window = list(ordered[offset : offset + limit])
    has_more = offset + len(window) < len(ordered)
    return RecallPage(
        ids=tuple(str(row["id"]) for row in window),
        total=len(ordered),
        offset=offset,
        has_more=has_more,
        next_offset=(offset + len(window)) if has_more else None,
        core_ids=tuple(core_ids),
    )


def _confirmed(content: Mapping[str, Any], now: int) -> bool:
    if content.get("tombstone") is True:
        return False
    if content.get("activation") != "confirmed":
        return False
    until = content.get("valid_until")
    return isinstance(until, int) and not isinstance(until, bool) and until > now


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def relevance(row: Mapping[str, Any], query: str) -> float:
    """Hybrid score in ``[0, 1]`` from the vector neighbor and the words."""

    semantic = _number(row.get("similarity"))
    lexical = lexical_overlap(query, str((row.get("content") or {}).get("text") or ""))
    rank = _number(row.get("text_match"))
    if rank > 0:
        lexical = max(lexical, min(1.0, rank))
    return max(0.0, min(1.0, max(semantic, lexical)))


def _lexical_accept(query: str, text: str) -> bool:
    """A keyword hit that is specific enough to be about the task.

    One shared word of a long task is not enough: ``contact`` appears in many
    rules. A single-token query still matches, and two distinctive tokens or
    about a third of them do too.
    """

    from memseek.search.names import _query_tokens, fold_name

    tokens = [token for token in _query_tokens(query) if len(token) >= 4]
    if not tokens:
        return False
    haystack = fold_name(text)
    hits = sum(1 for token in tokens if token in haystack)
    if hits == 0:
        return False
    if len(tokens) == 1:
        return True
    return hits >= 2 or hits / len(tokens) >= 0.34


def _matches_task(row: Mapping[str, Any], query: str) -> bool:
    if _number(row.get("similarity")) >= SEMANTIC_ACCEPT:
        return True
    text = str((row.get("content") or {}).get("text") or "")
    return _lexical_accept(query, text)


def _priority(row: Mapping[str, Any]) -> int:
    value = (row.get("content") or {}).get("priority")
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _seq(row: Mapping[str, Any]) -> int:
    return int(row.get("seq") or 0)


def select_rules(
    rows: Sequence[Mapping[str, Any]],
    *,
    query: str,
    now: int,
    offset: int,
    limit: int,
    core_size: int = CORE_SIZE,
) -> RecallPage:
    """Always-on core of hard rules, then task matches, newest first on ties.

    The core is the highest-priority confirmed hard rules (newest seq wins a
    priority tie) and is not dropped when the task does not mention them.
    Everything after it had to match the task. Rows already in the core are
    not repeated.
    """

    hard = [
        row
        for row in rows
        if row.get("collection") == "preferences"
        and (row.get("content") or {}).get("strength") == "hard"
        and _confirmed(row.get("content") or {}, now)
    ]
    core_sorted = sorted(hard, key=lambda row: (-_priority(row), -_seq(row), str(row["id"])))
    core = core_sorted[:core_size]
    core_ids = {str(row["id"]) for row in core}
    matched = [
        row
        for row in hard
        if str(row["id"]) not in core_ids and _matches_task(row, query)
    ]
    matched.sort(
        key=lambda row: (-relevance(row, query), -_seq(row), str(row["id"]))
    )
    return _page([*core, *matched], offset, limit, [str(row["id"]) for row in core])


def _visibility_ok(content: Mapping[str, Any], visibility: set[str]) -> bool:
    value = content.get("visibility")
    return isinstance(value, str) and value in visibility


def select_context(
    rows: Sequence[Mapping[str, Any]],
    *,
    query: str,
    now: int,
    visibility: set[str],
    offset: int,
    limit: int,
) -> RecallPage:
    """Task-matching life notes and rules the caller is allowed to see.

    Preferences have no visibility of their own; they are included only when
    they match the task, for this entity. Private notes stay out unless
    ``visibility`` contains ``private``. A shareable-only caller therefore
    still sees shareable notes and relevant rules, and nothing private.

    When the caller can see only a handful of notes, those notes are kept
    even if the task words miss them. Past that, a miss is not filled with
    unrelated notes.
    """

    notes = [
        row
        for row in rows
        if row.get("collection") == "life_context"
        and _confirmed(row.get("content") or {}, now)
        and _visibility_ok(row.get("content") or {}, visibility)
    ]
    rules = [
        row
        for row in rows
        if row.get("collection") == "preferences"
        and _confirmed(row.get("content") or {}, now)
        and _matches_task(row, query)
    ]
    keep_all_notes = len(notes) <= min(limit, SMALL_NOTE_SET)
    chosen_notes = notes if keep_all_notes else [row for row in notes if _matches_task(row, query)]
    chosen = [*chosen_notes, *rules]
    chosen.sort(key=lambda row: (-relevance(row, query), -_seq(row), str(row["id"])))
    # Stable de-dupe in case a row was supplied twice.
    seen: set[str] = set()
    ordered = []
    for row in chosen:
        identity = str(row["id"])
        if identity in seen:
            continue
        seen.add(identity)
        ordered.append(row)
    return _page(ordered, offset, limit, ())


def select_people(
    rows: Sequence[Mapping[str, Any]],
    *,
    query: str,
    now: int,
    visibility: set[str],
    offset: int,
    limit: int,
) -> RecallPage:
    """Confirmed people, name hits first, nothing when nothing matches.

    Exact, alias, and fuzzy accent-folded name matches sort above semantic
    and keyword matches. A query that names nobody and does not clear the
    semantic or keyword bar returns an empty page.
    """

    visible = [
        row
        for row in rows
        if row.get("collection") == "people"
        and _confirmed(row.get("content") or {}, now)
        and _visibility_ok(row.get("content") or {}, visibility)
    ]
    scored: list[tuple[Mapping[str, Any], float]] = []
    for row in visible:
        content = row.get("content") or {}
        named = name_score(query, content)
        if named >= NAME_ACCEPT or _matches_task(row, query):
            scored.append((row, named))
    scored.sort(
        key=lambda item: (
            -(1 if item[1] >= NAME_ACCEPT else 0),
            -item[1],
            -relevance(item[0], query),
            -_seq(item[0]),
            str(item[0]["id"]),
        )
    )
    return _page([row for row, _score in scored], offset, limit, ())
