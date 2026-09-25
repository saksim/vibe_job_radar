"""Artificial tracebacks verify useful locations without private exception data."""
import json
from pathlib import Path
import tempfile
import unittest

from portable_failure_diagnostics import SCRIPTS, failure_frames


class PortableFailureDiagnosticsTests(unittest.TestCase):
    def trace(self, code, filename):
        try:
            exec(compile(code, str(filename), 'exec'), {})
        except AssertionError as error:
            return failure_frames(error)
        self.fail('artificial exception was not raised')

    def test_only_allowed_code_locations_survive_without_messages_paths_or_locals(self):
        frames=self.trace("def artificial():\n private='PRIVATE_TOKEN_AND_SCRIPT'\n raise AssertionError(private)\nartificial()\n",
                          SCRIPTS/'system_pac_acceptance.py')
        self.assertEqual(frames,[{'file':'system_pac_acceptance.py','function':'<module>','line':4},
                                 {'file':'system_pac_acceptance.py','function':'artificial','line':3}])
        value=json.dumps(frames)
        self.assertNotIn('PRIVATE',value);self.assertNotIn(str(SCRIPTS),value)
        self.assertNotIn('AssertionError',value);self.assertNotIn('private',value)

    def test_other_modules_and_same_basename_elsewhere_are_omitted_and_depth_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.trace("raise AssertionError('PRIVATE')",Path(tmp)/'system_pac_acceptance.py'),[])
        self.assertEqual(self.trace("raise AssertionError('PRIVATE')",SCRIPTS/'unapproved.py'),[])
        frames=self.trace("def recurse(n):\n if n: return recurse(n-1)\n raise AssertionError('PRIVATE')\nrecurse(20)\n",
                          SCRIPTS/'verify_windows_portable.py')
        self.assertEqual(len(frames),8)
        self.assertTrue(all(set(row)=={'file','function','line'} for row in frames))
        self.assertNotIn('PRIVATE',json.dumps(frames))
