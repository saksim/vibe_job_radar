"""Fixed startup failure facts; never retain arguments, paths or raw errors."""
import re

STAGES = frozenset({'not_checked', 'environment', 'version', 'import', 'driver',
                    'executable', 'launch', 'context', 'ready'})
ERROR_TYPES = frozenset({'CrawlError', 'Error', 'TargetClosedError', 'TimeoutError',
                         'TimeoutExpired', 'PermissionError', 'FileNotFoundError',
                         'ModuleNotFoundError', 'ImportError', 'OSError',
                         'RuntimeError', 'ValueError', 'KeyError', 'TypeError'})
CAUSE_CODES = frozenset({'native_protocol_error', 'native_observation_limit',
                        'browser_closed', 'playwright_missing',
                        'playwright_driver_failed', 'native_surface_unsupported'})


def startup_failure_facts(value):
    value = value if isinstance(value, dict) else {}
    def fixed(key, allowed, fallback):
        candidate = value.get(key)
        return candidate if isinstance(candidate, str) and candidate in allowed else fallback
    launch = value.get('launch_tested')
    exit_hex = value.get('process_exit_hex')
    return {
        'stage': fixed('stage', STAGES, 'unknown'),
        'error_type': fixed('error_type', ERROR_TYPES, 'other'),
        'cause_code': fixed('cause_code', CAUSE_CODES, ''),
        'launch_tested': launch if type(launch) is bool else None,
        'process_exit_hex': exit_hex if isinstance(exit_hex, str) and
            re.fullmatch(r'0x[0-9A-F]{8}', exit_hex) else '',
    }
