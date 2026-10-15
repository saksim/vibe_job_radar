"""Wait for an owned fixture service to finish its asynchronous shutdown."""
import time


def close_service(service, observation, *, timeout=50):
    # Match the fixture's existing functional-operation budget. The product's
    # five-second close(), ownership rules and browser deadlines stay intact.
    started = time.monotonic()
    deadline = started + timeout
    # _worker clears service._thread in its finally block before Thread.run
    # actually returns. Retain the owned thread through that last interval.
    with service._lock:
        worker = service._thread
    service.close()
    first = True
    while True:
        with service._lock:
            alive = worker is not None and worker.is_alive()
            observation.update(worker_alive=alive,
                ownership_released=service._ownership.lease is None,
                closure_uncertain=service._closure_uncertain,
                closing_backends=service._closing_backends,
                backend_handles=len(service._backends),
                session_handles=len(service._session_leases),
                elapsed_ms=round((time.monotonic() - started) * 1000))
        if first:
            observation['pending_after_close'] = alive
            first = False
        if observation['closure_uncertain']:
            raise RuntimeError('native fixture service closure is uncertain')
        if not alive:
            if (not observation['ownership_released'] or observation['closing_backends']
                    or observation['backend_handles'] or observation['session_handles']):
                raise RuntimeError('native fixture service retained ownership after worker exit')
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('native fixture service shutdown did not finish')
        worker.join(timeout=min(.05, remaining))
