"""Actual app domain-PAC scope proof; checks never connect to a target."""
ENGINE='quickjs-ng:0.17.0.1'
FIELDS=('scope_confirmed','canonical_root_matched','unsupported_dns_rejected',
        'dns_scope_confirmed','dns_helpers_checked','native_scope_enabled','rolled_back')


def valid_evidence(value,*,require_frozen=True):
    return (isinstance(value,dict) and set(value)==set(FIELDS)|{
        'schema_version','engine','frozen_executable','target_requests','target_request_evidence'}
        and type(value['schema_version']) is int and value['schema_version']==1
        and value['engine']==ENGINE and type(value['frozen_executable']) is bool
        and (not require_frozen or value['frozen_executable'] is True)
        and all(value[key] is True for key in FIELDS)
        and type(value['target_requests']) is int and value['target_requests']==0
        and value['target_request_evidence']=='api-check-response-flags')


def verify_app(app,*,frozen_executable=False):
    current=app.json('/api/network/state')
    assert current['proxy_mode']=='auto' and current.get('pac_domain_available') is True
    proof=dict(schema_version=1,engine=ENGINE,frozen_executable=frozen_executable,
        target_requests=0,target_request_evidence='api-check-response-flags',**{key:False for key in FIELDS})
    def save(script,dns=False):
        state=app.json('/api/network/state')
        imported=app.json('/api/network/pac-domain'+('-dns' if dns else ''),dict(
            name='authored-domain.pac',script=script,revision=state['revision'],consent=True))
        assert imported['schema_version']==(10 if dns else 9)
        assert imported['proxy_mode']==('pac_domain_dns' if dns else 'pac_domain')
        assert imported['network_tested'] is False
        assert imported['policy']['native_browser_supported'] is True
        assert imported['policy']['pac_scope']=='explicit_trusted_portable_domain'
        return imported
    def check(imported):
        result=app.json('/api/network/pac/check',{'revision':imported['revision']})
        assert result['target_requested'] is False
        return result
    try:
        imported=save('function FindProxyForURL(url,host){return url==="https://pac-check.invalid/" && host==="pac-check.invalid" && typeof dnsResolve==="undefined" && typeof fetch==="undefined" ? "SOCKS5 127.0.0.1:19081; DIRECT" : "INVALID path-leaked";}')
        proof['scope_confirmed']=True;proof['native_scope_enabled']=True
        checked=check(imported)
        assert checked['passed'] is True and checked['transport']=='loopback_socks5_proxy'
        proof['canonical_root_matched']=True
        imported=save('function FindProxyForURL(url,host){dnsResolve(host);return "DIRECT";}')
        checked=check(imported)
        assert checked['passed'] is False and checked['code']=='pac_invalid_script'
        proof['unsupported_dns_rejected']=True
        imported=save('function FindProxyForURL(url,host){return url==="https://pac-check.invalid/" && host==="pac-check.invalid" && isResolvable("127.0.0.1") && isInNet(dnsResolve("localhost"),"127.0.0.0","255.0.0.0") && typeof myIpAddress==="function" ? "DIRECT" : "INVALID";}',dns=True)
        proof['dns_scope_confirmed']=True
        assert check(imported)['passed'] is True
        proof['dns_helpers_checked']=True
    finally:
        latest=app.json('/api/network/state')
        rollback=app.json('/api/network/proxy',dict(mode='auto',endpoint='',consent=False,revision=latest['revision']))
        assert rollback['proxy_mode']=='auto' and rollback['schema_version']==2
        proof['rolled_back']=True
    assert valid_evidence(proof,require_frozen=frozen_executable)
    return proof
