"""Faults and deterministic competing launches in artificial local workspaces."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_job_radar import evidence_ui
from vibe_job_radar.evidence_ui import EvidenceService
from vibe_job_radar.workspace import Workspace


class EvidenceInitializationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Workspace(temporary.name)

    def winning_launch(self):
        service = EvidenceService(self.workspace)
        service._write(0, 'artificial competing launch', lambda state: state.update(name='published first'))
        return service

    def assert_winner_preserved(self, service):
        state = service.state()
        self.assertEqual((state['revision'], state['name']), (1, 'published first'))
        self.assertEqual(list(self.workspace.root.glob('.evidence-init-*')), [])

    def test_initial_database_failure_is_retryable(self):
        with patch.object(evidence_ui.sqlite3, 'connect', side_effect=OSError('artificial database interruption')):
            with self.assertRaisesRegex(OSError, 'artificial database'):
                EvidenceService(self.workspace)
        self.assertFalse((self.workspace.root / 'evidence').exists())
        self.assertEqual(EvidenceService(self.workspace).state()['revision'], 0)

    def test_initial_publish_failure_is_retryable(self):
        with patch.object(evidence_ui.os, 'rename', side_effect=OSError('artificial publication interruption')):
            with self.assertRaisesRegex(OSError, 'artificial publication'):
                EvidenceService(self.workspace)
        self.assertFalse((self.workspace.root / 'evidence').exists())
        self.assertEqual(EvidenceService(self.workspace).state()['revision'], 0)

    def test_launch_during_initialization_keeps_the_winning_history(self):
        original = sqlite3.connect
        armed = True
        def interleave(*args, **kwargs):
            nonlocal armed
            if armed:
                armed = False
                self.winning_launch()
            return original(*args, **kwargs)
        with patch.object(evidence_ui.sqlite3, 'connect', side_effect=interleave):
            service = EvidenceService(self.workspace)
        self.assertFalse(armed)
        self.assert_winner_preserved(service)

    def test_competing_atomic_publish_keeps_the_winning_history(self):
        original = os.rename
        armed = True
        def interleave(source, target, *args, **kwargs):
            nonlocal armed
            if armed and Path(target) == self.workspace.root / 'evidence':
                armed = False
                self.winning_launch()
            return original(source, target, *args, **kwargs)
        with patch.object(evidence_ui.os, 'rename', side_effect=interleave):
            service = EvidenceService(self.workspace)
        self.assertFalse(armed)
        self.assert_winner_preserved(service)


if __name__ == '__main__':
    unittest.main()
