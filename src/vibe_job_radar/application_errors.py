"""Safe application errors shared by business services and their adapters."""


class InputError(ValueError):
    """An actionable error safe to display to the user."""
