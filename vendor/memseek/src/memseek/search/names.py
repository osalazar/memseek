"""Accent-insensitive name matching for people records.

A match can come from an explicit name, an alias, a ``Name:`` / ``Aliases:``
header inside the text, or the leading clause of a prose note. Titles and
function words do not count as names. Comparison folds case and accents, so
``Alcivar`` matches ``Alcívar``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping

_TITLES = frozenset(
    {
        "dra",
        "dr",
        "sr",
        "sra",
        "srta",
        "don",
        "dona",
        "doña",
        "ing",
        "lic",
        "mr",
        "mrs",
        "ms",
        "miss",
        "prof",
        "doctor",
        "doctora",
        "profesor",
        "profesora",
    }
)
_STOP = frozenset(
    {
        "the",
        "who",
        "is",
        "my",
        "a",
        "an",
        "of",
        "for",
        "and",
        "to",
        "de",
        "la",
        "el",
        "y",
        "del",
        "los",
        "las",
        "un",
        "una",
        "con",
        "por",
        "que",
        "en",
        "lo",
        "me",
        "mi",
        "su",
        "es",
        "was",
        "are",
        "be",
        "or",
        "on",
        "in",
        "at",
        "from",
        "this",
        "that",
        "with",
        "your",
        "you",
        "about",
        "please",
        "find",
        "search",
    }
)
_NAME_LINE = re.compile(r"(?im)^name:\s*(.+?)\s*$")
_ALIAS_LINE = re.compile(r"(?im)^aliases:\s*(.+?)\s*$")

# A name hit at or above this outranks a purely semantic hit.
NAME_ACCEPT = 0.80


def fold_name(value: str) -> str:
    """Lowercase, strip accents and punctuation, and collapse whitespace."""

    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = re.sub(r"[^a-z0-9]+", " ", stripped.casefold())
    return " ".join(cleaned.split())


def levenshtein(left: str, right: str) -> int:
    """Edit distance, short-circuiting when the strings are far apart."""

    if left == right:
        return 0
    if abs(len(left) - len(right)) > 2:
        return 3
    previous = list(range(len(right) + 1))
    for index, left_char in enumerate(left, start=1):
        current = [index]
        for right_index, right_char in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def _query_tokens(query: str) -> list[str]:
    return [
        token
        for token in fold_name(query).split()
        if token not in _TITLES and token not in _STOP and len(token) >= 3
    ]


def _fuzzy_token(query: str, candidate: str) -> bool:
    if query == candidate:
        return True
    if len(query) < 4 or len(candidate) < 4:
        return False
    distance = levenshtein(query, candidate)
    if len(query) >= 8:
        return distance <= 2
    return distance <= 1


def name_forms(content: Mapping[str, object]) -> list[str]:
    """Name strings a people record can be matched against, in priority order."""

    forms: list[str] = []
    name = content.get("name")
    if isinstance(name, str) and name.strip():
        forms.append(name.strip())
    aliases = content.get("aliases")
    if isinstance(aliases, list):
        forms.extend(
            item.strip() for item in aliases if isinstance(item, str) and item.strip()
        )
    text = content.get("text")
    if isinstance(text, str) and text.strip():
        for match in _NAME_LINE.finditer(text):
            forms.append(match.group(1).strip())
        for match in _ALIAS_LINE.finditer(text):
            forms.extend(
                part.strip() for part in re.split(r"[,;/]", match.group(1)) if part.strip()
            )
        if not forms:
            head = text.strip().splitlines()[0]
            folded_head = head.casefold()
            marker = folded_head.find(" is ")
            if marker > 0:
                head = head[:marker]
            forms.append(head[:160])
    seen: set[str] = set()
    unique: list[str] = []
    for form in forms:
        key = fold_name(form)
        if key and key not in seen:
            seen.add(key)
            unique.append(form)
    return unique


def name_score(query: str, content: Mapping[str, object]) -> float:
    """Score how well ``query`` names this record. 1.0 is exact, 0 is none.

    Token and fuzzy matches stay at or above ``NAME_ACCEPT`` so they sort
    ahead of semantic hits. A query with no usable name token scores 0.
    """

    query_tokens = _query_tokens(query)
    if not query_tokens:
        return 0.0
    folded_query = " ".join(query_tokens)
    best = 0.0
    for form in name_forms(content):
        folded = fold_name(form)
        if not folded:
            continue
        if folded == folded_query or folded == fold_name(query):
            return 1.0
        name_tokens = [
            token for token in folded.split() if token not in _TITLES and token not in _STOP
        ]
        if not name_tokens:
            continue
        if all(token in name_tokens for token in query_tokens):
            best = max(best, 0.95)
            continue
        exact = [token for token in query_tokens if token in name_tokens and len(token) >= 4]
        if exact:
            coverage = len(exact) / len(query_tokens)
            best = max(best, 0.84 + 0.08 * coverage)
            continue
        fuzzy = [
            token
            for token in query_tokens
            if any(_fuzzy_token(token, candidate) for candidate in name_tokens)
        ]
        if fuzzy and len(fuzzy) == len(query_tokens):
            best = max(best, 0.82)
        elif fuzzy:
            best = max(best, NAME_ACCEPT)
    return best


def lexical_overlap(query: str, text: str) -> float:
    """Share of distinctive query tokens that occur in ``text``, accent-folded.

    One hit is enough to count as a keyword match. Short tokens and function
    words are ignored so a generic task does not match every record.
    """

    tokens = _query_tokens(query)
    distinctive = [token for token in tokens if len(token) >= 4]
    if not distinctive:
        return 0.0
    haystack = fold_name(text)
    hits = sum(1 for token in distinctive if token in haystack)
    return hits / len(distinctive)
