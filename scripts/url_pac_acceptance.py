"""Actual app API checks for isolated full-URL PAC; authored .invalid inputs."""
ENGINE = 'quickjs-ng:0.17.0.1'


def valid_evidence(value, *, require_frozen=True):
    if not isinstance(value, dict) or set(value) != {
            'schema_version','engine','frozen_executable','scope_confirmed',
            'path_query_host_matched','calendar_helpers_checked','calendar_invalid_rejected',
            'unsupported_helper_rejected','target_requests','target_request_evidence','rolled_back'}:
        return False
    return (type(value['schema_version']) is int and value['schema_version']==2
        and value['engine']==ENGINE and type(value['frozen_executable']) is bool
        and (not require_frozen or value['frozen_executable'] is True)
        and all(value[key] is True for key in ('scope_confirmed','path_query_host_matched',
                                               'calendar_helpers_checked','calendar_invalid_rejected',
                                               'unsupported_helper_rejected','rolled_back'))
        and type(value['target_requests']) is int and value['target_requests']==0
        and value['target_request_evidence']=='api-check-response-flags')


def verify_app(app, *, frozen_executable=False):
    current=app.json('/api/network/state')
    if current['proxy_mode']!='auto' or current.get('pac_url_available') is not True:
        raise AssertionError('URL PAC test needs inactive settings and actual optional component')
    scripted=('const OriginalDate=Date;'
        'Date=function(){return new OriginalDate("2024-02-29T00:00:05.000Z");};'
        'function FindProxyForURL(url,host){'
        'if(url!=="https://pac-check.invalid/path?probe=explicit"||host!=="pac-check.invalid"'
        '||typeof dnsResolve!=="undefined"||typeof fetch!=="undefined"'
        '||!weekdayRange("THU","GMT")||weekdayRange("FRI","MON","GMT")'
        '||!dateRange(29,"FEB",29,"FEB","GMT")||dateRange("MAR","GMT")'
        '||!timeRange(23,1,"GMT")||timeRange(1,2,"GMT"))return "UNKNOWN wrong-input:9; DIRECT";'
        'return "SOCKS5 127.0.0.1:19081; DIRECT";}')
    proof=dict(schema_version=2,engine=ENGINE,frozen_executable=frozen_executable,
               scope_confirmed=False,path_query_host_matched=False,
               calendar_helpers_checked=False,calendar_invalid_rejected=False,
               unsupported_helper_rejected=False,target_requests=0,
               target_request_evidence='api-check-response-flags',rolled_back=False)
    try:
        imported=app.json('/api/network/pac-url',dict(name='authored-url.pac',script=scripted,
            revision=current['revision'],consent=True))
        assert imported['schema_version']==6 and imported['proxy_mode']=='pac_url'
        assert imported['network_tested'] is False
        proof['scope_confirmed']=True
        checked=app.json('/api/network/pac/check',{'revision':imported['revision']})
        assert checked['passed'] is True and checked['target_requested'] is False
        assert checked['transport']=='loopback_socks5_proxy'
        proof['path_query_host_matched']=True
        proof['calendar_helpers_checked']=True
        imported=app.json('/api/network/pac-url',dict(name='authored-invalid-calendar.pac',
            script='function FindProxyForURL(url,host){dateRange(31,"APR",31,"APR");return "DIRECT";}',
            revision=imported['revision'],consent=True))
        checked=app.json('/api/network/pac/check',{'revision':imported['revision']})
        assert checked['passed'] is False and checked['code']=='pac_invalid_script'
        assert checked['target_requested'] is False
        proof['calendar_invalid_rejected']=True
        imported=app.json('/api/network/pac-url',dict(name='authored-unsupported-helper.pac',
            script='function FindProxyForURL(url,host){dnsResolve(host);return "DIRECT";}',
            revision=imported['revision'],consent=True))
        checked=app.json('/api/network/pac/check',{'revision':imported['revision']})
        assert checked['passed'] is False and checked['code']=='pac_invalid_script'
        assert checked['target_requested'] is False
        proof['unsupported_helper_rejected']=True
    finally:
        latest=app.json('/api/network/state')
        rollback=app.json('/api/network/proxy',dict(mode='auto',endpoint='',consent=False,
            revision=latest['revision']))
        assert rollback['schema_version']==2 and rollback['proxy_mode']=='auto'
        proof['rolled_back']=True
    assert valid_evidence(proof,require_frozen=frozen_executable)
    return proof
