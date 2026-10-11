"""Local controls for hosted guard, DNS wire handling and exact lease cleanup."""
import copy
import json
import os
from pathlib import Path
import socket
import struct
import sys
import unittest
from unittest.mock import Mock, patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import wpad_dns_protocol as protocol
import wpad_dns_acceptance as acceptance

NAMES=['wpad','wpad.fixture.invalid']
TAG='vjr-wpad-'+'a'*32

def rule(name='{00000000-0000-0000-0000-000000000001}',**change):
    return {'Name':name,'Comment':TAG,'DisplayName':TAG,'Namespace':NAMES[:],
            'NameServers':['127.0.0.1'],**change}

def query(name='wpad.fixture.invalid',kind=1,*,extra=b''):
    labels=b''.join(bytes([len(p)])+p.encode('ascii') for p in name.split('.'))+b'\0'
    return struct.pack('!6H',17,0x100,1,0,0,int(bool(extra)))+labels+struct.pack('!2H',kind,1)+extra

class DnsProtocolTests(unittest.TestCase):
    def test_a_aaaa_and_unowned_queries_never_forward(self):
        for kind,answer_count,category in ((1,1,'owned_a'),(28,0,'owned_aaaa'),(15,0,'unsupported_type')):
            response,actual=protocol.dns_reply(query(kind=kind),set(NAMES))
            with self.subTest(kind=kind):
                self.assertEqual(actual,category)
                self.assertEqual(struct.unpack('!6H',response[:12])[3],answer_count)
                if kind==1:
                    self.assertEqual(response[-4:],b'\x7f\0\0\x01')
                    self.assertEqual(struct.unpack('!I',response[-10:-6])[0],0)
        response,category=protocol.dns_reply(query('SECRET.invalid'),set(NAMES))
        self.assertEqual(category,'other_name');self.assertEqual(response[3]&15,3)

    def test_oversized_truncated_compressed_and_multiple_questions_are_refused(self):
        original=query()
        malformed=[b'',original[:16],original+b'extra',b'x'*4097,
                   original[:12]+b'\xc0\x0c'+original[-4:],
                   original[:4]+b'\0\2'+original[6:],
                   original[:2]+b'\x81\0'+original[4:]]
        for wire in malformed:
            with self.subTest(length=len(wire)):
                self.assertEqual(protocol.dns_reply(wire,set(NAMES)),(None,'malformed'))

    def test_bounded_edns_and_case_insensitive_query(self):
        opt=b'\0'+struct.pack('!HHIH',41,1232,0,0)
        response,kind=protocol.dns_reply(query('WPAD.FIXTURE.INVALID',extra=opt),set(NAMES))
        self.assertEqual(kind,'owned_a')
        self.assertEqual(struct.unpack('!6H',response[:12])[-1],0)
        self.assertEqual(protocol.dns_reply(query(extra=opt+b'x'),set(NAMES)),(None,'malformed'))

    def test_real_loopback_udp_tcp_serve_and_release_both_owned_sockets(self):
        dns=protocol.LoopbackDns(NAMES,port=0).start()
        try:
            udp=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
            with udp:
                udp.settimeout(3);udp.sendto(query(),('127.0.0.1',dns.ports['udp']))
                actual,_=udp.recvfrom(4096)
                self.assertEqual(actual,protocol.dns_reply(query(),set(NAMES))[0])
            with socket.create_connection(('127.0.0.1',dns.ports['tcp']),timeout=3) as tcp:
                wire=query(kind=28);tcp.sendall(struct.pack('!H',len(wire))+wire)
                size=struct.unpack('!H',dns._exact(tcp,2))[0]
                actual=dns._exact(tcp,size)
                self.assertEqual(actual,protocol.dns_reply(wire,set(NAMES))[0])
        finally:dns.close()
        state=dns.snapshot()
        self.assertEqual(state['queries'],{'udp_owned_a':1,'tcp_owned_aaaa':1})
        self.assertEqual(state['errors'],0);self.assertTrue(state['listeners_closed'])
        self.assertEqual(state['live_threads'],0);self.assertEqual(state['forwarded_queries'],0)
        self.assertNotIn('fixture.invalid',json.dumps(state))
        self.assertEqual(len(dns.sockets),2)
        self.assertEqual([sock.fileno() for sock in dns.sockets],[-1,-1])
        self.assertTrue(all(not thread.is_alive() for thread in dns.threads))
        # A closed TCP connection may retain TIME_WAIT on POSIX. Verify the
        # owned descriptors above; immediate rebinding is only valid for UDP.
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
            sock.bind(('127.0.0.1',dns.ports['udp']))

    def test_ephemeral_udp_port_is_not_assumed_available_to_tcp(self):
        # Model a TCP reservation at the number assigned to UDP. All successful
        # binds and the subsequent DNS exchanges still use real loopback sockets.
        real_socket=socket.socket;udp_port=None;attempts=[]
        class ReservedTcpPort:
            def __init__(self,family,kind):
                self.inner=real_socket(family,kind);self.kind=kind
            def __getattr__(self,name):return getattr(self.inner,name)
            def bind(self,address):
                nonlocal udp_port
                attempts.append((self.kind,address))
                if self.kind==socket.SOCK_STREAM and address[1]==udp_port:
                    raise PermissionError(10013,'UDP-selected number is unavailable to TCP')
                self.inner.bind(address)
                if self.kind==socket.SOCK_DGRAM:udp_port=self.inner.getsockname()[1]
        dns=protocol.LoopbackDns(NAMES,port=0)
        with patch.object(protocol.socket,'socket',side_effect=ReservedTcpPort):
            dns.start()
        try:
            self.assertEqual(attempts,[(socket.SOCK_DGRAM,('127.0.0.1',0)),
                                      (socket.SOCK_STREAM,('127.0.0.1',0))])
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as udp:
                udp.settimeout(3);udp.sendto(query(),('127.0.0.1',dns.ports['udp']))
                actual,_=udp.recvfrom(4096)
                self.assertEqual(actual,protocol.dns_reply(query(),set(NAMES))[0])
            with socket.create_connection(('127.0.0.1',dns.ports['tcp']),timeout=3) as tcp:
                wire=query(kind=28);tcp.sendall(struct.pack('!H',len(wire))+wire)
                size=struct.unpack('!H',dns._exact(tcp,2))[0]
                self.assertEqual(dns._exact(tcp,size),protocol.dns_reply(wire,set(NAMES))[0])
        finally:dns.close()
        self.assertEqual(dns.snapshot()['queries'],{'udp_owned_a':1,'tcp_owned_aaaa':1})
        self.assertEqual(dns.snapshot()['errors'],0)
        self.assertTrue(dns.snapshot()['listeners_closed'])
        self.assertEqual([sock.fileno() for sock in dns.sockets],[-1,-1])
        self.assertTrue(all(not thread.is_alive() for thread in dns.threads))

    def test_fixed_dns_port_failure_closes_both_sockets_before_serving(self):
        # The actual hosted fixture must still use UDP and TCP port 53. A denied
        # fixed port fails once; it must not fall back to another port or serve
        # a partially initialized DNS listener.
        udp=Mock();tcp=Mock();failure=PermissionError(10013,'fixed port denied')
        udp.getsockname.return_value=('127.0.0.1',53)
        tcp.bind.side_effect=failure
        dns=protocol.LoopbackDns(NAMES)
        with patch.object(protocol.socket,'socket',side_effect=[udp,tcp]) as factory,\
                patch.object(protocol.threading,'Thread') as thread:
            with self.assertRaises(PermissionError) as raised:dns.start()
            self.assertIs(raised.exception,failure)
            thread.assert_not_called()
        self.assertEqual(factory.call_count,2)
        udp.bind.assert_called_once_with(('127.0.0.1',53))
        tcp.bind.assert_called_once_with(('127.0.0.1',53))
        udp.close.assert_called_once_with();tcp.close.assert_called_once_with()
        self.assertTrue(dns.stop.is_set());self.assertTrue(dns.closed)
        self.assertEqual(dns.threads,[])

    def test_only_bounded_exact_wpad_names_are_accepted(self):
        for bad in (['wpad'],['wpad','*.invalid'],['wpad','.fixture.invalid'],
                    ['wpad','example.invalid'],['wpad','wpad.fixture.invalid']*2,
                    ['wpad','wpad.'+'a'*64+'.invalid']):
            with self.subTest(names=bad),self.assertRaises(ValueError):protocol.validate_names(bad)
        self.assertEqual(protocol.validate_names(NAMES),NAMES)


