"""CI-only evidence around unchanged native retry acceptance (Issue111/PR112)."""
from unittest.mock import patch
import run_native_read_retry as acceptance
from run_native_auth_probe import ObservedBackend
from native_wait_diagnostics import Stages, capture, observed_backend, require_ci


def main():
    require_ci()
    stages = Stages()
    out = acceptance.ROOT / 'browser-acceptance' / 'native-read-retry'
    with capture(stages, out, 'retry-wait'), \
            patch.object(acceptance, 'NativeBackend', observed_backend(ObservedBackend, stages)):
        acceptance.main()


if __name__ == '__main__':
    main()
