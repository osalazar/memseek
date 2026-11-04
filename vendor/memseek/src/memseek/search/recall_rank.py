"""Ordering for task-relevant rules, notes, and people.

These functions are pure so the ranking can be tested without a database.
SQL retrieval only supplies rows; it does not decide who outranks whom.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from memseek.search.names import (
    lexical_overlap,
    name_forms,
    name_identity,
    name_match,
    name_score,
    query_token_hits,
)

# Cosine similarity on text-embedding-3-small. Below this, a vector neighbor
# is not evidence the row is about the task. Keyword and name hits do not
# need to clear it. People search does not use this floor: hub vectors clear
# it, and a higher bar, against queries that do not name them.
SEMANTIC_ACCEPT = 0.28
# Reviewed people may match on cosine only above this, and only when the hit
# also stands clear of the rest of the corpus. Unreviewed candidates never
# match on cosine alone.
PEOPLE_VECTOR_MIN = 0.90
# Absolute gap over the mean cosine of the visible people for this query.
PEOPLE_VECTOR_MARGIN = 0.15
# Gap over the next-best cosine. A pack of hub vectors sits together at the
# top and fails this, so none of them are returned.
PEOPLE_VECTOR_GAP = 0.08
PEOPLE_PAGE = 5
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


def _owner_reviewed(content: Mapping[str, Any]) -> bool:
    """Owner-reviewed people outrank archivist candidates when the match ties.

    A missing ``reviewed`` flag follows activation: confirmed is reviewed, and
    an archivist candidate is stored with ``reviewed: false``.
    """

    reviewed = content.get("reviewed")
    if reviewed is True:
        return True
    if reviewed is False:
        return False
    return content.get("activation") == "confirmed"


def _searchable_person(content: Mapping[str, Any], now: int, visibility: set[str]) -> bool:
    """Owner-visible people: confirmed rows, plus private archivist candidates.

    Other candidates stay hidden. Shareable scope never includes a private
    candidate, and a row still has to clear the caller's visibility list.
    """

    if content.get("tombstone") is True:
        return False
    if not _visibility_ok(content, visibility):
        return False
    until = content.get("valid_until")
    if not (isinstance(until, int) and not isinstance(until, bool) and until > now):
        return False
    activation = content.get("activation")
    if activation == "confirmed":
        return True
    return activation == "candidate" and content.get("source") == "archivist-file"


def label_unreviewed_text(content: Mapping[str, Any]) -> str:
    """Text as rendered. Archivist candidates always carry an ``(unreviewed)`` marker."""

    text = str(content.get("text") or "")
    if content.get("source") == "archivist-file" and "(unreviewed)" not in text.casefold():
        return "(unreviewed) " + text
    return text


def label_unreviewed_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Copy rows so rendered people text shows the unreviewed marker."""

    labelled: list[dict[str, Any]] = []
    for row in rows:
        content = row.get("content")
        if not isinstance(content, dict):
            labelled.append(dict(row))
            continue
        text = label_unreviewed_text(content)
        if text == content.get("text"):
            labelled.append(dict(row))
            continue
        copied = dict(row)
        copied["content"] = {**content, "text": text}
        labelled.append(copied)
    return labelled


def _visible_people(
    rows: Sequence[Mapping[str, Any]], *, now: int, visibility: set[str]
) -> list[Mapping[str, Any]]:
    return [
        row
        for row in rows
        if row.get("collection") == "people"
        and _searchable_person(row.get("content") or {}, now, visibility)
    ]


def _similarity(row: Mapping[str, Any]) -> float | None:
    raw = row.get("similarity")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def _corpus_similarity(rows: Sequence[Mapping[str, Any]]) -> tuple[float, float]:
    """Mean and population standard deviation of cosines for this query."""

    values = [value for row in rows if (value := _similarity(row)) is not None]
    if not values:
        return 0.0, 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return mean, math.sqrt(variance)


def _next_similarity(rows: Sequence[Mapping[str, Any]], row_id: str) -> float | None:
    """Highest cosine in the corpus other than ``row_id``."""

    best: float | None = None
    for row in rows:
        if str(row.get("id")) == row_id:
            continue
        value = _similarity(row)
        if value is None:
            continue
        if best is None or value > best:
            best = value
    return best


_NAME_TIER = {"exact": 3, "all": 2, "fallback": 1}
_NAME_SCORE = {"exact": 1.0, "all": 0.95, "fallback": 0.85}


def _people_reason(
    row: Mapping[str, Any],
    query: str,
    *,
    mean: float,
    others: float | None,
    use_fallback: bool,
) -> tuple[bool, float, str]:
    """Why a visible person is kept or dropped.

    A full name match outranks every token matching, which outranks a
    one-token miss. That miss is used only when nobody matched in full.
    Unreviewed candidates still need a lexical name. A reviewed vector-only
    hit needs cosine at least ``PEOPLE_VECTOR_MIN``, a margin over the corpus
    mean, and a gap over the next cosine so a hub cluster is dropped.
    """

    content = row.get("content") or {}
    kind = name_match(query, content)
    if kind in {"exact", "all"} or (kind == "fallback" and use_fallback):
        return True, _NAME_SCORE[kind], kind
    named = name_score(query, content)
    if not _owner_reviewed(content):
        return False, named, "rejected-unreviewed"
    similarity = _similarity(row)
    if similarity is None or similarity < PEOPLE_VECTOR_MIN:
        return False, named, "rejected-vector"
    if similarity < mean + PEOPLE_VECTOR_MARGIN:
        return False, named, "rejected-margin"
    if others is not None and similarity - others < PEOPLE_VECTOR_GAP:
        return False, named, "rejected-gap"
    return True, named, "vector"


