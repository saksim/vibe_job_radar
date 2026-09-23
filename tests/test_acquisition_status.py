"""Status consistency must not promote fixture evidence into site certification."""
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_workbench import capture
from test_guided import fixture_adapter
from vibe_job_radar.acquisition_status import describe_adapter, snapshot, markdown_table, LEVELS
from vibe_job_radar.guided.adapters import builtins, Registry
from vibe_job_radar.guided.native_policy import contract_for
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]


class AcquisitionStatusTests(unittest.TestCase):
    def test_builtin_availability_and_default_are_separate_from_verification(self):
        for site in snapshot()['sites']:
            self.assertTrue(site['definition_matches_recorded_evidence'])
            self.assertEqual(site['certification'], 'not_live_verified')
            bridge, native = site['backends']
            self.assertTrue(bridge['default'])
            self.assertFalse(native['default'])
            self.assertEqual(native['available'], site['platform'] == 'liepin')
            for row in site['backends']:
                self.assertIn(row['verification_level'], LEVELS)
                self.assertFalse(row['live_verified'])
                self.assertFalse(row['pilot_verified'])
                self.assertFalse(row['user_network_verified'])
                for evidence in row['evidence']:
                    self.assertEqual(evidence['level'], 'controlled_verified')
                    self.assertEqual(len(evidence['source_revision']), 40)
                    self.assertTrue(evidence['browser_os'] and evidence['network'] and evidence['scope'])

    def test_adapter_changes_cannot_inherit_same_key_evidence(self):
        adapter = builtins().get('liepin')
        for changed in (replace(adapter, version='future'), replace(adapter, resource_domains=('other.test',)),
                        replace(adapter, certification='live_verified'), replace(adapter, card_selector='.new-cards')):
            data = describe_adapter(changed)
            self.assertFalse(data['definition_matches_recorded_evidence'])
            self.assertTrue(all(not row['evidence'] for row in data['backends']))
            self.assertEqual(data['certification'], 'not_live_verified')

    def test_contract_change_removes_native_evidence(self):
        adapter = builtins().get('liepin')
        changed = replace(adapter, native_contract=replace(contract_for(adapter), key='new-unverified-contract'))
        native = describe_adapter(changed)['backends'][1]
        self.assertTrue(native['available'])
        self.assertEqual(native['verification_level'], 'implemented')
        self.assertEqual(native['evidence'], [])

    def test_custom_adapter_has_no_automatic_inherited_certification(self):
        registry = Registry([replace(fixture_adapter(), certification='live_verified')])
        data = registry.describe()[0]
        self.assertEqual(data['certification'], data['acquisition']['certification'])
        self.assertEqual(data['certification'], 'not_live_verified')
        self.assertFalse(data['acquisition']['definition_matches_recorded_evidence'])

    def test_service_and_report_use_the_same_offline_catalogue(self):
        with tempfile.TemporaryDirectory() as tmp, patch('socket.create_connection', side_effect=AssertionError('offline')):
            workspace = Workspace(tmp)
            service = GuidedService(workspace)
            try:
                described = [site['acquisition'] for site in service.state()['sites']]
                workspace.add_job(capture())
                report = workspace.analyze({})
                self.assertEqual(described, snapshot()['sites'])
                self.assertEqual(report['manifest']['software_acquisition_capabilities'], snapshot())
                self.assertEqual(report['manifest']['stats']['full_text_job_groups'], 1)
                old_bytes = (workspace.root/'reports'/report['id']/'run_manifest.json').read_bytes()
                workspace.analyze({})
                self.assertEqual((workspace.root/'reports'/report['id']/'run_manifest.json').read_bytes(), old_bytes)
            finally:
                service.close()

    def test_documentation_is_generated_from_registry(self):
        document = (ROOT/'docs/ACQUISITION_CAPABILITIES.md').read_text(encoding='utf-8')
        self.assertIn('\n'+markdown_table()+'\n', document)
        result = subprocess.run([sys.executable, str(ROOT/'scripts/check_acquisition_status.py')],
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