class OwnershipTests(unittest.TestCase):
    def test_cleanup_failure_retains_only_finite_action_and_source_location(self):
        error=RuntimeError('SECRET raw host failure')
        error.detail={'code':'host_operation_failed','line':77,'message':'SECRET','id':'SECRET'}
        self.assertEqual(protocol.cleanup_error_facts('remove',error),
                         {'action':'remove','code':'host_operation_failed','line':77})
        error.detail={'code':'SECRET','line':True}
        self.assertEqual(protocol.cleanup_error_facts('rules',error),
                         {'action':'rules','code':'cleanup_operation_failed'})

    def test_malformed_rule_values_never_authorize_delete_or_break_finite_diagnostics(self):
        rows=[rule(Name=['SECRET']),rule(Namespace=[1,'SECRET']),None,rule(Comment='SECRET')]*3
        facts=protocol.rule_facts(rows,TAG,NAMES,set())
        self.assertEqual(facts['count'],12);self.assertEqual(len(facts['rows']),8)
        self.assertNotIn('SECRET',json.dumps(facts))
        self.assertTrue(all(not protocol.owned_rule(r,TAG,NAMES,set()) for r in rows))
        self.assertEqual(protocol.rule_facts('SECRET',TAG,NAMES,set()),{'list':False,'rows':[]})

    def test_partial_add_then_transport_failure_removes_exact_owned_rule(self):
        rows=[];calls=[]
        def operation(action,payload):
            calls.append(action)
            if action=='rules':return copy.deepcopy(rows)
            if action=='add':
                rows.append(rule());raise RuntimeError('output failed after mutation')
            if action=='remove':
                self.assertEqual(payload,{'tag':TAG,'names':NAMES,'id':rule()['Name']})
                rows.clear();return {'removed':True}
        lease=protocol.NrptLease(operation,TAG,NAMES,[])
        with self.assertRaises(RuntimeError):lease.acquire()
        lease.close()
        self.assertEqual(rows,[]);self.assertTrue(lease.cleanup['restored'])
        self.assertEqual(lease.cleanup['removed'],1)
        self.assertEqual(calls,['add','rules','remove','rules'])

    def test_malformed_add_receipt_is_reconciled_without_trusting_returned_id(self):
        rows=[]
        def operation(action,payload):
            if action=='rules':return copy.deepcopy(rows)
            if action=='add':rows.append(rule());return {'Name':'unowned'}
            if action=='remove':rows.clear()
        lease=protocol.NrptLease(operation,TAG,NAMES,[])
        with self.assertRaises(ValueError):lease.acquire()
        lease.close();self.assertTrue(lease.cleanup['restored'])

    def test_other_tags_namespace_servers_and_original_ids_are_never_owned(self):
        for change in ({'Comment':'other'},{'DisplayName':'other'},{'Namespace':['.invalid']},
                       {'Namespace':NAMES+['another.invalid']},{'NameServers':['192.0.2.1']}):
            with self.subTest(change=change):self.assertFalse(protocol.owned_rule(rule(**change),TAG,NAMES,set()))
        self.assertFalse(protocol.owned_rule(rule(),TAG,NAMES,{rule()['Name']}))
        with self.assertRaises(ValueError):protocol.NrptLease(lambda *a:None,TAG,NAMES,[rule()])

    def test_cleanup_attempts_all_owned_rules_and_keeps_unknown_concurrent_rule(self):
        first=rule()
        second=rule(name='{00000000-0000-0000-0000-000000000002}')
        unknown=rule(name='{00000000-0000-0000-0000-000000000003}',Comment='other')
        rows=[first,second,unknown];removed=[]
        def operation(action,payload):
            if action=='rules':return copy.deepcopy(rows)
            if action=='remove':
                removed.append(payload['id'])
                if payload['id']==first['Name']:raise RuntimeError('cannot remove first')
                rows[:]=[r for r in rows if r['Name']!=payload['id']]
        lease=protocol.NrptLease(operation,TAG,NAMES,[]);lease.close()
        self.assertEqual(removed,[first['Name'],second['Name']])
        self.assertEqual(rows,[first,unknown])
        self.assertFalse(lease.cleanup['restored']);self.assertEqual(lease.cleanup['errors'],1)

    def test_fixture_attempts_every_cleanup_without_replacing_primary_error(self):
        class Component:
            def __init__(self,name):self.name=name
            def close(self):calls.append(self.name);raise RuntimeError('cleanup')
        calls=[];fixture=acceptance.DnsWpadFixture(lambda *a:None)
        fixture.lease=Component('lease');fixture.http=Component('http');fixture.dns=Component('dns')
        fixture.__exit__(ValueError,ValueError('primary'),None)
        self.assertEqual(calls,['lease','http','dns'])
        self.assertEqual(fixture.cleanup_errors,3);self.assertEqual(fixture.primary_error,'acceptance_failed')

    def test_nonhosted_guard_runs_before_any_host_operation_or_listener(self):
        for key in protocol.HOSTED_ENV:
            environment={**protocol.HOSTED_ENV,key:'wrong'}
            with self.subTest(key=key),patch.dict(os.environ,environment,clear=True),\
                    patch.object(acceptance,'host_operation') as operation,\
                    patch.object(acceptance,'LoopbackDns') as dns,\
                    patch.object(acceptance,'OwnedHttp') as http:
                with self.assertRaises(ValueError):acceptance.DnsWpadFixture(lambda *a:None).__enter__()
                operation.assert_not_called();dns.assert_not_called();http.assert_not_called()

    def test_partial_setup_failure_is_closed_before_returning(self):
        class FailedLease:
            def __init__(self,*args):pass
            def acquire(self):raise RuntimeError('receipt failure')
            def close(self):calls.append('lease')
        class Component:
            def __init__(self,*args):pass
            def start(self):return self
            def close(self):calls.append('component')
        calls=[]
        state={'admin':True,'policy_allowed':True,'service_enabled':True,'rules':[],'names':NAMES}
        with patch.object(acceptance,'require_hosted'),patch.object(acceptance,'host_operation',return_value=state),\
                patch.object(acceptance,'NrptLease',FailedLease),patch.object(acceptance,'LoopbackDns',Component),\
                patch.object(acceptance,'OwnedHttp',Component):
            fixture=acceptance.DnsWpadFixture(lambda *a:None)
            with self.assertRaises(RuntimeError):fixture.__enter__()
        self.assertTrue(fixture.closed);self.assertEqual(calls,['lease','component','component'])


