"""Run inside the Memseek runtime. Complete artifact records must fit or be omitted."""
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from uuid import uuid4
from memseek.artifacts import _pack_block
from memseek.render import escape_untrusted

class ArtifactRenderingTests(unittest.TestCase):
    def row(self, text):
        return dict(id=uuid4(),occurred_at=datetime(2026,9,29,tzinfo=timezone.utc),
            collection='style_profiles',type='style',key='voice',scores={},content={'text':text})

    def pack(self, rows, budget=2000):
        return _pack_block(rows,catalog=SimpleNamespace(processors={}),
            max_tokens=budget,input_cap_left=20,seen=set())

    def test_full_long_record_preserves_middle_and_fences(self):
        text='Short conversational sentences. '*12 + 'MIDDLE: <untrusted> & quoted wording. ' + 'Keep the ending clear. '*15
        self.assertGreater(len(text),500)
        selected,lines,truncated=self.pack([self.row(text)])
        self.assertEqual(len(selected),1)
        self.assertFalse(truncated)
        self.assertIn(escape_untrusted(text),lines[0])
        self.assertNotIn('[...] truncated [...]',lines[0])
        self.assertNotIn('<untrusted>',lines[0])

    def test_budget_accounts_for_full_record_not_preview(self):
        first=self.row('Keep it short.')
        second=self.row('z'*900)
        selected,lines,truncated=self.pack([first,second],budget=220)
        self.assertEqual([r['id'] for r in selected],[first['id']])
        self.assertEqual(len(lines),1)
        self.assertTrue(truncated)

    def test_no_partial_record_when_nothing_fits(self):
        selected,lines,truncated=self.pack([self.row('é'*900)],budget=160)
        self.assertEqual(selected,[])
        self.assertEqual(lines,[])
        self.assertTrue(truncated)


def run_regressions():
    result=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(ArtifactRenderingTests))
    if not result.wasSuccessful():
        raise AssertionError('Artifact rendering regressions failed')

if __name__=='__main__':
    unittest.main()
