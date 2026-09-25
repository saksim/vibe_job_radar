"""First-timeout evidence contains bounded code positions, never local values."""
import importlib.util
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch


class NativeRetryLocationsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scripts = Path(__file__).resolve().parents[1]/'scripts'
        sys.path.insert(0,str(scripts))
        try:
            spec=importlib.util.spec_from_file_location('native_retry_locations_fixture',scripts/'run_native_read_retry.py')
            cls.module=importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.module)
        finally:
            sys.path.remove(str(scripts))

    def test_worker_positions_omit_local_values_and_full_paths(self):
        private_value = 'must-not-be-recorded'
        rows = self.module.worker_locations(threading.current_thread())
        self.assertTrue(rows)
        self.assertLessEqual(len(rows),32)
        for row in rows:
            self.assertEqual(set(row),{'file','function','line'})
            self.assertEqual(Path(row['file']).name,row['file'])
        self.assertNotIn(private_value,str(rows))

    def test_missing_or_retired_worker_has_no_stack(self):
        self.assertEqual(self.module.worker_locations(None),[])
        with patch.object(self.module.sys,'_current_frames',return_value={}):
            self.assertEqual(self.module.worker_locations(threading.current_thread()),[])
        with patch.object(self.module.sys,'_current_frames',side_effect=RuntimeError('unavailable')):
            self.assertEqual(self.module.worker_locations(threading.current_thread()),[])
