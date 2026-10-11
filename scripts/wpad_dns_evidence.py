"""Strict finite evidence for actual frozen DNS WPAD, including restoration."""
def _zero(value):
    return type(value) is int and value==0


def _one(value):
    return type(value) is int and value==1


def valid_wpad_dns_evidence(value):
    if (not isinstance(value,dict) or value.get('success') is not True
            or value.get('kind')!='frozen-dns-wpad-acceptance'
            or value.get('frozen_executable') is not True):
        return False
    api=value.get('api');fixture=value.get('fixture');trace=value.get('request_trace')
    if not all(isinstance(v,dict) for v in (api,fixture,trace)):return False
    if (api.get('stage')!='complete' or api.get('discovery_code')!='wpad_source_ready'
            or api.get('check_code')!='pac_evaluated'
            or not all(api.get(k) is True for k in ('success','real_dns_wpad','source_matched',
                'offline_grant','script_checked','rolled_back','check_passed'))):
        return False
    if (fixture.get('stage')!='ready' or fixture.get('restored') is not True
            or fixture.get('primary_error') is not None or fixture.get('host_error') is not None
            or not _zero(fixture.get('cleanup_errors'))
            or fixture.get('adapter_dns_or_suffix_changes') is not False
            or fixture.get('global_proxy_or_security_changes') is not False):
        return False
    dns=fixture.get('dns');http=fixture.get('http');nrpt=fixture.get('nrpt_cleanup')
    if not all(isinstance(v,dict) for v in (dns,http,nrpt)):return False
    queries=dns.get('queries')
    if (not isinstance(queries,dict) or not queries
            or any(k not in {'udp_owned_a','tcp_owned_a','udp_owned_aaaa','tcp_owned_aaaa'}
                   or type(n) is not int or not 0<n<=128 for k,n in queries.items())
            or not 1<=sum(v for k,v in queries.items() if k.endswith('_owned_a'))<=128
            or not _zero(dns.get('errors')) or not _zero(dns.get('forwarded_queries'))
            or not _zero(dns.get('live_threads')) or dns.get('listeners_closed') is not True):
        return False
    cleanup=http.get('cleanup')
    if (not _one(http.get('requests')) or http.get('observer_failed') is not False
            or http.get('protocol_failed') is not False or http.get('child_closed') is not True
            or http.get('reader_closed') is not True or not isinstance(cleanup,dict)
            or cleanup.get('success') is not True or cleanup.get('stage')!='complete'
            or cleanup.get('error') is not None or not _zero(cleanup.get('cleanup_errors'))
            or not _one(cleanup.get('requests')) or cleanup.get('listener_closed') is not True
            or cleanup.get('existing_listener_preserved') is not True):
        return False
    if (nrpt.get('attempted') is not True or nrpt.get('restored') is not True
            or not _one(nrpt.get('removed')) or not _zero(nrpt.get('errors'))
            or fixture.get('nrpt_cleanup_failures')!=[]):
        return False
    for key in ('nrpt_add_facts','nrpt_cleanup_facts'):
        facts=fixture.get(key)
        if (not isinstance(facts,dict) or facts.get('list') is not True
                or not _one(facts.get('count')) or not isinstance(facts.get('rows'),list)
                or len(facts['rows'])!=1 or not isinstance(facts['rows'][0],dict)):
            return False
        row=facts['rows'][0]
        if (any(row.get(k) is not True for k in
                ('id_present','comment_match','display_name_match','names_match','servers_match','owned'))
                or row.get('original_identity') is not False):
            return False
    events=trace.get('events')
    if (not _one(trace.get('requests_observed')) or not _zero(trace.get('events_dropped'))
            or not _zero(trace.get('root_registration_failures'))
            or not isinstance(events,list) or len(events)!=1 or not isinstance(events[0],dict)):
        return False
    return (events[0].get('connection_owner_scope')=='workbench'
            and events[0].get('owner_relation') in {'same_process','descendant_snapshot'}
            and events[0].get('declared_client')=='project_pac'
            and events[0].get('stage')=='ui_check')
