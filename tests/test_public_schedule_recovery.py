"""Recovery preserves live ownership and the clock-rollback consent boundary."""
import unittest

import test_public_schedule as fixtures
from vibe_job_radar.public_schedule import DAY, PublicSchedule


class ScheduleRecoveryTests(unittest.TestCase):
    setUp = fixtures.ScheduleTests.setUp
    configure = fixtures.ScheduleTests.configure
    wait = fixtures.ScheduleTests.wait
    hold = fixtures.ScheduleTests.hold

    def test_recovery_keeps_own_live_task_and_disable_can_still_cancel_it(self):
        self.configure(); self.now[0] += DAY
        self.transport.json.side_effect = self.hold
        self.schedule.tick(); self.assertTrue(self.entered.wait(5))
        original = self.schedule._read()
        self.schedule.recover()
        self.assertEqual(self.schedule._read(), original)
        self.assertEqual(self.tasks.snapshot()['status'], 'running')
        self.schedule.disable({'revision': original['revision']})
        self.assertTrue(self.tasks._cancel.is_set())
        self.release.set(); self.assertEqual(self.wait()['status'], 'cancelled')
        self.schedule.tick()
        state = self.schedule.state()
        self.assertEqual(state['status'], 'disabled')
        self.assertEqual(len(state['history']), 1)
        self.assertEqual(state['history'][0]['status'], 'cancelled')
        self.assertEqual(self.transport.json.call_count, 1)

    def test_completed_recovery_after_clock_rollback_keeps_report_and_pauses(self):
        self.configure(); self.now[0] += DAY
        self.schedule.tick(); original = self.wait()
        report = self.workspace.root / 'reports' / original['report_id'] / 'requirements.csv'
        before = report.read_bytes()
        self.now[0] -= 2
        restarted = PublicSchedule(self.workspace, self.tasks, clock=lambda: self.now[0])
        restarted.recover()
        state = restarted.state()
        self.assertEqual((state['status'], state['code'], state['next_due']),
                         ('paused', 'clock_rollback', None))
        self.assertEqual(state['history'][0]['report_id'], original['report_id'])
        self.assertEqual(state['history'][0]['status'], 'completed')
        revision = state['revision']
        restarted.tick(); restarted.tick()
        self.assertEqual(restarted.state()['revision'], revision)
        self.assertEqual(report.read_bytes(), before)
        self.assertTrue(self.workspace.report(original['report_id']))
        self.assertEqual(self.transport.json.call_count, 1)

    def test_live_recovery_after_clock_rollback_retains_task_until_cancelled(self):
        self.configure(); self.now[0] += DAY
        self.transport.json.side_effect = self.hold
        self.schedule.tick(); self.assertTrue(self.entered.wait(5))
        original = self.schedule._read()
        self.now[0] -= 2
        restarted = PublicSchedule(self.workspace, self.tasks, clock=lambda: self.now[0])
        restarted.recover()
        state = restarted.state()
        self.assertEqual((state['status'], state['code'], state['next_due']),
                         ('paused', 'clock_rollback', None))
        self.assertEqual(state['active_task_id'], original['active_task_id'])
        self.assertEqual(state['history'], [])
        restarted.tick(); self.assertTrue(self.tasks._cancel.is_set())
        self.release.set(); self.assertEqual(self.wait()['status'], 'cancelled')
        restarted.tick()
        state = restarted.state()
        self.assertEqual((state['status'], state['code']), ('paused', 'clock_rollback'))
        self.assertEqual(len(state['history']), 1)
        self.assertEqual(state['history'][0]['status'], 'cancelled')
        self.assertEqual(self.transport.json.call_count, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
