"""Original scheduled outcomes survive replacement, without replay or private data."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from vibe_job_radar.public_outcomes import PublicOutcomes,RETAINED,FIELDS
from vibe_job_radar.workspace import InputError


def terminal(**changes):
    return dict(id='a'*32,attempt=1,kind='search',resume_binding='b'*64,
                status='completed',report_id='c'*32,stale=False,**changes)


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)/'public_tasks';self.outcomes=PublicOutcomes(self.root)

    def test_reads_create_nothing_and_receipt_excludes_queries_urls_messages_and_credentials(self):
        self.assertIsNone(self.outcomes.get('a'*32));self.assertFalse(self.root.exists())
        self.root.mkdir()
        task=terminal(query={'query':'PRIVATE_QUERY'},message='PRIVATE_MESSAGE',cookie='PRIVATE_COOKIE',source_url='https://private.example')
        self.outcomes.remember(task);receipt=self.outcomes.get(task['id'])
        self.assertEqual(set(receipt),FIELDS);self.assertEqual(receipt['report_id'],task['report_id'])
        raw=self.outcomes.path.read_bytes()
        for private in (b'PRIVATE_QUERY',b'PRIVATE_MESSAGE',b'PRIVATE_COOKIE',b'private.example'):
            self.assertNotIn(private,raw)
        self.outcomes.remember(task)
        self.assertEqual(self.outcomes.get(task['id']),receipt)

    def test_attempts_are_distinct_and_only_last_64_terminal_attempts_are_retained(self):
        self.root.mkdir();first={**terminal(),'status':'cancelled','report_id':''}
        self.outcomes.remember(first);self.outcomes.remember({**terminal(),'attempt':2})
        self.assertEqual(self.outcomes.get('a'*32,1)['status'],'cancelled')
        self.assertEqual(self.outcomes.get('a'*32,2)['status'],'completed')
        for index in range(RETAINED):self.outcomes.remember({**terminal(),'id':f'{index:032x}'})
        self.assertIsNone(self.outcomes.get('a'*32,1));self.assertIsNone(self.outcomes.get('a'*32,2))
        self.assertEqual(self.outcomes.get(f'{RETAINED-1:032x}')['status'],'completed')
        with closing(sqlite3.connect(self.outcomes.path)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0],RETAINED)

    def test_nonterminal_is_not_a_receipt_and_existing_outcome_cannot_be_rewritten(self):
        self.outcomes.remember({'status':'running'});self.assertFalse(self.root.exists())
        self.root.mkdir();self.outcomes.remember(terminal());before=self.outcomes.get('a'*32)
        with self.assertRaises(InputError):self.outcomes.remember({**terminal(),'status':'failed'})
        self.assertEqual(self.outcomes.get('a'*32),before)
        for change in ({'id':'bad'},{'attempt':True},{'attempt':0},{'kind':'unknown'},
                       {'report_id':''},{'report_id':'bad'},{'resume_binding':'bad'}):
            with self.subTest(change=change),self.assertRaises(InputError):
                self.outcomes.remember({**terminal(),**change})
        self.assertEqual(self.outcomes.get('a'*32),before)

    def test_corrupt_duplicate_mismatched_and_future_payloads_fail_without_fabricating_success(self):
        self.root.mkdir();self.outcomes.remember(terminal());valid=self.outcomes.get('a'*32)
        invalid=[json.dumps({**valid,'schema_version':2}),json.dumps({**valid,'id':'d'*32}),
                 json.dumps({**valid,'attempt':2}),json.dumps({**valid,'stale':None}),
                 json.dumps(valid)[:-1]+',"status":"failed"}', 'x'*2049, '['*1000+']'*1000]
        for raw in invalid:
            with closing(sqlite3.connect(self.outcomes.path)) as conn:
                conn.execute('UPDATE outcomes SET payload=?',(raw,));conn.commit()
            with self.subTest(length=len(raw)),self.assertRaises(InputError):self.outcomes.get('a'*32)
        with closing(sqlite3.connect(self.outcomes.path)) as conn:conn.execute('PRAGMA user_version=99')
        before=self.outcomes.path.read_bytes()
        with self.assertRaises(InputError):self.outcomes.remember({**terminal(),'id':'d'*32})
        self.assertEqual(self.outcomes.path.read_bytes(),before)

    def test_oversized_receipt_database_is_not_replaced(self):
        self.root.mkdir();self.outcomes.path.write_bytes(b'X'*(1024*1024+1))
        with self.assertRaises(InputError):self.outcomes.get('a'*32)
        with self.assertRaises(InputError):self.outcomes.remember(terminal())
        self.assertEqual(self.outcomes.path.stat().st_size,1024*1024+1)

    def test_symlink_database_is_not_followed_or_replaced(self):
        self.root.mkdir();target=self.root/'unrelated';target.write_bytes(b'keep')
        try:self.outcomes.path.symlink_to(target)
        except OSError:self.skipTest('symlink not available')
        with self.assertRaises(InputError):self.outcomes.get('a'*32)
        with self.assertRaises(InputError):self.outcomes.remember(terminal())
        self.assertEqual(target.read_bytes(),b'keep')


if __name__=='__main__':unittest.main()
