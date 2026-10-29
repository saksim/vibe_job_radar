"""Map the controlled fixtures' symbolic IP at the native dial boundary."""
from unittest.mock import patch


def mapped_native_connect(destination):
    # Keep the production ownership/cancellation path, changing only the
    # artificial destination. This never relaxes production network policy.
    from vibe_job_radar.guided.native_connect import connect

    def dial(address, *args, **kwargs):
        target = destination() if address == ('93.184.216.34', 443) else address
        if target[0] not in ('127.0.0.1', '::1'):
            raise AssertionError('controlled native fixture attempted external connection')
        return connect(target, *args, **kwargs)

    return patch('vibe_job_radar.guided.native_connect.connect', side_effect=dial)


def observed_native_proxy(proxy_address, dials):
    """Retain proxy fixtures' dial evidence and their no-direct-traffic guard."""
    from vibe_job_radar.guided.native_connect import connect

    def dial(address, *args, **kwargs):
        if address != proxy_address or address[0] not in ('127.0.0.1', '::1'):
            raise AssertionError('controlled native proxy fixture attempted direct connection')
        dials.append(address)
        return connect(address, *args, **kwargs)

    return patch('vibe_job_radar.guided.native_connect.connect', side_effect=dial)