def _hides_as_duplicate(content: Mapping[str, Any], reviewed_keys: set[str]) -> bool:
    """An archivist candidate whose name or alias copies a reviewed person.

    Reviewed rows stay. The comparison is the folded full form, so a shared
    given name alone does not hide anyone. Nothing is deleted.
    """

    if _owner_reviewed(content) or content.get("source") != "archivist-file":
        return False
    return bool(name_identity(content) & reviewed_keys)


def _people_decisions(
    visible: Sequence[Mapping[str, Any]],
    query: str,
) -> list[tuple[Mapping[str, Any], bool, float, str]]:
    """Classify visible people. Fallback applies only when nothing fully matches."""

    reviewed_keys: set[str] = set()
    for row in visible:
        content = row.get("content") or {}
        if _owner_reviewed(content):
            reviewed_keys.update(name_identity(content))
    kinds = [name_match(query, row.get("content") or {}) for row in visible]
    token_hits = [query_token_hits(query, row.get("content") or {}) for row in visible]
    # An exact token wins. Prefix and fuzzy hits for that token are dropped,
    # so Barba does not also return Bárbara or Barra. A single-token fuzzy
    # hit is kept only when nobody matched the token exactly.
    exact_tokens = {
        index
        for hits in token_hits
        for index, hit in enumerate(hits)
        if hit == "exact"
    }
    use_fallback = not any(kind in {"exact", "all"} for kind in kinds)
    mean, _stdev = _corpus_similarity(visible)
    decisions: list[tuple[Mapping[str, Any], bool, float, str]] = []
    for row, _kind, hits in zip(visible, kinds, token_hits, strict=True):
        keep, score, reason = _people_reason(
            row,
            query,
            mean=mean,
            others=_next_similarity(visible, str(row.get("id"))),
            use_fallback=use_fallback,
        )
        if keep and reason in {"exact", "all", "fallback"} and any(
            index in exact_tokens and hit in {"prefix", "fuzzy"} for index, hit in enumerate(hits)
        ):
            keep = False
            reason = "rejected-inexact"
        content = row.get("content") or {}
        if keep and _hides_as_duplicate(content, reviewed_keys):
            keep = False
            reason = "duplicate"
        decisions.append((row, keep, score, reason))
    return decisions


def select_people(
    rows: Sequence[Mapping[str, Any]],
    *,
    query: str,
    now: int,
    visibility: set[str],
    offset: int,
    limit: int,
) -> RecallPage:
    """Owner people, including unreviewed archivist candidates.

    A full-name match ranks above every token matching, which ranks above a
    one-token miss. On equal strength an owner-reviewed person ranks above a
    candidate. Unreviewed candidates match on the name only. A reviewed
    vector hit must clear 0.90 and stand away from the corpus, so hub
    vectors and a nonsense query return nobody. The page is at most
    ``PEOPLE_PAGE`` hits; ``offset`` walks the rest.
    """

    visible = _visible_people(rows, now=now, visibility=visibility)
    scored: list[tuple[Mapping[str, Any], float, str]] = []
    for row, keep, named, reason in _people_decisions(visible, query):
        if keep:
            scored.append((row, named, reason))
    scored.sort(
        key=lambda item: (
            -_NAME_TIER.get(item[2], 0),
            -(1 if _owner_reviewed(item[0].get("content") or {}) else 0),
            -item[1],
            -relevance(item[0], query),
            -_seq(item[0]),
            str(item[0]["id"]),
        )
    )
    page_limit = min(limit, PEOPLE_PAGE) if limit else PEOPLE_PAGE
    return _page([row for row, _score, _reason in scored], offset, page_limit, ())


def people_debug(
    rows: Sequence[Mapping[str, Any]],
    *,
    query: str,
    now: int,
    visibility: set[str],
    top: int = 8,
) -> dict[str, Any]:
    """Top people cosines for an owner debug check. No biography text."""

    visible = _visible_people(rows, now=now, visibility=visibility)
    mean, stdev = _corpus_similarity(visible)
    explained: list[dict[str, Any]] = []
    for row, keep, named, reason in _people_decisions(visible, query):
        content = row.get("content") or {}
        forms = name_forms(content)
        similarity = _similarity(row)
        z_score = None
        if similarity is not None and stdev > 1e-9:
            z_score = round((similarity - mean) / stdev, 3)
        explained.append(
            {
                "id": str(row.get("id")),
                "name": (forms[0] if forms else "")[:80],
                "similarity": None if similarity is None else round(similarity, 4),
                "z": z_score,
                "name_score": round(named, 4),
                "reviewed": _owner_reviewed(content),
                "kept": keep,
                "reason": reason,
            }
        )
    explained.sort(
        key=lambda item: (
            -(item["similarity"] if isinstance(item["similarity"], float) else -1.0),
            item["id"],
        )
    )
    return {
        "query": query[:120],
        "count": len(visible),
        "mean": round(mean, 4),
        "stdev": round(stdev, 4),
        "vector_min": PEOPLE_VECTOR_MIN,
        "margin": PEOPLE_VECTOR_MARGIN,
        "gap": PEOPLE_VECTOR_GAP,
        "top": explained[:top],
    }