class ApiGateTests(unittest.TestCase):
    def case(self,*,dns=True,source=True,implicit_fetch=False,owner='workbench',target=False):
        from types import SimpleNamespace
        counts={};requests=[0];calls=[]
        fixture=SimpleNamespace(
            dns=SimpleNamespace(snapshot=lambda:{'queries':dict(counts)}),
            http=SimpleNamespace(snapshot=lambda:{'requests':requests[0]}),
            expected_ids=lambda:{'a'*64})
        trace=SimpleNamespace(stage=lambda name:None,snapshot=lambda:{
            'requests_observed':requests[0],'events_dropped':0,'root_registration_failures':0,
            'events':[{'connection_owner_scope':owner,'declared_client':'project_pac'}]})
        state={'proxy_mode':'auto','schema_version':2,'revision':0,
               'wpad':{'discovery_running':False,'code':'wpad_not_discovered'}}
        def call(path,data=None):
            calls.append(path)
            if path=='/api/network/state':return copy.deepcopy(state)
            if path=='/api/network/wpad/discover':
                if dns:counts['udp_owned_a']=1
                if implicit_fetch:requests[0]+=1
                state['wpad']={'discovery_running':False,'code':'wpad_source_ready',
                               'config_id':('a' if source else 'b')*64,'candidate':'c'*32}
                return copy.deepcopy(state)
            if path=='/api/network/wpad':
                state.update(schema_version=5,proxy_mode='wpad',revision=1)
                return copy.deepcopy(state)
            if path=='/api/network/pac/check':
                requests[0]+=1
                return {'passed':True,'transport':'loopback_socks5_proxy',
                        'target_requested':target,'code':'pac_evaluated'}
            if path=='/api/network/proxy':
                state.update(schema_version=2,proxy_mode='auto',revision=2)
                return copy.deepcopy(state)
            raise AssertionError('unexpected fixture API')
        return SimpleNamespace(json=call),fixture,trace,calls

    def test_unobserved_dns_or_foreign_source_never_reaches_trust_or_download(self):
        for options in ({'dns':False},{'source':False},{'implicit_fetch':True}):
            with self.subTest(options=options):
                app,fixture,trace,calls=self.case(**options);evidence={}
                with self.assertRaises(AssertionError):acceptance.verify_app(app,fixture,trace,evidence)
                self.assertFalse(evidence['success'])
                self.assertNotIn('/api/network/wpad',calls)
                self.assertNotIn('/api/network/pac/check',calls)

    def test_unknown_request_owner_or_target_connection_cannot_qualify(self):
        for options in ({'owner':'unknown'},{'owner':'other_process'},{'target':True}):
            with self.subTest(options=options):
                app,fixture,trace,calls=self.case(**options);evidence={}
                with self.assertRaises(AssertionError):acceptance.verify_app(app,fixture,trace,evidence)
                self.assertFalse(evidence['success']);self.assertFalse(evidence['rolled_back'])

    def test_known_fixture_flow_requires_both_permissions_and_rollback(self):
        app,fixture,trace,calls=self.case();evidence={}
        acceptance.verify_app(app,fixture,trace,evidence)
        self.assertTrue(evidence['success']);self.assertTrue(evidence['rolled_back'])
        self.assertLess(calls.index('/api/network/wpad/discover'),calls.index('/api/network/wpad'))
        self.assertLess(calls.index('/api/network/wpad'),calls.index('/api/network/pac/check'))
        self.assertEqual(calls[-1],'/api/network/proxy')

if __name__=='__main__':unittest.main(verbosity=2)
