"""Artificial upstream only; original Collector, quota ledger and reports stay real."""
import json
import os
import threading
import unittest
from unittest.mock import patch

from vibe_job_radar.collection import Collector
from vibe_job_radar.collection_runtime import CollectionBusy
from vibe_job_radar.network import SafeHTTP
from vibe_job_radar.workspace import InputError
import test_collection_runtime as runtime
from test_public_category import BODY, Wire, job_url


class BackgroundTests(unittest.TestCase):
    setUp = runtime.RuntimeTests.setUp
    tearDown = runtime.RuntimeTests.tearDown
    runner = runtime.RuntimeTests.runner
    wait = runtime.RuntimeTests.wait
    wire = runtime.RuntimeTests.wire

    def task(self, mode='urls', **changes):
        values = dict(mode=mode, roles=['architect'], platforms=['liepin'], permit_platforms=['liepin'],
                      consent=True, rights_note='Artificial background regression only.',
                      urls=job_url(1), detail_budget=1, search_budget=1, search_storage_rights=True,
                      endpoint='https://feed.example/jobs', contract_ref='https://feed.example/contract', feed_budget=2)
        values.update(changes)
        return self.collector.start(values)

    def start(self, runner, state, **extra):
        return runner.start(dict(id=state['id'], consent=True, **extra))

    def no_saved_secrets(self, runner, *secrets):
        view = json.dumps(runner.state(), ensure_ascii=False)
        for secret in secrets:
            self.assertNotIn(secret, view)
        for path in self.workspace.root.rglob('*'):
            if path.is_file():
                body = path.read_bytes()
                for secret in secrets:
                    self.assertNotIn(secret.encode(), body, str(path))
        self.assertNotIn('_args', runner._thread.__dict__)

    def test_url_batch_exceeds_category_step_limit_and_keeps_quota_and_report(self):
        wire = self.wire()
        state = self.task(urls='\n'.join(job_url(i) for i in range(1, 13)))
        runner = self.runner()
        self.start(runner, state)
        done = self.wait(runner)
        self.assertEqual((done['status'], done['code']), ('needs_attention', 'finished'))
        self.assertEqual([d['status'] for d in done['task']['details']], ['ok'] + ['budget_skipped'] * 11)
        self.assertEqual(done['task']['detail_attempts'], 1)
        self.assertEqual(wire.calls, ['https://www.liepin.com/robots.txt', job_url(1)])
        self.assertEqual(self.ledger.summary('liepin')['page']['day'], 1)
        self.assertEqual(self.ledger.summary('liepin')['request']['day'], 2)
        self.assertTrue((self.workspace.root/'reports'/done['task']['report_id']/'dashboard.html').is_file())

    def test_search_captures_environment_key_once_and_finishes_original_report(self):
        initial, replacement = 'artificial-search-captured-018', 'artificial-search-changed-018'
        observed = []
        def search(url, *, headers):
            observed.append(headers['X-Subscription-Token'])
            os.environ['BRAVE_SEARCH_API_KEY'] = replacement
            return {'web': {'results': [{'url': job_url(1), 'title': '架构师', 'description': BODY}]},
                    'query': {'more_results_available': False}}
        self.wire()
        state = self.task('search', search_budget=2)
        runner = self.runner()
        with patch.dict(os.environ, {'BRAVE_SEARCH_API_KEY': initial}), patch.object(SafeHTTP, 'json', side_effect=search):
            self.start(runner, state)
            done = self.wait(runner)
        self.assertEqual(observed, [initial, initial])
        self.assertEqual(done['code'], 'finished')
        self.assertEqual(done['task']['search_requests'], 2)
        self.assertEqual(done['task']['detail_attempts'], 1)
        self.assertEqual(done['task']['details'][0]['status'], 'ok')
        self.assertTrue(done['task']['report_id'])
        self.no_saved_secrets(runner, initial, replacement)

    def test_search_requires_key_only_in_search_phase_and_manual_step_still_works(self):
        state = self.task('search')
        runner = self.runner()
        self.wire()
        payload = {'web': {'results': [{'url': job_url(1), 'title': '架构师', 'description': BODY}]}}
        with patch.dict(os.environ, {'BRAVE_SEARCH_API_KEY': ''}), patch.object(SafeHTTP, 'json', return_value=payload) as upstream:
            with self.assertRaises(InputError):
                self.start(runner, state)
            self.assertEqual(self.collector.status({'id': state['id']})['search_requests'], 0)
            upstream.assert_not_called()
            self.collector.step({'id': state['id'], 'api_key': 'artificial-manual-search-018'})
            ready = self.collector.step({'id': state['id'], 'api_key': 'artificial-manual-search-018'})
            self.assertEqual(ready['phase'], 'detail')
            self.start(runner, ready)
            done = self.wait(runner)
        self.assertEqual(upstream.call_count, 1)
        self.assertEqual(done['code'], 'finished')
        self.assertEqual(done['task']['details'][0]['status'], 'ok')
        self.assertTrue(done['task']['report_id'])

    def test_feed_pause_restarts_with_new_in_memory_token_and_shared_owner(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        old, new = 'artificial-feed-first-018', 'artificial-feed-second-018'
        requests = []
        def feed(url, *, headers):
            requests.append((url, dict(headers)))
            number = len(requests)
            if number == 1:
                entered.set()
                if not release.wait(10):
                    raise AssertionError('controlled feed response was not released')
            return {'jobs': [{'title': '架构师', 'text': BODY, 'url': job_url(number), 'evidence_level': 'full_text'}],
                    'next_cursor': 'second-page' if number == 1 else ''}
        other_task = self.task()
        state = self.task('feed')
        runner = self.runner()
        with patch.object(SafeHTTP, 'json', side_effect=feed):
            self.start(runner, state, api_key=old)
            self.assertTrue(entered.wait(30))
            observer = self.runner(Collector(self.workspace))
            self.assertTrue(observer.state()['owned_elsewhere'])
            with self.assertRaises(CollectionBusy):
                self.start(observer, other_task)
            with self.assertRaises(CollectionBusy):
                observer.collector.step({'id': other_task['id']})
            self.assertEqual(runner.pause({'id': state['id']})['status'], 'pausing')
            release.set()
            paused = self.wait(runner)
            self.assertEqual(paused['task']['feed_requests'], 1)
            self.assertEqual(paused['task']['feed_cursor'], 'second-page')
            self.no_saved_secrets(runner, old)
            restarted = self.runner(Collector(self.workspace))
            self.assertFalse(restarted.state()['active'])
            self.assertEqual(len(requests), 1)
            self.start(restarted, paused['task'], api_key=new)
            done = self.wait(restarted)
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done['task']['feed_requests'], 2)
        self.assertEqual(requests, [('https://feed.example/jobs', {'Authorization': 'Bearer ' + old}),
                                    ('https://feed.example/jobs?cursor=second-page', {'Authorization': 'Bearer ' + new})])
        self.assertTrue(done['task']['report_id'])
        self.no_saved_secrets(restarted, old, new)

    def test_unknown_feed_outcome_keeps_reserved_attempt_and_never_replays_after_restart(self):
        state = self.task('feed')
        runner = self.runner()
        with patch.object(SafeHTTP, 'json', side_effect=RuntimeError('controlled uncertain outcome')) as upstream:
            self.start(runner, state)
            stopped = self.wait(runner)
            self.assertEqual(stopped['code'], 'interrupted_uncertain')
            self.assertEqual(stopped['task']['feed_requests'], 1)
            self.assertIn('in_flight', stopped['task'])
            with self.assertRaises(InputError):
                self.start(runner, state)
            restarted = self.runner(Collector(self.workspace))
            recovered = restarted.collector.status({'id': state['id']})
            self.assertEqual(recovered['feed_outcomes'][0]['status'], 'interrupted_uncertain')
            self.assertEqual(upstream.call_count, 1)
            self.start(restarted, recovered)
            done = self.wait(restarted)
        self.assertEqual(upstream.call_count, 1)
        self.assertEqual(done['status'], 'needs_attention')
        self.assertEqual(done['task']['feed_requests'], 1)

    def test_changed_configuration_stops_before_second_detail(self):
        wire = Wire()
        original = wire.public_get
        def fetch(url, **kwargs):
            response = original(url, **kwargs)
            if url == job_url(1):
                # A different process may replace this persisted configuration.
                self.collector.platform_config.write_text(json.dumps({'platforms': self.workspace.config['platforms']}),
                                                          encoding='utf-8')
            return response
        wire.public_get = fetch
        self.wire(wire)
        state = self.task(urls=job_url(1) + '\n' + job_url(2), detail_budget=2)
        runner = self.runner()
        self.start(runner, state)
        stopped = self.wait(runner)
        self.assertEqual(stopped['code'], 'conditions_changed')
        self.assertEqual(stopped['task']['detail_attempts'], 1)
        self.assertEqual([d['status'] for d in stopped['task']['details']], ['ok', 'pending'])
        self.assertNotIn(job_url(2), wire.calls)

    def test_corrupted_noncategory_scope_is_rejected_without_upstream_requests(self):
        runner = self.runner()
        with patch.object(SafeHTTP, 'json') as upstream:
            for mode, changes in [('urls', {'detail_budget': 301}), ('search', {'search_budget': 301}),
                                   ('feed', {'feed_budget': 21}), ('urls', {'phase': 'feed'})]:
                with self.subTest(mode=mode, changes=changes):
                    state = self.task(mode)
                    saved = self.collector._load(state['id'])
                    saved.update(changes)
                    self.collector._save(saved)
                    with self.assertRaises(InputError):
                        self.start(runner, state, api_key='artificial-scope-018')
            upstream.assert_not_called()


if __name__ == '__main__':
    unittest.main()
