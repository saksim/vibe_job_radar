"""Frozen DNS WPAD needs observed, attributed and fully restored evidence."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from wpad_dns_evidence import valid_wpad_dns_evidence


def good_evidence():
    """Authored schema control, never actual runtime qualification."""
    match={'list':True,'count':1,'rows':[{'id_present':True,'comment_match':True,
        'display_name_match':True,'names_match':True,'servers_match':True,'owned':True,
        'original_identity':False}]}
    return {'kind':'frozen-dns-wpad-acceptance','frozen_executable':True,'success':True,
        'api':{'success':True,'stage':'complete','real_dns_wpad':True,'source_matched':True,
               'offline_grant':True,'script_checked':True,'rolled_back':True,'check_passed':True,
               'discovery_code':'wpad_source_ready','check_code':'pac_evaluated'},
        'fixture':{'stage':'ready','restored':True,'primary_error':None,'host_error':None,
            'cleanup_errors':0,'adapter_dns_or_suffix_changes':False,'global_proxy_or_security_changes':False,
            'dns':{'queries':{'udp_owned_a':3},'errors':0,'forwarded_queries':0,'live_threads':0,'listeners_closed':True},
            'http':{'requests':1,'observer_failed':False,'protocol_failed':False,'child_closed':True,
                    'reader_closed':True,'cleanup':{'success':True,'stage':'complete','error':None,
                        'cleanup_errors':0,'requests':1,'listener_closed':True,'existing_listener_preserved':True}},
            'nrpt_cleanup':{'attempted':True,'restored':True,'removed':1,'errors':0},
            'nrpt_cleanup_failures':[],'nrpt_add_facts':copy.deepcopy(match),'nrpt_cleanup_facts':copy.deepcopy(match)},
        'request_trace':{'requests_observed':1,'events_dropped':0,'root_registration_failures':0,
            'events':[{'connection_owner_scope':'workbench','owner_relation':'descendant_snapshot',
                       'declared_client':'project_pac','stage':'ui_check'}]}}


class EvidenceTests(unittest.TestCase):
    def test_complete_authored_schema_is_recognized_and_source_only_is_refused(self):
        proof=good_evidence();self.assertTrue(valid_wpad_dns_evidence(proof))
        for change in ({'kind':'hosted-source-dns-wpad-control'},{'frozen_executable':False},
                       {'success':False},{'success':1},{'api':None},{'fixture':None},{'request_trace':None}):
            with self.subTest(change=change):
                self.assertFalse(valid_wpad_dns_evidence({**proof,**change}))
        self.assertFalse(valid_wpad_dns_evidence(None))

    def test_every_business_phase_is_required(self):
        for key in ('success','real_dns_wpad','source_matched','offline_grant','script_checked','rolled_back','check_passed'):
            for bad in (None,False,1):
                proof=good_evidence();proof['api'][key]=bad
                with self.subTest(key=key,bad=bad):self.assertFalse(valid_wpad_dns_evidence(proof))

    def test_no_queries_or_foreign_or_malformed_dns_cannot_qualify(self):
        for queries in ({},{'udp_owned_aaaa':1},{'udp_owned_a':True},{'udp_owned_a':1.0},
                        {'udp_owned_a':0},{'udp_owned_a':129},{'udp_owned_a':1,'udp_other_name':1},
                        {'udp_owned_a':1,'tcp_malformed':1},None):
            proof=good_evidence();proof['fixture']['dns']['queries']=queries
            with self.subTest(queries=queries):self.assertFalse(valid_wpad_dns_evidence(proof))

    def test_request_owner_and_count_must_be_observed_without_loss(self):
        for field,value in (('connection_owner_scope','unknown'),('connection_owner_scope','verifier'),
                            ('owner_relation','unavailable'),('declared_client','other'),('stage','setup')):
            proof=good_evidence();proof['request_trace']['events'][0][field]=value
            with self.subTest(field=field,value=value):self.assertFalse(valid_wpad_dns_evidence(proof))
        for field,value in (('requests_observed',True),('requests_observed',2),('events_dropped',1),
                            ('root_registration_failures',1),('events',[])):
            proof=good_evidence();proof['request_trace'][field]=value
            with self.subTest(field=field,value=value):self.assertFalse(valid_wpad_dns_evidence(proof))

    def test_success_before_cleanup_and_numeric_boolean_substitution_are_refused(self):
        changes=[
            (('restored',),False),(('cleanup_errors',),1),(('primary_error',),'acceptance_failed'),
            (('nrpt_cleanup','removed'),0),(('nrpt_cleanup','errors'),1),
            (('nrpt_cleanup','restored'),False),(('nrpt_cleanup_failures',),[{'action':'remove'}]),
            (('http','child_closed'),False),(('http','reader_closed'),False),
            (('http','protocol_failed'),True),(('http','observer_failed'),True),
            (('http','requests'),2),(('http','requests'),True),
            (('http','cleanup','existing_listener_preserved'),False),
            (('http','cleanup','success'),False),(('http','cleanup','requests'),0),
            (('dns','errors'),1),(('dns','live_threads'),1),(('dns','forwarded_queries'),1),
            (('dns','listeners_closed'),False),(('dns','errors'),False)]
        for path,value in changes:
            proof=good_evidence();row=proof['fixture']
            for part in path[:-1]:row=row[part]
            row[path[-1]]=value
            with self.subTest(path=path,value=value):self.assertFalse(valid_wpad_dns_evidence(proof))

    def test_exact_ownership_is_required_before_and_during_cleanup(self):
        for phase in ('nrpt_add_facts','nrpt_cleanup_facts'):
            for key in ('id_present','comment_match','display_name_match','names_match','servers_match','owned'):
                proof=good_evidence();proof['fixture'][phase]['rows'][0][key]=False
                with self.subTest(phase=phase,key=key):self.assertFalse(valid_wpad_dns_evidence(proof))
            proof=good_evidence();proof['fixture'][phase]['rows'][0]['original_identity']=True
            self.assertFalse(valid_wpad_dns_evidence(proof))

    def test_builder_and_verifier_opt_in_refuse_nonhosted_before_output_or_process(self):
        import build_windows_portable as builder
        import verify_windows_portable as verifier
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'GITHUB_ACTIONS':'false'}),\
                patch.object(builder,'build_candidate') as build:
            root=Path(temp);out=root/'out'
            with self.assertRaises(ValueError):builder.build(out,root/'source.json',verify_wpad_dns=True)
            self.assertFalse(out.exists());build.assert_not_called()
            with self.assertRaises(ValueError):verifier.verify(root/'absent',root/'report.json',verify_wpad_dns=True)
            self.assertFalse((root/'report.json').exists())

if __name__=='__main__':unittest.main(verbosity=2)
