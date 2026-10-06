"""Native artificial-search wait at the workbench's state refresh cadence."""
from __future__ import annotations

import time


def wait_for_guided_job(service):
    """Keep the original 45-second deadline without polling the full ledger at 20 Hz.

    The private busy flag is only a cheap wake-up hint for this test harness.
    Completion and returned job data always come from a fresh public state read.
    """
    deadline = time.monotonic() + 45
    next_state_read = time.monotonic()
    while True:
        with service._lock:
            busy = service._busy
        if not busy or time.monotonic() >= next_state_read:
            state = service.state()
            if not state['busy']:
                return state['jobs'][0]
            next_state_read = time.monotonic() + 2
        if time.monotonic() > deadline:
            raise TimeoutError('native search did not finish')
        time.sleep(.05)
