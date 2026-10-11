"""Bounded loopback DNS and exact NRPT ownership for the hosted WPAD fixture."""
from __future__ import annotations
import os
import re
import socket
import struct
import threading

HOSTED_ENV = {'CI':'true','GITHUB_ACTIONS':'true','RUNNER_ENVIRONMENT':'github-hosted',
              'RUNNER_OS':'Windows','GITHUB_REPOSITORY':'saksim/vibe_job_radar'}
LABEL = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
NAME = re.compile(r'wpad(?:\.'+LABEL+r'){2,}')
TAG = re.compile(r'vjr-wpad-[0-9a-f]{32}')


def require_hosted():
    if os.name != 'nt' or any(os.environ.get(k) != v for k,v in HOSTED_ENV.items()):
        raise ValueError('hosted Windows WPAD fixture required')


def validate_names(names):
    if (not isinstance(names,list) or not 2 <= len(names) <= 9
            or any(not isinstance(n,str) or len(n)>253 or
                   (n!='wpad' and NAME.fullmatch(n) is None) for n in names)
            or len(set(names))!=len(names) or 'wpad' not in names):
        raise ValueError('invalid exact WPAD namespaces')
    return sorted(names)


def namespace_match(value,names):
    return (isinstance(value,list) and all(isinstance(n,str) for n in value)
            and sorted(value)==sorted(names))


def owned_rule(row, tag, names, original_ids):
    """A returned ID alone never authorizes deletion."""
    return bool(isinstance(row,dict) and isinstance(row.get('Name'),str)
            and row['Name'] and row['Name'] not in original_ids
            and row.get('Comment')==tag and row.get('DisplayName')==tag
            and namespace_match(row.get('Namespace'),names)
            and row.get('NameServers')==['127.0.0.1'])


def rule_facts(rows,tag,names,original_ids):
    """Finite mismatches, never actual rule IDs, values or namespace text."""
    if not isinstance(rows,list):return {'list':False,'rows':[]}
    return {'list':True,'count':len(rows),'rows':[
        {'id_present':isinstance(r.get('Name'),str) and bool(r['Name']),
         'comment_match':r.get('Comment')==tag,'display_name_match':r.get('DisplayName')==tag,
         'names_match':namespace_match(r.get('Namespace'),names),
         'servers_match':r.get('NameServers')==['127.0.0.1'],
         'server_value_kinds':[k if k in ('ip_address','string','other') else 'unrecognized'
                               for k in r.get('NameServerValueKinds',[])[:8]]
                              if isinstance(r.get('NameServerValueKinds'),list) else [],
         'original_identity':isinstance(r.get('Name'),str) and r['Name'] in original_ids,
         'owned':owned_rule(r,tag,names,original_ids)}
        if isinstance(r,dict) else {'mapping':False} for r in rows[:8]]}


def cleanup_error_facts(action,error):
    facts={'action':action,'code':'cleanup_operation_failed'}
    detail=getattr(error,'detail',None)
    if isinstance(detail,dict):
        if detail.get('code') in ('host_operation_failed','host_operation_transport_failed'):
            facts['code']=detail['code']
        if type(detail.get('line')) is int and 0<detail['line']<2000:facts['line']=detail['line']
    return facts


class NrptLease:
    def __init__(self, operation, tag, names, before):
        self.operation=operation
        if TAG.fullmatch(tag) is None:raise ValueError('invalid ownership tag')
        self.tag=tag;self.names=validate_names(names);self.before=before
        # This fixture deliberately supports only the observed policy-free
        # ephemeral host, never adapting an existing enterprise NRPT policy.
        if before:raise ValueError('existing NRPT policy is outside fixture scope')
        self.original_ids=set();self.attempted=False;self.add_facts=None;self.cleanup_facts=None;self.cleanup_failures=[]
        self.cleanup={'attempted':False,'removed':0,'errors':0,'restored':False}

    def acquire(self):
        self.attempted=True  # mutation can precede missing/malformed output
        rows=self.operation('add',{'tag':self.tag,'names':self.names})
        self.add_facts=rule_facts(rows,self.tag,self.names,self.original_ids)
        if not isinstance(rows,list) or len(rows)!=1 or not owned_rule(rows[0],self.tag,self.names,self.original_ids):
            raise ValueError('NRPT addition not confirmed')

    def close(self):
        if self.cleanup['attempted']:return
        self.cleanup['attempted']=True
        try:rows=self.operation('rules',{})
        except Exception as error:
            self.cleanup['errors']+=1
            self.cleanup_failures.append(cleanup_error_facts('rules',error))
            return
        self.cleanup_facts=rule_facts(rows,self.tag,self.names,self.original_ids)
        if not isinstance(rows,list):
            self.cleanup['errors']+=1
            return
        # Reconcile even when Add succeeded before transport/output failed.
        for row in rows:
            if owned_rule(row,self.tag,self.names,self.original_ids):
                try:
                    self.operation('remove',{'tag':self.tag,'names':self.names,'id':row['Name']})
                    self.cleanup['removed']+=1
                except Exception as error:
                    self.cleanup['errors']+=1
                    if len(self.cleanup_failures)<8:self.cleanup_failures.append(cleanup_error_facts('remove',error))
        try:self.cleanup['restored']=self.operation('rules',{})==self.before
        except Exception as error:
            self.cleanup['errors']+=1
            if len(self.cleanup_failures)<8:self.cleanup_failures.append(cleanup_error_facts('rules',error))


