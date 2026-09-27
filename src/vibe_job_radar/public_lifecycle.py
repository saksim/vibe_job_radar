"""Local cooperative cancellation, separate from provider errors and permissions."""


class PublicTaskCancelled(Exception):
    pass


def check_cancelled(event):
    if event is not None and event.is_set():
        raise PublicTaskCancelled()
