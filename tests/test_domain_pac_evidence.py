"""A flag cannot replace the frozen app's domain input and scope proof."""
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from domain_pac_acceptance import valid_evidence,ENGINE,FIELDS


def good_domain_proof():
    return dict(schema_version=1,engine=ENGINE,frozen_executable=True,
        target_requests=0,target_request_evidence='api-check-response-flags',**{key:True for key in FIELDS})


class DomainPacEvidenceTests(unittest.TestCase):
    def test_missing_or_incomplete_scope_proof_is_rejected(self):
        self.assertTrue(valid_evidence(good_domain_proof()))
        for key in good_domain_proof():
            proof=good_domain_proof();proof.pop(key);self.assertFalse(valid_evidence(proof),key)
        for change in ({'schema_version':True},{'engine':'other'},{'frozen_executable':False},
                {'target_requests':1},{'target_requests':False},{'target_request_evidence':'unmeasured'},
                *({key:False} for key in FIELDS),*({key:1} for key in FIELDS)):
            self.assertFalse(valid_evidence({**good_domain_proof(),**change}),change)

    def test_builder_requires_actual_domain_proof_alongside_flag(self):
        import build_windows_portable as builder
        from test_native_component_portable import good_component
        with tempfile.TemporaryDirectory() as tmp:
            bundle=Path(tmp);(bundle/'VibeJobRadar.exe').write_bytes(b'authored fixture')
            report=dict(success=True,verified_browser='bundled',files=builder.inventory(bundle),
                native_components={c:good_component(c) for c in ('bundled','chrome')})
            for change in ({},{'domain_pac_worker_verified':True},
                    {'domain_pac_worker_verified':1,'domain_pac':good_domain_proof()},
                    {'domain_pac_worker_verified':True,'domain_pac':{**good_domain_proof(),'canonical_root_matched':False}}):
                with self.assertRaises(ValueError):builder.validate_runtime_evidence(bundle,{**report,**change},require_domain_pac=True)
            builder.validate_runtime_evidence(bundle,{**report,'domain_pac_worker_verified':True,
                'domain_pac':good_domain_proof()},require_domain_pac=True)
