"""Artificial source data: a public report includes its provenance at publication."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.local_public import LocalPublicDataClient
from vibe_job_radar.public_example import PublicExample
from vibe_job_radar.public_tasks import PublicTasks
from vibe_job_radar.store import Store
from vibe_job_radar.utils import atomic_json
from vibe_job_radar.workspace import Workspace
from test_local_public import payload, query
from test_redirect_recovery import fixture_payload


class PublicReportPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace=Workspace(Path(self.temp.name)/'workspace')

    def run_source(self, kind):
        transport=Mock()
        if kind=='example':
            transport.json.return_value=fixture_payload()
            return PublicExample(self.workspace,transport).run({'consent':True})
        transport.json.return_value=payload()
        client=LocalPublicDataClient(self.workspace,transport=transport)
        tasks=PublicTasks(self.workspace,hybrid_client=client)
        self.addCleanup(tasks.close)
        result=client.search(query(),consent=True)
        return tasks._import(result,query())

    def hook_writes(self, function):
        stack=ExitStack()
        for module in ('pipeline','public_tasks','public_example'):
            stack.enter_context(patch('vibe_job_radar.'+module+'.atomic_json',side_effect=function))
        return stack

    def published(self):
        return [p for p in (self.workspace.root/'reports').iterdir()
                if len(p.name)==32 and all(c in '0123456789abcdef' for c in p.name)]

    def check_source_failure(self,kind):
        def fail(path,value):
            if Path(path).name=='public_source.json':raise OSError('artificial provenance write failure')
            return atomic_json(path,value)
        with self.hook_writes(fail),self.assertRaises(OSError):
            self.run_source(kind)
        self.assertEqual(self.published(),[], 'report became visible before provenance was complete')
        self.assertEqual(list((self.workspace.root/'reports').iterdir()),[])
        with Store(self.workspace.db) as store:
            self.assertGreater(len(store.records()),0,'original source data must remain durable')
        if kind=='example':
            self.assertFalse((self.workspace.root/'public_examples/latest.json').exists(),
                             'a failed report must not be cached as successful')

    def test_hybrid_source_failure_does_not_publish_partial_report(self):
        self.check_source_failure('hybrid')

    def test_example_source_failure_does_not_publish_or_cache_partial_report(self):
        self.check_source_failure('example')

    def check_success(self,kind):
        source_writes=[]
        def inspect(path,value):
            if Path(path).name=='public_source.json':
                source_writes.append(Path(path))
                self.assertEqual(self.published(),[], 'report must stay private while provenance writes')
            return atomic_json(path,value)
        with self.hook_writes(inspect):
            result=self.run_source(kind)
        self.assertEqual(len(source_writes),1)
        self.assertTrue(result['success'])
        folders=self.published()
        self.assertEqual(len(folders),1)
        manifest=json.loads((folders[0]/'run_manifest.json').read_text(encoding='utf-8'))
        self.assertIn('public_source.json',manifest['output_files_sha256'])
        for name,digest in manifest['output_files_sha256'].items():
            self.assertEqual(hashlib.sha256((folders[0]/name).read_bytes()).hexdigest(),digest)
        source=json.loads((folders[0]/'public_source.json').read_text(encoding='utf-8'))
        self.assertEqual(source['report_id'],result['report_id'])
        if kind=='example':self.assertEqual(source['stats'],manifest['stats'])

    def test_hybrid_provenance_is_complete_when_report_first_becomes_visible(self):
        self.check_success('hybrid')

    def test_example_provenance_is_complete_when_report_first_becomes_visible(self):
        self.check_success('example')

    def test_attachments_cannot_replace_core_outputs_or_escape_report(self):
        from vibe_job_radar.models import JobRecord
        from vibe_job_radar.pipeline import analyze
        with Store(self.workspace.db) as store:
            store.add(JobRecord(title='Architect',text='Artificial fixture',source_ref='fixture'))
        for name in ('effective_config.json','run_manifest.json','../outside.json'):
            with self.subTest(name=name),self.assertRaises(ValueError):
                analyze(self.workspace.db,self.workspace.root/'reports'/('a'*32),
                        json_attachments=lambda manifest:{name:{'unexpected':True}})
            self.assertEqual(list((self.workspace.root/'reports').iterdir()),[])
        self.assertFalse((self.workspace.root/'outside.json').exists())
