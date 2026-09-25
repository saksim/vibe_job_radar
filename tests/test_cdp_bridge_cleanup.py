"""A real owned Windows child tree; no browser, account or external request."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


HOLDER = r'''
from pathlib import Path
import sys,time
root=Path(sys.argv[1])
with (root/'locked').open('w') as stream:
    (root/'ready').write_text('ready')
    deadline=time.monotonic()+30
    while not (root/'stop').exists() and time.monotonic()<deadline:
        time.sleep(.02)
(root/'released').write_text('released')
'''

PARENT = r'''
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const [python,code,root] = JSON.parse(process.argv[1]);
spawn(python, ['-c',code,root], {stdio:'ignore', windowsHide:true});
const timer = setInterval(() => {
  if (fs.existsSync(path.join(root, 'stop'))) clearInterval(timer);
}, 20);
setTimeout(() => clearInterval(timer), 30000).unref();
'''


class BridgeCleanupTests(unittest.TestCase):
    def wait_file(self, path):
        deadline = time.monotonic() + 10
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(path.exists(), path.name + ' fixture did not become ready')

    def test_windows_eof_releases_owned_descendant_but_preserves_unrelated_process(self):
        if sys.platform != 'win32':
            return  # Windows-only ownership behavior; exercised by all Windows CI matrices.
        from vibe_job_radar.guided.cdp_pipe import node_executable
        node = shutil.which('node') or node_executable()
        bridge = Path(__file__).resolve().parents[1] / 'src/vibe_job_radar/guided/cdp_bridge.js'
        with tempfile.TemporaryDirectory(prefix='owned-bridge-test-') as folder:
            root = Path(folder)
            owned, unrelated = root/'owned', root/'unrelated'
            owned.mkdir(); unrelated.mkdir()
            peer = subprocess.Popen([sys.executable, '-c', HOLDER, str(unrelated)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
            proc = None
            try:
                self.wait_file(unrelated/'ready')
                args = ['-e', PARENT, '--', json.dumps([sys.executable, HOLDER, str(owned)]),
                        '--remote-debugging-pipe']
                proc = subprocess.Popen([node, str(bridge), node, json.dumps(args)],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                self.wait_file(owned/'ready')
                # A live grandchild really holds the file before requesting EOF.
                with self.assertRaises(PermissionError):
                    (owned/'locked').unlink()
                proc.stdin.close()
                proc.wait(timeout=10)
                self.assertIsNone(peer.poll(), 'cleanup terminated an unrelated owned fixture')
                with self.assertRaises(PermissionError):
                    (unrelated/'locked').unlink()
                # Parent exit alone is insufficient on Windows: the grandchild's
                # handle must have closed before the bridge reports completion.
                (owned/'locked').unlink()
                self.assertFalse((owned/'released').exists(), 'fixture self-exited instead of tree cleanup')
            finally:
                (owned/'stop').write_text('stop'); (unrelated/'stop').write_text('stop')
                peer.wait(timeout=10)
                if proc is not None:
                    if proc.stdin and not proc.stdin.closed:
                        proc.stdin.close()
                    proc.wait(timeout=10)
                # The unfixed version leaves the grandchild alive. Its own
                # bounded stop-file protocol cleans only this authored fixture.
                deadline = time.monotonic() + 10
                while (owned/'locked').exists() and time.monotonic() < deadline:
                    try:
                        (owned/'locked').unlink()
                    except PermissionError:
                        time.sleep(.02)


if __name__ == '__main__':
    unittest.main()
