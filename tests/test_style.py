"""Host-only tests of trusted evidence/routing boundaries, without SDK dependencies."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('style', Path(__file__).parents[1]/'personal_context/style.py')
style = importlib.util.module_from_spec(spec); spec.loader.exec_module(style)
SCOPE = dict(channel='chat', audience='friends', domain='personal', recipient='any', language='en')
FRAME = dict(actions=['communicate'], channels=['chat'], audiences=['friends'], domains=['personal'],
             recipients=['group:games'], language='en')

def sample(i, **kw):
    return dict(event_id=f'e{i}', text=f'Message {i}', timestamp=f'2026-09-{i+1:02}T12:00:00+02:00',
                source='whatsapp:games', authority='owner', origin='authored', **kw)

class StyleTests(unittest.TestCase):
    def test_scope_and_owner_isolation(self):
        entities = style.style_entities_for_frame('owner', FRAME)
        self.assertIn(style.style_entity('owner', SCOPE), entities)
        for change in [dict(channel='email'), dict(audience='client'), dict(domain='work'), dict(language='fr')]:
            self.assertNotIn(style.style_entity('owner', {**SCOPE, **change}), entities)
        self.assertNotIn(style.style_entity('another', SCOPE), entities)
        self.assertEqual(entities, style.style_entities_for_frame('owner', {**FRAME, 'audiences':['external','friends']}))

    def test_unknown_and_non_communication_get_no_style(self):
        for change in [dict(language=None), dict(actions=['research']), dict(audiences=['any']),
                       dict(channels=['chat','phone']), dict(language='unknown')]:
            self.assertEqual(style.style_entities_for_frame('owner', {**FRAME, **change}), ['__no_style__'])
        with self.assertRaises(ValueError):
            style.normalize_scope({**SCOPE, 'audience':'any'})

    def test_reject_untrusted_or_generated(self):
        for field, value in [('authority','external'),('authority','assistant'),('origin','generated'),
                             ('timestamp','2026-09-01T12:00:00')]:
            with self.assertRaises(ValueError):
                style.sample_window([{**sample(1), field:value}])

    def test_retries_and_repeated_text_are_not_extra_evidence(self):
        result = style.sample_window([sample(1),sample(1),{**sample(2),'text':'Message 1'},sample(3)])
        self.assertEqual(len(result),2)
        with self.assertRaises(ValueError):
            style.sample_window([sample(1),{**sample(1),'text':'Changed'}])

    def test_rolls_by_actual_timestamp_and_bounds_tokens(self):
        samples = [{**sample(1), 'event_id':str(i), 'text':str(i)*300,
                    'timestamp':f'2026-09-01T12:00:{i:02}+02:00'} for i in range(60)]
        result = style.sample_window(list(reversed(samples)))
        self.assertLessEqual(len(result),48)
        self.assertEqual(result[-1]['event_id'],'59')
        self.assertLess(sum(len(s['text']) for s in result),30000)

if __name__ == '__main__':
    unittest.main()
