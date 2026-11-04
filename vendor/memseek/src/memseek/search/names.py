"""Accent-insensitive name matching for people records.

Tokens come from the person's name and from real aliases. An alias phrase
that starts with ``not``, ``no``, ``ex``, or ``not other`` is a negative and
is ignored. Notes and biography text are not names. A prose record with no
structured name still yields the clause before ``is`` (``Alex A is a coach``).
Titles and function words do not count. Comparison folds case and accents,
so ``Alcivar`` matches ``Alcívar``.
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
        "not",
        "no",
        "person",
        "people",
    }
)
# Alias phrases that begin with one of these are negatives ("not Chloe"),
# not other names for the person.
_NEGATIVE_ALIAS = frozenset({"not", "no", "ex"})
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
    """Tokens for keyword overlap. Name search uses :func:`_name_tokens`."""

    return [
        token
        for token in fold_name(query).split()
        if token not in _TITLES and token not in _STOP and len(token) >= 3
    ]


def _name_tokens(value: str) -> list[str]:
    """Significant name tokens. Stop words and one-character tokens are out."""

    return [
        token
        for token in fold_name(value).split()
        if token not in _TITLES and token not in _STOP and len(token) >= 2
    ]


def _fuzzy_token(query: str, candidate: str) -> bool:
    """One substitution on a token of 5 or more characters.

    The tokens must be the same length and share a first letter. That keeps a
    trailing or internal typo (``Barva`` / ``Barba``) and rejects ``person``
    against ``Persson`` (an insertion) and ``Gerson`` (a different letter).
    """

    if len(query) < 5 or len(candidate) < 5:
        return False
    if len(query) != len(candidate) or query[0] != candidate[0]:
        return False
    return levenshtein(query, candidate) <= 1


def _token_match(query: str, candidate: str) -> bool:
    """Exact, a prefix of 3 or more characters, or a tight fuzzy match."""

    if query == candidate:
        return True
    shorter, longer = (query, candidate) if len(query) <= len(candidate) else (candidate, query)
    if len(shorter) >= 3 and longer.startswith(shorter):
        return True
    return _fuzzy_token(query, candidate)


def _negative_alias(phrase: str) -> bool:
    tokens = fold_name(phrase).split()
    if not tokens:
        return True
    if tokens[0] in _NEGATIVE_ALIAS:
        return True
    return len(tokens) >= 2 and tokens[0] == "not" and tokens[1] == "other"


def _name_pieces(name: str) -> list[str]:
    """The name outside parentheses, plus parenthetical aliases that are real."""

    pieces: list[str] = []
    outside = re.sub(r"\([^)]*\)", " ", name).strip()
    if outside and not _negative_alias(outside):
        pieces.append(outside)
    for inner in re.findall(r"\(([^)]*)\)", name):
        inner = inner.strip()
        if inner and not _negative_alias(inner):
            pieces.append(inner)
    return pieces


def _alias_phrases(value: str) -> list[str]:
    phrases: list[str] = []
    for part in re.split(r"[,;/]", value):
        phrase = part.strip()
        if phrase and not _negative_alias(phrase):
            phrases.append(phrase)
    return phrases


def _prose_name(text: str) -> str | None:
    """Leading ``Name is ...`` clause, when the record has no structured name."""

    head = text.strip().splitlines()[0] if text.strip() else ""
    marker = head.casefold().find(" is ")
    if marker <= 0:
        return None
    name = head[:marker].strip()[:160]
    if not name or _negative_alias(name):
        return None
    return name


def name_forms(content: Mapping[str, object]) -> list[str]:
    """Name and real alias strings. Notes and biography text are not included."""

    forms: list[str] = []
    name = content.get("name")
    if isinstance(name, str) and name.strip():
        forms.extend(_name_pieces(name))
    aliases = content.get("aliases")
    if isinstance(aliases, list):
        for item in aliases:
            if isinstance(item, str) and item.strip() and not _negative_alias(item):
                forms.extend(_name_pieces(item))
    text = content.get("text")
    if isinstance(text, str) and text.strip():
        for match in _NAME_LINE.finditer(text):
            forms.extend(_name_pieces(match.group(1)))
        for match in _ALIAS_LINE.finditer(text):
            for phrase in _alias_phrases(match.group(1)):
                forms.extend(_name_pieces(phrase))
        if not forms:
            prose = _prose_name(text)
            if prose:
                forms.extend(_name_pieces(prose))
    seen: set[str] = set()
    unique: list[str] = []
    for form in forms:
        key = fold_name(form)
        if key and key not in seen:
            seen.add(key)
            unique.append(form)
    return unique


def name_match(query: str, content: Mapping[str, object]) -> str:
    """How ``query`` names this record: ``exact``, ``all``, ``fallback``, or ``none``.

    Every significant query token must match a name token (exact, a prefix of
    3 or more characters, or a one-edit fuzzy match). ``fallback`` means the
    query has at least three significant tokens, all but one match, and the
    longest token is among the matches. The caller uses fallback only when
    nobody matched in full.
    """

    query_tokens = _name_tokens(query)
    if not query_tokens:
        return "none"
    forms = name_forms(content)
    folded_query = fold_name(query)
    if any(fold_name(form) == folded_query for form in forms):
        return "exact"
    name_tokens: list[str] = []
    for form in forms:
        name_tokens.extend(_name_tokens(form))
    if not name_tokens:
        return "none"
    matched = [
        token
        for token in query_tokens
        if any(_token_match(token, candidate) for candidate in name_tokens)
    ]
    if len(matched) == len(query_tokens):
        return "all"
    if len(query_tokens) >= 3 and len(query_tokens) - len(matched) == 1:
        longest = max(len(token) for token in query_tokens)
        if all(token in matched for token in query_tokens if len(token) == longest):
            return "fallback"
    return "none"


def name_score(query: str, content: Mapping[str, object]) -> float:
    """Score how well ``query`` names this record. 1.0 is exact, 0 is none.

    Full-name equality outranks every token matching, which outranks a
    one-token miss. Strict matches stay at or above ``NAME_ACCEPT``.
    """

    return {"exact": 1.0, "all": 0.95, "fallback": 0.85, "none": 0.0}[name_match(query, content)]


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
