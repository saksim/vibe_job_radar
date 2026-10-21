"""Artificial imports only: parsing integrity and atomic workbench writes."""
import csv
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout

from vibe_job_radar.cli import main
from vibe_job_radar.ingest import iter_items
from vibe_job_radar.models import JobRecord
from vibe_job_radar.store import Store
from vibe_job_radar.workspace import InputError, Workspace


class ImportIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def items(self, content, suffix='.csv'):
        source = self.root / ('input' + suffix)
        source.write_text(content, encoding='utf-8', newline='')
        return list(iter_items(source))

    def assert_rejected(self, content, suffix='.csv'):
        items = self.items(content, suffix)
        self.assertTrue(items)
        self.assertFalse(any(isinstance(item, JobRecord) for _, item in items))
        self.assertTrue(all(isinstance(item, ValueError) for _, item in items))

    def test_csv_extra_cells_cannot_truncate_complete_jd(self):
        self.assert_rejected('title,text\nArchitect,First requirement,Second requirement\n')

    def test_csv_duplicate_and_alias_headers_cannot_overwrite(self):
        for header in ('title,text,text', 'title,text,职位描述'):
            with self.subTest(header=header):
                self.assert_rejected(header + '\nArchitect,First,Second\n')

    def test_csv_missing_cells_in_ignored_columns_are_still_invalid(self):
        self.assert_rejected('title,text,spreadsheet_note\nArchitect,Full text\n')

    def test_csv_unclosed_quote_is_an_error(self):
        self.assert_rejected('title,text\nArchitect,"First requirement\nSecond requirement')

    def test_long_valid_csv_jd_has_the_same_limit_as_json(self):
        body = 'X' * 140000
        items = self.items('title,text\nArchitect,' + body + '\n')
        self.assertEqual(len(items), 1)
        self.assertIsInstance(items[0][1], JobRecord)
        self.assertEqual(items[0][1].text, body)

    def test_valid_quoted_chinese_columns_and_extra_column_are_preserved(self):
        buffer = io.StringIO(newline='')
        writer = csv.writer(buffer)
        writer.writerow(['职位名称', '职位描述', 'spreadsheet_note'])
        body = 'First requirement, with a comma\nSecond requirement'
        writer.writerow(['架构师', body, 'not a JobRecord field'])
        items = self.items(buffer.getvalue())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0][1].text, body)
        self.assertEqual(items[0][1].title, '架构师')

    def test_json_duplicate_fields_and_aliases_cannot_overwrite(self):
        for suffix in ('.json', '.jsonl'):
            for content in ('{"title":"Architect","text":"First","text":"Second"}',
                            '{"title":"Architect","text":"First","职位描述":"Second"}'):
                with self.subTest(suffix=suffix, content=content):
                    self.assert_rejected(content, suffix)

    def test_csv_error_does_not_hide_later_cli_input(self):
        folder = self.root / 'inputs'
        folder.mkdir()
        (folder / 'a.csv').write_text('title,text\nArchitect,"unterminated', encoding='utf-8')
        (folder / 'b.json').write_text('{"title":"Architect","text":"Valid later JD"}', encoding='utf-8')
        db = self.root / 'cli.sqlite'
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(['ingest', str(folder), '--db', str(db)])
        self.assertEqual(code, 2)
        with Store(db) as store:
            self.assertEqual([job.text for job in store.records()], ['Valid later JD'])
        errors = json.loads(db.with_suffix('.ingest_errors.json').read_text(encoding='utf-8'))
        self.assertEqual(len(errors), 1)

    def workspace(self):
        workspace = Workspace(self.root / 'workspace')
        with Store(workspace.db) as store:
            store.add(JobRecord(title='Existing', text='Previous data', source_ref='existing'))
            store.event('previous', 'ok', {})
        return workspace

    def snapshot(self, workspace):
        with closing(sqlite3.connect(workspace.db)) as connection, connection:
            return {table: connection.execute('SELECT * FROM ' + table + ' ORDER BY rowid').fetchall()
                    for table in ('records', 'observations', 'events')}

    def request(self):
        return {'filename':'batch.json', 'content':json.dumps([
                    {'title':'First', 'text':'First new JD'},
                    {'title':'Second', 'text':'Second new JD'}]),
                'rights_note':'Artificial offline test', 'full_text_confirmed':True}

    def test_workbench_rejects_malformed_csv_without_importing_any_row(self):
        workspace = self.workspace()
        before = self.snapshot(workspace)
        request = self.request()
        request.update(filename='batch.csv', content='title,text\nFirst,Valid JD\nSecond,Part one,Part two\n')
        with self.assertRaises(InputError):
            workspace.import_file(request)
        self.assertEqual(self.snapshot(workspace), before)
        self.assertEqual(list((workspace.root / 'imports').iterdir()), [])

    def check_write_failure(self, trigger):
        workspace = self.workspace()
        with closing(sqlite3.connect(workspace.db)) as connection, connection:
            connection.execute(trigger)
        before = self.snapshot(workspace)
        try:
            workspace.import_file(self.request())
        except (InputError, sqlite3.Error) as exc:
            failure = exc
        else:
            self.fail('The injected database failure was not reached')
        self.assertEqual(self.snapshot(workspace), before, 'failed import left a partial batch')
        self.assertIsInstance(failure, InputError, 'database failures need an actionable UI error')

    def test_second_observation_failure_rolls_back_entire_import(self):
        self.check_write_failure("""CREATE TRIGGER reject_second BEFORE INSERT ON observations
            WHEN (SELECT count(*) FROM observations) = 2
            BEGIN SELECT RAISE(ABORT, 'artificial second observation failure'); END""")

    def test_import_event_failure_also_rolls_back_records_and_observations(self):
        self.check_write_failure("""CREATE TRIGGER reject_import_event BEFORE INSERT ON events
            WHEN NEW.action = 'workbench_import'
            BEGIN SELECT RAISE(ABORT, 'artificial event failure'); END""")

    def test_blank_csv_lines_preserve_existing_logical_source_references(self):
        items = self.items('title,text\n\nFirst,First JD\n\nSecond,Second JD\n')
        self.assertEqual([ref.rsplit(':', 1)[1] for ref, _ in items], ['2', '3'])
        self.assertEqual([item.source_ref for _, item in items], [ref for ref, _ in items])

    def test_failed_batch_can_be_retried_with_original_observation_counts(self):
        workspace = self.workspace()
        with closing(sqlite3.connect(workspace.db)) as connection, connection:
            connection.execute("""CREATE TRIGGER reject_import BEFORE INSERT ON events
                WHEN NEW.action = 'workbench_import'
                BEGIN SELECT RAISE(ABORT, 'artificial failure'); END""")
        with self.assertRaises(InputError):
            workspace.import_file(self.request())
        with closing(sqlite3.connect(workspace.db)) as connection, connection:
            connection.execute('DROP TRIGGER reject_import')
        self.assertEqual(workspace.import_file(self.request())['new_snapshots'], 2)
        self.assertEqual(workspace.import_file(self.request())['new_snapshots'], 0)
        snapshot = self.snapshot(workspace)
        self.assertEqual(len(snapshot['records']), 3)
        self.assertEqual(len(snapshot['observations']), 5)
        self.assertEqual(len(snapshot['events']), 3)
        first, second = (json.loads(event[-1]) for event in snapshot['events'][1:])
        self.assertEqual((first['observations'], first['new_snapshots']), (2, 2))
        self.assertEqual((second['observations'], second['new_snapshots']), (2, 0))
