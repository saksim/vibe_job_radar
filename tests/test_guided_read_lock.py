"""Coordinate an actual open file handle with the task's atomic writer."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.workspace import Workspace


class GuidedReadLockTests(unittest.TestCase):
    def test_open_reader_serializes_atomic_save_on_same_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = GuidedService(Workspace(tmp))
            try:
                with patch.object(service, '_submit'):
                    task = service.create({'platform':'liepin','keyword':'架构师','consent':True,
                        'rights_note':'Artificial offline concurrency fixture'})
                state = service._load(task['id'])
                target = service._path(task['id'])
                reading, release = threading.Event(), threading.Event()
                attempted, saved = threading.Event(), threading.Event()
                errors = []
                original = Path.read_text
                def hold_read(path, *args, **kwargs):
                    if path != target:
                        return original(path, *args, **kwargs)
                    with path.open(encoding='utf-8') as stream:
                        reading.set()
                        if not release.wait(5):
                            raise AssertionError('fixture read was not released')
                        return stream.read()
                def reader():
                    try: service._load(task['id'])
                    except Exception as exc: errors.append(exc)
                def writer():
                    attempted.set()
                    try:
                        service._save(state, 'paused', status='paused')
                        saved.set()
                    except Exception as exc: errors.append(exc)
                with patch.object(Path, 'read_text', hold_read):
                    read_thread = threading.Thread(target=reader); read_thread.start()
                    write_thread = None
                    try:
                        self.assertTrue(reading.wait(3))
                        write_thread = threading.Thread(target=writer); write_thread.start()
                        self.assertTrue(attempted.wait(3))
                        self.assertFalse(saved.wait(.1))
                    finally:
                        release.set(); read_thread.join(5)
                        if write_thread: write_thread.join(5)
                self.assertEqual(errors, [])
                self.assertTrue(saved.is_set())
                self.assertEqual(service._load(task['id'])['status'], 'paused')
            finally:
                service.close()


if __name__ == '__main__':
    unittest.main()
