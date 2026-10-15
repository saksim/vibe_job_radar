"""Frozen URL-PAC proof must include scope, exact input and rejection/rollback."""
import importlib.util
from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from url_pac_acceptance import valid_evidence, ENGINE
from test_native_component_portable import good_component


def good_url_proof():
    return dict(schema_version=3,engine=ENGINE,frozen_executable=True,scope_confirmed=True,
        path_query_host_matched=True,calendar_helpers_checked=True,calendar_invalid_rejected=True,
        unsupported_helper_rejected=True,dns_helpers_checked=True,
        dns_probe_scope='localhost_ipv4_and_helper_exports',target_requests=0,
        target_request_evidence='api-check-response-flags',rolled_back=True)


class UrlPacEvidenceTests(unittest.TestCase):
    def test_strict_complete_evidence_and_frozen_scope(self):
        self.assertTrue(valid_evidence(good_url_proof()))
        for key in good_url_proof():
            value=good_url_proof();value.pop(key)
            self.assertFalse(valid_evidence(value),key)
        for changed in ({'schema_version':True},{'schema_version':1},{'engine':'other'},{'frozen_executable':False},
                        {'scope_confirmed':1},{'path_query_host_matched':False},
                        {'unsupported_helper_rejected':False},{'target_requests':False},
                        {'calendar_helpers_checked':False},{'calendar_helpers_checked':1},
                        {'calendar_invalid_rejected':False},{'calendar_invalid_rejected':1},
                        {'target_requests':1},{'target_request_evidence':'unmeasured'},
                        {'rolled_back':False},{'extra':True}):
            self.assertFalse(valid_evidence({**good_url_proof(),**changed}),changed)
        source={**good_url_proof(),'frozen_executable':False}
        self.assertTrue(valid_evidence(source,require_frozen=False))
        self.assertFalse(valid_evidence(source))

    def test_build_gate_cannot_accept_flag_without_full_frozen_proof(self):
        import tempfile
        spec=importlib.util.spec_from_file_location('url_pac_builder',ROOT/'scripts/build_windows_portable.py')
        builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as tmp:
            bundle=Path(tmp);(bundle/'VibeJobRadar.exe').write_bytes(b'artificial unit fixture')
            report=dict(success=True,verified_browser='bundled',files=builder.inventory(bundle),
                native_components={c:good_component(c) for c in ('bundled','chrome')})
            for changed in ({},{'url_pac_worker_verified':True},
                            {'url_pac_worker_verified':1,'url_pac':good_url_proof()},
                            {'url_pac_worker_verified':True,'url_pac':{**good_url_proof(),'frozen_executable':False}},
                            {'url_pac_worker_verified':True,'url_pac':{**good_url_proof(),'calendar_helpers_checked':False}},
                            {'url_pac_worker_verified':True,'url_pac':{**good_url_proof(),'calendar_invalid_rejected':False}}):
                with self.assertRaises(ValueError):
                    builder.validate_runtime_evidence(bundle,{**report,**changed},require_url_pac=True)
            builder.validate_runtime_evidence(bundle,
                {**report,'url_pac_worker_verified':True,'url_pac':good_url_proof()},require_url_pac=True)
