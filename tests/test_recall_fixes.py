"""Outcomes 1–4: rule recall, private matching, name search, people loader."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor" / "memseek" / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from memseek.artifacts import continue_block
from memseek.search.names import fold_name, name_score
from memseek.search.recall_rank import label_unreviewed_text, select_context, select_people, select_rules

import backfill_embeddings
import load_people

FOREVER = 253402300799


def rule(identity, text, *, priority=50, seq=1, strength="hard", similarity=0.0, text_match=0.0):
    return {
        "id": identity,
        "seq": seq,
        "collection": "preferences",
        "similarity": similarity,
        "text_match": text_match,
        "content": {
            "text": text,
            "activation": "confirmed",
            "strength": strength,
            "priority": priority,
            "valid_until": FOREVER,
        },
    }


def note(identity, text, *, visibility="private", collection="life_context", seq=1, similarity=0.0):
    return {
        "id": identity,
        "seq": seq,
        "collection": collection,
        "similarity": similarity,
        "text_match": 0.0,
        "content": {
            "text": text,
            "activation": "confirmed",
            "visibility": visibility,
            "kind": "fact",
            "valid_until": FOREVER,
        },
    }


def person(identity, text, *, visibility="private", seq=1, similarity=0.0, activation="confirmed"):
    return {
        "id": identity,
        "seq": seq,
        "collection": "people",
        "similarity": similarity,
        "text_match": 0.0,
        "content": {
            "text": text,
            "activation": activation,
            "visibility": visibility,
            "kind": "relationship",
            "valid_until": FOREVER,
        },
    }


class RulesRecall(unittest.TestCase):
    def test_core_plus_relevant_newest_first_and_pages(self):
        rows = [
            rule("old-low", "Keep archives.", priority=10, seq=1),
            rule("new-low", "Keep archives too.", priority=10, seq=9),
            rule("top", "Never disclose private medical reasons.", priority=100, seq=2),
            rule("email-old", "For client emails, use a concise style.", priority=30, seq=3, similarity=0.4),
            rule("email-new", "For client emails, use a concise style.", priority=30, seq=8, similarity=0.4),
            rule("plants", "Water the plants on Sunday.", priority=15, seq=4),
        ]
        page = select_rules(
            rows, query="Email the client a concise update", now=1_700_000_000,
            offset=0, limit=10, core_size=1,
        )
        self.assertEqual(page.core_ids, ("top",))
        self.assertEqual(page.ids[0], "top")
        self.assertEqual(page.ids[1:], ("email-new", "email-old"))
        self.assertNotIn("plants", page.ids)
        self.assertNotIn("old-low", page.ids)
        self.assertFalse(page.has_more)

        paged = select_rules(
            rows, query="Email the client a concise update", now=1_700_000_000,
            offset=1, limit=1, core_size=1,
        )
        self.assertEqual(paged.ids, ("email-new",))
        self.assertTrue(paged.has_more)
        self.assertEqual(paged.next_offset, 2)
        rest = select_rules(
            rows, query="Email the client a concise update", now=1_700_000_000,
            offset=paged.next_offset, limit=10, core_size=1,
        )
        self.assertEqual(rest.ids, ("email-old",))
        self.assertFalse(rest.has_more)

    def test_tie_on_score_keeps_the_newer_rule(self):
        rows = [
            rule("older", "Mention the atlas project in the note.", priority=5, seq=1, similarity=0.9),
            rule("newer", "Mention the atlas project in the note.", priority=5, seq=8, similarity=0.9),
            rule("core", "Always be kind.", priority=100, seq=1),
        ]
        page = select_rules(rows, query="atlas project", now=1, offset=0, limit=10, core_size=1)
        matched = [item for item in page.ids if item != "core"]
        self.assertEqual(matched, ["newer", "older"])


class MatchingVisibility(unittest.TestCase):
    def test_owner_sees_private_notes_and_rules_shareable_scope_does_not(self):
        rows = [
            note("secret", "Private cancellation reason: SYNTHETIC_MEDICAL_SECRET."),
            note("atlas", "Sarah's work meeting concerns the synthetic Atlas project.", visibility="shareable", seq=2),
            note("other-owner", "OTHER_OWNER_MARKER", visibility="private"),
            rule("privacy", "Never disclose private medical reasons.", priority=100, seq=3),
        ]
        # The other owner is a different entity and is not in this result set.
        owned = [row for row in rows if row["id"] != "other-owner"]
        internal = select_context(
            owned,
            query="Research Atlas",
            now=1,
            visibility={"private", "shareable"},
            offset=0,
            limit=6,
        )
        self.assertIn("secret", internal.ids)
        self.assertIn("atlas", internal.ids)
        self.assertNotIn("other-owner", internal.ids)

        outward = select_context(
            owned,
            query="Research Atlas",
            now=1,
            visibility={"shareable"},
            offset=0,
            limit=6,
        )
        self.assertNotIn("secret", outward.ids)
        self.assertIn("atlas", outward.ids)

    def test_matching_includes_a_relevant_rule(self):
        rows = [
            note(f"n{i}", f"Unrelated note {i} about commuting.", seq=i)
            for i in range(10)
        ]
        rows.append(rule("email-rule", "For client emails, use bullet points.", seq=50))
        page = select_context(
            rows,
            query="Write the client email",
            now=1,
            visibility={"private", "shareable"},
            offset=0,
            limit=6,
        )
        self.assertIn("email-rule", page.ids)
        self.assertTrue(all(not item.startswith("n") for item in page.ids))

    def test_small_note_set_is_not_dropped(self):
        rows = [
            note("secret", "Private cancellation reason: SYNTHETIC_MEDICAL_SECRET."),
            note("atlas", "Sarah's work meeting concerns the synthetic Atlas project.", visibility="shareable"),
        ]
        page = select_context(
            rows, query="Research Atlas", now=1, visibility={"private", "shareable"}, offset=0, limit=6
        )
        self.assertCountEqual(page.ids, ["secret", "atlas"])


class PeopleNames(unittest.TestCase):
    def test_accent_and_alias_and_title_and_empty_miss(self):
        rows = [
            person("alcivar", "Name: Juan Alcívar\nRole: neighbor"),
            person("barba", "Name: Elizabeth Barba Orozco\nAliases: Dra. Barba\nRole: physician"),
            person("other", "Name: Marcos Peña\nRole: unrelated"),
            person("candidate", "Name: Elizabeth Barba\nRole: duplicate", activation="candidate"),
            person("shareable", "Name: Elena Barba\nRole: public", visibility="shareable"),
        ]
        alcivar = select_people(
            rows, query="Alcivar", now=1, visibility={"private", "shareable"}, offset=0, limit=6
        )
        self.assertEqual(alcivar.ids, ("alcivar",))

        barba = select_people(
            rows, query="Dra. Barba", now=1, visibility={"private", "shareable"}, offset=0, limit=6
        )
        self.assertEqual(barba.ids[0], "barba")
        self.assertNotIn("candidate", barba.ids)

        self.assertGreater(name_score("Alcivar", rows[0]["content"]), 0.9)
        self.assertGreaterEqual(name_score("Dra. Barba", rows[1]["content"]), 0.8)
        self.assertEqual(fold_name("Alcívar"), "alcivar")

        miss = select_people(
            rows, query="zzzz-not-a-person", now=1, visibility={"private", "shareable"}, offset=0, limit=6
        )
        self.assertEqual(miss.ids, ())
        self.assertFalse(miss.has_more)

        shareable = select_people(
            rows, query="Barba", now=1, visibility={"shareable"}, offset=0, limit=6
        )
        self.assertEqual(shareable.ids, ("shareable",))

    def test_archivist_candidates_are_found_and_rank_below_reviewed(self):
        reviewed = person(
            "reviewed-barba",
            "Name: Elizabeth Barba Orozco\nRole: physician",
            seq=2,
            similarity=0.4,
        )
        reviewed["content"]["reviewed"] = True
        candidate = person(
            "file-barba",
            "Name: Elizabeth Barba Orozco\nRole: physician",
            activation="candidate",
            seq=9,
            similarity=0.4,
        )
        candidate["content"]["reviewed"] = False
        candidate["content"]["source"] = "archivist-file"
        other_candidate = person(
            "guess",
            "Name: Elizabeth Barba\nUNCONFIRMED_MARKER",
            activation="candidate",
            seq=3,
        )
        rows = [reviewed, candidate, other_candidate]
        page = select_people(
            rows, query="Dra. Barba", now=1, visibility={"private", "shareable"}, offset=0, limit=6
        )
        self.assertEqual(page.ids, ("reviewed-barba", "file-barba"))
        self.assertNotIn("guess", page.ids)
        self.assertIn("(unreviewed)", label_unreviewed_text(candidate["content"]).casefold())

        hidden = select_people(
            rows, query="Dra. Barba", now=1, visibility={"shareable"}, offset=0, limit=6
        )
        self.assertNotIn("file-barba", hidden.ids)
        self.assertNotIn("reviewed-barba", hidden.ids)

    def test_name_match_ranks_above_a_stronger_semantic_hit(self):
        rows = [
            person("semantic", "Name: Marcos Peña\nRole: catalogs", similarity=0.95),
            person("named", "Name: Elizabeth Barba Orozco", similarity=0.05),
        ]
        page = select_people(
            rows, query="Barba", now=1, visibility={"private"}, offset=0, limit=6
        )
        self.assertEqual(page.ids[0], "named")


class Paging(unittest.TestCase):
    def test_token_cut_is_not_a_dead_end(self):
        offset, nxt, has_more, truncated = continue_block(
            source_offset=0,
            source_has_more=False,
            source_next=None,
            packed=12,
            returned=40,
            token_truncated=True,
            unpaged_total=None,
            missing=False,
        )
        self.assertTrue(truncated)
        self.assertTrue(has_more)
        self.assertEqual(nxt, 12)
        self.assertEqual(offset, 0)

    def test_last_page_tells_the_caller_to_stop(self):
        _offset, nxt, has_more, truncated = continue_block(
            source_offset=12,
            source_has_more=False,
            source_next=None,
            packed=4,
            returned=4,
            token_truncated=False,
            unpaged_total=None,
            missing=False,
        )
        self.assertTrue(truncated)
        self.assertFalse(has_more)
        self.assertIsNone(nxt)

    def test_view_has_more_exposes_next_offset(self):
        _offset, nxt, has_more, truncated = continue_block(
            source_offset=0,
            source_has_more=True,
            source_next=40,
            packed=40,
            returned=40,
            token_truncated=False,
            unpaged_total=None,
            missing=False,
        )
        self.assertTrue(has_more and truncated)
        self.assertEqual(nxt, 40)


class PeopleLoader(unittest.TestCase):
    SAMPLE = "\n".join(
        [
            "# comment",
            "LOCK this section",
            "KEEP another",
            "Name|reach|role|P#|LANG|notes",
            "Elizabeth Barba Orozco|barba@example.com|physician|P1|ES|Dra. Barba",
            "Juan Alcívar|HOLE|neighbor|P2|ES",
            "Elizabeth Barba Orozco|barba@example.com|physician|P1|ES|Dra. Barba",
        ]
    )

    def test_parse_skips_header_and_marks_unreviewed_private_candidates(self):
        people, _notes = load_people.parse_people(self.SAMPLE)
        self.assertEqual(len(people), 2)
        first = people[0]["content"]
        self.assertEqual(first["activation"], "candidate")
        self.assertEqual(first["visibility"], "private")
        self.assertEqual(first["biography_source_claim"], "archivist-file")
        self.assertEqual(first["source"], "archivist-file")
        self.assertIs(first["reviewed"], False)
        self.assertNotIn("review_id", first)
        self.assertIn("(unreviewed)", people[0]["text"].casefold())
        self.assertIn("UNREVIEWED CANDIDATE", people[0]["text"])
        self.assertIn("not owner-reviewed", people[0]["text"])
        self.assertIn("Alcívar", people[1]["text"])
        self.assertIn("Name: Elizabeth Barba Orozco", people[0]["text"])
        self.assertIn("Dra. Barba", people[0]["text"])

    def test_reparse_is_idempotent(self):
        first, _ = load_people.parse_people(self.SAMPLE)
        second, _ = load_people.parse_people(self.SAMPLE)
        self.assertEqual([row["key"] for row in first], [row["key"] for row in second])
        self.assertEqual([row["dedupe_key"] for row in first], [row["dedupe_key"] for row in second])
        self.assertEqual(len({row["key"] for row in first}), 2)

    def test_dry_run_does_not_need_the_network(self):
        path = ROOT / "tests" / "_people_sample.txt"
        path.write_text(self.SAMPLE, encoding="utf-8")
        try:
            summary = load_people.load(path, owner="owner", dry_run=True, batch_size=20, pause=0, url="http://127.0.0.1:9")
        finally:
            path.unlink()
        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["parsed"], 2)
        self.assertEqual(summary["inserted"], 0)
        self.assertFalse(summary["reviewed"])


class BackfillEstimate(unittest.TestCase):
    def test_cost_is_a_few_cents_for_the_live_corpus(self):
        estimate = backfill_embeddings.estimate_cost()
        self.assertEqual(estimate["model"], "text-embedding-3-small")
        self.assertEqual(estimate["usd_per_million_input_tokens"], 0.02)
        self.assertLess(estimate["usd"], 0.05)
        self.assertGreater(estimate["tokens"], 300_000)
        text = json.dumps(estimate)
        self.assertNotIn("api_key", text.casefold())


class CatalogShape(unittest.TestCase):
    def test_package_and_rollback_files_exist(self):
        package = (ROOT / "catalog" / "packages" / "personal_context_v5.yaml").read_text(encoding="utf-8")
        self.assertIn("version: 0.5.0", package)
        self.assertIn("interaction_context@5", package)
        self.assertIn("embedding_rules", package)
        rollback = list((ROOT / "rollback" / "catalog-0.4.0").rglob("*.yaml"))
        self.assertEqual(len(rollback), 32)
        old = (ROOT / "rollback" / "catalog-0.4.0" / "packages" / "personal_context_v4.yaml").read_text(encoding="utf-8")
        self.assertIn("version: 0.4.0", old)
        self.assertNotIn("embedding_rules", old)


if __name__ == "__main__":
    unittest.main()
