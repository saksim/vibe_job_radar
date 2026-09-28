from types import SimpleNamespace
import unittest

from vibe_job_radar.guided.cdp_browser import CDPContext
from vibe_job_radar.guided.saved_session import _cookies


class CookieCompatibilityTests(unittest.TestCase):
    def export(self, *extra):
        raw = {'name': 'synthetic_session', 'value': 'synthetic_value', 'domain': 'fixture.test',
               'path': '/', 'expires': -1, 'httpOnly': True, 'secure': True,
               'session': True, 'size': 24, 'priority': 'Medium'}
        raw.update(dict(extra))
        context = CDPContext.__new__(CDPContext)
        context.ident = 'owned-context'
        context.browser = SimpleNamespace(connection=SimpleNamespace(root=SimpleNamespace(
            send=lambda method, params: {'cookies': [raw]})))
        return context.cookies()

    def test_unspecified_samesite_remains_compatible_with_saved_session(self):
        cookies = _cookies(self.export(), {'fixture.test'})
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0]['sameSite'], 'Lax')
        self.assertEqual(cookies[0]['expires'], -1)
        self.assertTrue(cookies[0]['httpOnly'])
        self.assertNotIn('priority', cookies[0])

    def test_explicit_samesite_is_preserved(self):
        self.assertEqual(_cookies(self.export(('sameSite', 'Strict')), {'fixture.test'})[0]['sameSite'], 'Strict')

    def test_partitioned_cookies_never_become_unpartitioned_saved_cookies(self):
        for values in [(('partitionKey', {'topLevelSite': 'https://fixture.test'}),),
                       (('partitionKeyOpaque', True),)]:
            with self.subTest(values=values):
                self.assertEqual(_cookies(self.export(*values), {'fixture.test'}), [])


if __name__ == '__main__':
    unittest.main()
