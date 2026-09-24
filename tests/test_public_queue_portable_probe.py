"""Preflight the same bounded queued-worker probe before the frozen CI run."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from verify_windows_portable import verify_queued_worker


class QueuePortableProbeTests(unittest.TestCase):
    def test_source_process_uses_the_same_probe_and_original_cache_report_pipeline(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,
                {k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')},clear=True):
            root=Path(temp);cwd=root/'another directory';cwd.mkdir()
            result=verify_queued_worker(Path(sys.executable),root,cwd,dict(os.environ),
                command_prefix=[sys.executable,str(ROOT/'scripts/start_workbench.py')])
            self.assertEqual(result,{'completed_queries':1,'network_requests':0,'full_text_job_groups':1,'daily_plan_enabled':False})


if __name__=='__main__':unittest.main()
