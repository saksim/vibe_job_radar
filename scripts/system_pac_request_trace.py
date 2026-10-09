"""Bounded diagnostics for an owned loopback PAC test server only.

Connection-owner facts are observations, not proof of the historical caller.
No request URL, header value, PID, process name or command is serialized.
"""
from __future__ import annotations
import ctypes
import os
import socket
import sys
import threading
import time

STAGES=frozenset({'setup','ui_check','cached_worker_prepare','cached_worker_running','cached_worker_finished','restore','finished'})
ROOTS=frozenset({'verifier','workbench','cached_worker'})


def windows_connection_owner(peer, local):
    if os.name!='nt' or peer[0]!='127.0.0.1' or local[0]!='127.0.0.1':
        return None
    from ctypes import wintypes as w
    class Row(ctypes.Structure):
        _fields_=[(n,w.DWORD) for n in ('state','local_addr','local_port','remote_addr','remote_port','pid')]
    class Table(ctypes.Structure):
        _fields_=[('count',w.DWORD),('rows',Row*1)]
    api=ctypes.WinDLL('iphlpapi',use_last_error=True).GetExtendedTcpTable
    api.argtypes=[ctypes.c_void_p,ctypes.POINTER(w.DWORD),w.BOOL,w.ULONG,ctypes.c_int,w.ULONG]
    api.restype=w.DWORD
    size=w.DWORD()
    if api(None,ctypes.byref(size),False,socket.AF_INET,5,0) not in (0,122):return None
    capacity=size.value+65536
    if capacity>1048576:return None
    data=ctypes.create_string_buffer(capacity);size.value=capacity
    if api(data,ctypes.byref(size),False,socket.AF_INET,5,0)!=0:return None
    count=w.DWORD.from_buffer(data).value;offset=Table.rows.offset;stride=ctypes.sizeof(Row)
    if offset+count*stride>min(size.value,capacity):return None
    matches=[]
    for i in range(count):
        row=Row.from_buffer(data,offset+i*stride)
        ip=lambda v:socket.inet_ntoa(int(v).to_bytes(4,sys.byteorder))
        if (ip(row.local_addr),socket.ntohs(row.local_port&65535))==peer and (ip(row.remote_addr),socket.ntohs(row.remote_port&65535))==local:
            matches.append(row.pid)
    return matches[0] if len(matches)==1 and matches[0]>0 else None


def windows_creation_time(pid):
    if os.name!='nt':return None
    from ctypes import wintypes as w
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];api.OpenProcess.restype=w.HANDLE
    api.GetProcessTimes.argtypes=[w.HANDLE,*([ctypes.POINTER(w.FILETIME)]*4)];api.GetProcessTimes.restype=w.BOOL
    api.CloseHandle.argtypes=[w.HANDLE];api.CloseHandle.restype=w.BOOL
    handle=api.OpenProcess(0x1000,False,pid)
    if not handle:return None
    try:
        values=[w.FILETIME() for _ in range(4)]
        if not api.GetProcessTimes(handle,*(ctypes.byref(v) for v in values)):return None
        return (values[0].dwHighDateTime<<32)|values[0].dwLowDateTime
    finally:api.CloseHandle(handle)


def windows_parents():
    if os.name!='nt':return None
    from ctypes import wintypes as w
    class Entry(ctypes.Structure):
        _fields_=[('size',w.DWORD),('usage',w.DWORD),('pid',w.DWORD),('heap',ctypes.c_size_t),
                  ('module',w.DWORD),('threads',w.DWORD),('parent',w.DWORD),('priority',w.LONG),
                  ('flags',w.DWORD),('exe',w.WCHAR*260)]
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.CreateToolhelp32Snapshot.argtypes=[w.DWORD,w.DWORD];api.CreateToolhelp32Snapshot.restype=w.HANDLE
    for name in ('Process32FirstW','Process32NextW'):
        f=getattr(api,name);f.argtypes=[w.HANDLE,ctypes.POINTER(Entry)];f.restype=w.BOOL
    api.CloseHandle.argtypes=[w.HANDLE];api.CloseHandle.restype=w.BOOL
    handle=api.CreateToolhelp32Snapshot(2,0)
    if handle in (None,ctypes.c_void_p(-1).value):return None
    try:
        entry=Entry();entry.size=ctypes.sizeof(entry)
        if not api.Process32FirstW(handle,ctypes.byref(entry)):return None
        result={}
        for _ in range(8192):
            result[entry.pid]=entry.parent
            if not api.Process32NextW(handle,ctypes.byref(entry)):
                return result if ctypes.get_last_error()==18 else None
        return None
    finally:api.CloseHandle(handle)


def owner_scope(pid, roots, creation_time=windows_creation_time, parents=windows_parents):
    """Compare only registered process identities; reject recycled/racing ancestry."""
    if pid is None:return 'unknown','unavailable'
    born=creation_time(pid)
    if born is None:return 'unknown','unavailable'
    chain=None
    seen=set()
    for depth in range(16):
        for role,identity in roots.items():
            if pid==identity[0]:
                if born!=identity[1]:return 'unknown','identity_changed'
                return role,'same_process' if depth==0 else 'descendant_snapshot'
        if depth==0:chain=parents()
        if chain is None or pid not in chain:return 'unknown','unavailable'
        parent=chain[pid]
        if parent==0:return 'other_process','untracked'
        if parent in seen:return 'unknown','ancestry_cycle'
        seen.add(pid)
        parent_born=creation_time(parent)
        if parent_born is None:return 'unknown','unavailable'
        if parent_born>born:return 'unknown','identity_changed'
        pid,born=parent,parent_born
    return 'unknown','ancestry_limit'


class FixtureRequestTrace:
    def __init__(self, *, owner_lookup=windows_connection_owner, creation_time=windows_creation_time,
                 parents=windows_parents, clock=time.monotonic):
        self._owner=owner_lookup;self._creation_time=creation_time;self._parents=parents;self._clock=clock
        self._start=clock();self._lock=threading.Lock();self._stage='setup';self._roots={}
        self._events=[];self._observed=0;self._dropped=0;self._registration_failures=0

    def stage(self, name):
        if name not in STAGES:raise ValueError('unknown fixture stage')
        with self._lock:self._stage=name

    def register(self, name, pid):
        if name not in ROOTS:raise ValueError('unknown fixture root')
        try:born=self._creation_time(pid)
        except Exception:born=None
        with self._lock:
            if born is None:
                self._registration_failures+=1;self._roots.pop(name,None)
            else:self._roots[name]=(pid,born)

    def observe(self, peer, local, user_agent):
        with self._lock:
            self._observed+=1
            if len(self._events)>=16:
                self._dropped+=1;return
            event={'sequence':self._observed,'stage':self._stage,
                   'elapsed_ms':min(3600000,max(0,round((self._clock()-self._start)*1000))),
                   'declared_client':'project_pac' if user_agent=='VibeJobRadar-PAC' else 'missing' if not user_agent else 'other',
                   'connection_owner_scope':'unknown','owner_relation':'unavailable'}
            self._events.append(event)
            roots=dict(self._roots)
        try:
            pid=self._owner(peer,local)
            scope,relation=owner_scope(pid,roots,self._creation_time,self._parents)
        except Exception:scope,relation='unknown','lookup_failed'
        with self._lock:event.update(connection_owner_scope=scope,owner_relation=relation)

    def snapshot(self):
        with self._lock:
            return {'events':[dict(e) for e in self._events],'requests_observed':self._observed,
                    'events_dropped':self._dropped,'root_registration_failures':self._registration_failures,
                    'historical_cause_confirmed':False}
