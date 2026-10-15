"""Release must require protocol and credential proof from the frozen program."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from ntlm_proxy_acceptance import COMPONENT_FIELDS, FIELDS, valid_component, valid_evidence


def good_ntlm_proof():
    component = dict(schema_version=1, success=True, library='pyspnego:0.12.4',
        frozen_executable=True, network_scope='offline-artificial-protocol-no-proxy-connection',
        code='verified', **{key: True for key in COMPONENT_FIELDS})
    return dict(component=component, **{key: True for key in FIELDS})


class NtlmEvidenceTests(unittest.TestCase):
    def test_missing_component_or_restart_proofs_cannot_qualify_release(self):
        self.assertTrue(valid_evidence(good_ntlm_proof()))
        for key in FIELDS:
            damaged = good_ntlm_proof(); damaged[key] = False
            self.assertFalse(valid_evidence(damaged))
        for key in good_ntlm_proof()['component']:
            damaged = good_ntlm_proof(); damaged['component'].pop(key)
            self.assertFalse(valid_evidence(damaged))
        for change in ({'frozen_executable': False}, {'schema_version': True},
                       {'library': 'other'}, {'code': 'failure'},
                       {'network_scope': 'real enterprise proxy certified'},
                       *({key: 1} for key in COMPONENT_FIELDS)):
            self.assertFalse(valid_evidence({**good_ntlm_proof(),
                'component': {**good_ntlm_proof()['component'], **change}}))

    def test_builder_requires_structured_worker_proof_beside_flag(self):
        import build_windows_portable as builder
        from test_native_component_portable import good_component
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary); (bundle / 'VibeJobRadar.exe').write_bytes(b'artificial fixture')
            report = dict(success=True, verified_browser='bundled', files=builder.inventory(bundle),
                          native_components={c: good_component(c) for c in ('bundled', 'chrome')})
            for value in ({}, {'ntlm_proxy_worker_verified': True},
                          {'ntlm_proxy_worker_verified': True, 'ntlm_proxy': {'component': True}}):
                with self.assertRaises(ValueError):
                    builder.validate_runtime_evidence(bundle, {**report, **value}, require_ntlm=True)
            builder.validate_runtime_evidence(bundle, {**report, 'ntlm_proxy_worker_verified': True,
                'ntlm_proxy': good_ntlm_proof()}, require_ntlm=True)
