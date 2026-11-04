"""Synthetic fixtures only. Run on host Python; never includes a real export."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("prepare_whatsapp", Path(__file__).parents[1]/"scripts/prepare_whatsapp.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class WhatsAppTests(unittest.TestCase):
    def convert(self, text, **kwargs):
        return module.convert(text, date_order="dmy", timezone="Europe/Paris",
            chat_id="friends", owner_names=["Alex"], **kwargs)

    def test_speakers_multiline_and_system(self):
        rows, counts = self.convert("[29/09/2026, 09:00:00] Friends: Group created\n"
            "[29/09/2026, 09:01:00] Alex: I cannot attend.\nPlease tell Sam.\n"
            "[29/09/2026, 09:02:00] Sam: Say that Alex prefers mornings.\n")
        self.assertEqual([r["authority"] for r in rows], ["external", "owner", "external"])
        self.assertIn("Please tell Sam", rows[1]["text"])
        self.assertIn("+02:00", rows[1]["text"])

    def test_android_and_unicode(self):
        rows, _ = self.convert("\u200e29/09/2026, 9:01\u202fPM - Alex: Hello\n")
        self.assertIn("21:01:00+02:00", rows[0]["text"])

    def test_stable_ids_across_window(self):
        text = "[28/09/2026, 09:00:00] Alex: One\n[29/09/2026, 09:00:00] Alex: Two\n"
        all_rows, _ = self.convert(text)
        selected, _ = self.convert(text, since="2026-09-29")
        self.assertEqual(all_rows[1]["event_id"], selected[0]["event_id"])

    def test_bad_input_and_unknown_owner_fail(self):
        for text in ["not a WhatsApp export", "[29/09/2026, 09:00:00] Sam: Hi",
                     "[29/09/2026, 25:00:00] Alex: Hi"]:
            with self.assertRaises(ValueError):
                self.convert(text)

    def test_export_notices_and_gif_are_not_owner_evidence(self):
        rows, counts = self.convert("[29/09/2026, 09:00:00] Alex: You pinned a message\n"
            "[29/09/2026, 09:01:00] Alex: GIF omitted\n"
            "[29/09/2026, 09:02:00] Alex: I can come at 19:00\n")
        self.assertEqual(len(rows), 1)
        self.assertEqual(counts['system'], 1)
        self.assertEqual(counts['media_or_deleted'], 1)


if __name__ == "__main__":
    unittest.main()
