"""Exercise the portable metadata probe with the real HTTP/worker path first."""
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from verify_windows_portable import RunningApp, verify_detail_history
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace


class DetailHistoryPortableProbeTests(unittest.TestCase):
    def test_probe_preserves_history_and_flags_old_writer_without_network_or_browser(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(temp)
            server = LocalServer(workspace)
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
            thread.start()
            app = object.__new__(RunningApp)
            app.port, app.origin, app.token = server.server_address[1], server.origin, server.token
            try:
                before = server.guided.ledger.summary('liepin')
                with patch.object(server.guided, '_backend', side_effect=AssertionError('no browser')):
                    ident, history = verify_detail_history(app, workspace.root)
                self.assertEqual(history['origin'], 'legacy_partial')
                self.assertEqual([a['outcome'] for a in history['attempts']], ['jd_incomplete', 'unfinished'])
                self.assertEqual(server.guided.ledger.summary('liepin'), before)
                self.assertEqual(server.guided._load(ident)['detail_attempt_history'], history)
                self.assertFalse(workspace.db.exists())
                self.assertEqual(list((workspace.root / 'reports').iterdir()), [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)


if __name__ == '__main__':
    unittest.main()