def dns_reply(packet, names):
    """No forwarding, recursion, compression expansion, or arbitrary query logs."""
    if not isinstance(packet,bytes) or not 17<=len(packet)<=4096:
        return None,'malformed'
    ident,flags,qd,an,ns,ar=struct.unpack('!6H',packet[:12])
    if flags & ~0x0110 or qd!=1 or an or ns or ar>1:return None,'malformed'
    offset=12;labels=[]
    try:
        while True:
            size=packet[offset];offset+=1
            if not size:break
            if size>63 or offset+size>len(packet):return None,'malformed'
            labels.append(packet[offset:offset+size].decode('ascii').lower())
            offset+=size
            if len(labels)>127:return None,'malformed'
        name='.'.join(labels)
        if not name or len(name)>253 or offset+4>len(packet):return None,'malformed'
        qtype,qclass=struct.unpack('!2H',packet[offset:offset+4]);offset+=4
        question=packet[12:offset]
        if ar:
            # Optional bounded EDNS OPT with an uncompressed root owner.
            if offset+11>len(packet) or packet[offset:offset+3]!=b'\0\0)':
                return None,'malformed'
            length=struct.unpack('!H',packet[offset+9:offset+11])[0]
            if offset+11+length!=len(packet):return None,'malformed'
        elif offset!=len(packet):return None,'malformed'
    except (IndexError,UnicodeError,struct.error):
        return None,'malformed'
    answer=b'';rcode=0;category='owned_aaaa' if qtype==28 else 'owned_a'
    if name not in names:
        rcode=3;category='other_name'
    elif qclass!=1 or qtype not in (1,28):
        rcode=5;category='unsupported_type'
    elif qtype==1:
        # TTL zero: no fixture address should be cached after its lease.
        answer=b'\xc0\x0c'+struct.pack('!HHIH',1,1,0,4)+socket.inet_aton('127.0.0.1')
    header=struct.pack('!6H',ident,0x8400|(flags&0x0100)|rcode,1,int(bool(answer)),0,0)
    return header+question+answer,category


class LoopbackDns:
    def __init__(self,names,*,port=53):
        self.names=frozenset(validate_names(names));self.stop=threading.Event()
        self.lock=threading.Lock();self.counts={};self.errors=0;self.closed=False
        self.sockets=[];self.threads=[];self.port=port

    def start(self):
        try:
            for transport,kind in (('udp',socket.SOCK_DGRAM),('tcp',socket.SOCK_STREAM)):
                sock=socket.socket(socket.AF_INET,kind);self.sockets.append(sock)
                if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):
                    sock.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
                sock.bind(('127.0.0.1',self.port))
                if self.port==0:self.port=sock.getsockname()[1]
                sock.settimeout(.2)
                if kind==socket.SOCK_STREAM:sock.listen(4)
                thread=threading.Thread(target=self._serve,args=(sock,transport),daemon=True)
                self.threads.append(thread);thread.start()
        except BaseException:
            self.close();raise
        return self

    def _reply(self,data,transport):
        reply,category=dns_reply(data,self.names)
        with self.lock:
            key=transport+'_'+category
            self.counts[key]=self.counts.get(key,0)+1
            if sum(self.counts.values())>128:
                self.errors+=1
                self.stop.set()
                return None
        return reply

    @staticmethod
    def _exact(conn,size):
        data=b''
        while len(data)<size:
            part=conn.recv(size-len(data))
            if not part:raise ValueError('truncated fixture DNS frame')
            data+=part
        return data

    def _serve(self,sock,transport):
        while not self.stop.is_set():
            try:
                if transport=='udp':
                    data,peer=sock.recvfrom(4097)
                    if peer[0]!='127.0.0.1':continue
                    reply=self._reply(data,transport)
                    if reply is not None:sock.sendto(reply,peer)
                else:
                    conn,peer=sock.accept()
                    with conn:
                        conn.settimeout(1)
                        if peer[0]!='127.0.0.1':continue
                        size=struct.unpack('!H',self._exact(conn,2))[0]
                        if not 17<=size<=4096:
                            self._reply(b'',transport);continue
                        reply=self._reply(self._exact(conn,size),transport)
                        if reply is not None:conn.sendall(struct.pack('!H',len(reply))+reply)
            except (socket.timeout,TimeoutError):
                continue
            except (OSError,ValueError):
                if not self.stop.is_set():
                    with self.lock:self.errors+=1

    def close(self):
        self.stop.set()
        for sock in self.sockets:sock.close()
        for thread in self.threads:thread.join(2)
        self.closed=all(not t.is_alive() for t in self.threads)

    def snapshot(self):
        with self.lock:
            return {'queries':dict(self.counts),'errors':self.errors,'listeners_closed':self.closed,
                    'live_threads':sum(t.is_alive() for t in self.threads),'forwarded_queries':0}
