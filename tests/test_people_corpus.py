"""People search against a few thousand real-looking names.

The stand-in embedding copies the shape of text-embedding-3-small on short
records: unrelated texts share a cone, so cosine often clears the old 0.28
gate, and only a near-paraphrase clears the people gate.
"""

from __future__ import annotations

import math
import random
import re
import sys
import unicodedata
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor" / "memseek" / "src"))

from memseek.search.recall_rank import PEOPLE_COSINE_MIN, PEOPLE_PAGE, select_people

FOREVER = 253402300799
_DIM = 256
_FIRST = (
    "Ana Luis Maria Jose Pedro Lucia Andres Sofia Diego Carmen Hugo Elena Pablo Rosa "
    "Ivan Nora Oscar Vera Raul Ines Mateo Julia Tomas Clara"
).split()
_LAST = (
    "Garcia Rodriguez Martinez Lopez Gonzalez Perez Sanchez Ramirez Torres Flores "
    "Rivera Gomez Diaz Cruz Morales Reyes Ortiz Vargas Castro Romero Navarro Mendoza Silva Rojas"
).split()


def _fold(text: str) -> list[str]:
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", stripped.casefold()).split()


def embed_like_small(text: str) -> list[float]:
    """Shared cone plus one nearly-orthogonal direction per token."""

    acc = [0.0] * _DIM
    acc[0] = 1.1
    seen: set[str] = set()
    for token in _fold(text):
        if token in seen:
            continue
        seen.add(token)
        rng = random.Random("emb3small:" + token)
        for index in range(_DIM):
            acc[index] += rng.uniform(-1, 1) / math.sqrt(_DIM)
    norm = math.sqrt(sum(value * value for value in acc)) or 1.0
    return [value / norm for value in acc]


def _cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def _row(identity: str, name: str, query_vector: list[float]) -> dict:
    text = f"(unreviewed)\nName: {name}\nRole: neighbor\nReach: HOLE"
    vector = embed_like_small(text)
    return {
        "id": identity,
        "seq": 1,
        "collection": "people",
        "similarity": _cosine(query_vector, vector),
        "text_match": 0.0,
        "content": {
            "text": text,
            "activation": "candidate",
            "visibility": "private",
            "source": "archivist-file",
            "reviewed": False,
            "kind": "relationship",
            "valid_until": FOREVER,
        },
    }


def _corpus() -> list[dict]:
    rng = random.Random(2450)
    names = [f"{rng.choice(_FIRST)} {rng.choice(_LAST)} {rng.choice(_LAST)}" for _ in range(2448)]
    names.append("Dra. Elizabeth Barba Orozco")
    names.append("Cristian Alcivar Conde")
    return names


class RealisticPeopleSearch(unittest.TestCase):
    def setUp(self):
        self.names = _corpus()
        self.assertGreaterEqual(len(self.names), 2450)
        self.assertLessEqual(len(self.names), 2460)

    def _search(self, query: str):
        query_vector = embed_like_small(query)
        rows = [_row(f"p{index}", name, query_vector) for index, name in enumerate(self.names)]
        # The old 0.28 gate would keep a crowd. The stand-in must still show that.
        above_old = sum(1 for row in rows if row["similarity"] >= 0.28)
        self.assertGreater(above_old, 100, "stand-in should reproduce the loose cosine cone")
        return select_people(
            rows,
            query=query,
            now=1_700_000_000,
            visibility={"private", "shareable"},
            offset=0,
            limit=100,
        )

    def test_nonsense_returns_nobody(self):
        page = self._search("zzzz-not-a-person")
        self.assertEqual(page.total, 0)
        self.assertEqual(page.ids, ())
        stranger = max(row["similarity"] for row in (
            _row("x", name, embed_like_small("zzzz-not-a-person")) for name in self.names[:40]
        ))
        self.assertLess(stranger, PEOPLE_COSINE_MIN)

    def test_barba_and_alcivar_are_short_name_lists(self):
        barba = self._search("Barba")
        self.assertLessEqual(barba.total, PEOPLE_PAGE)
        self.assertGreaterEqual(barba.total, 1)
        top = self.names[int(barba.ids[0][1:])]
        self.assertEqual(top, "Dra. Elizabeth Barba Orozco")

        alcivar = self._search("Alcívar")
        self.assertLessEqual(alcivar.total, 5)
        self.assertGreaterEqual(alcivar.total, 1)
        self.assertLessEqual(len(alcivar.ids), PEOPLE_PAGE)
        top = self.names[int(alcivar.ids[0][1:])]
        self.assertEqual(top, "Cristian Alcivar Conde")

    def test_near_paraphrase_clears_the_vector_gate_and_pages(self):
        query = "Name: Cristian Alcivar Conde Role: neighbor Reach: HOLE"
        query_vector = embed_like_small(query)
        text = "(unreviewed)\nName: Cristian Alcivar Conde\nRole: neighbor\nReach: HOLE"
        similarity = _cosine(query_vector, embed_like_small(text))
        self.assertGreaterEqual(similarity, PEOPLE_COSINE_MIN)
        extras = []
        for index in range(7):
            extras.append(
                {
                    "id": f"same-{index}",
                    "seq": index,
                    "collection": "people",
                    "similarity": 0.0,
                    "text_match": 0.0,
                    "content": {
                        "text": "(unreviewed)\nName: Barba Nunez Extra\nRole: x",
                        "activation": "candidate",
                        "visibility": "private",
                        "source": "archivist-file",
                        "reviewed": False,
                        "valid_until": FOREVER,
                    },
                }
            )
        # Seven strong name matches page at 5.
        page = select_people(
            extras,
            query="Barba",
            now=1,
            visibility={"private"},
            offset=0,
            limit=100,
        )
        self.assertEqual(page.total, 7)
        self.assertEqual(len(page.ids), PEOPLE_PAGE)
        self.assertTrue(page.has_more)
        rest = select_people(
            extras, query="Barba", now=1, visibility={"private"}, offset=page.next_offset, limit=100
        )
        self.assertEqual(len(rest.ids), 2)
        self.assertFalse(rest.has_more)
