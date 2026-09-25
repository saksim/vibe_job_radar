"""Bounded code locations only: no exception text, locals, paths or source lines."""
from pathlib import Path
import re
import traceback

SCRIPTS = Path(__file__).resolve().parent
ALLOWED = frozenset({'verify_windows_portable.py', 'system_pac_acceptance.py'})


def failure_frames(error):
    frames = []
    for frame, line in traceback.walk_tb(error.__traceback__):
        path = Path(frame.f_code.co_filename)
        if path.name not in ALLOWED or path.resolve().parent != SCRIPTS:
            continue
        name = frame.f_code.co_name
        if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,63}|<module>|<lambda>', name):
            name = '<anonymous>'
        frames.append({'file': path.name, 'function': name, 'line': line})
    return frames[-8:]
