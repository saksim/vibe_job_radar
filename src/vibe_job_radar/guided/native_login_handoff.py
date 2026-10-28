"""One explicit bridge-to-native login handoff, preserving the original task."""
from copy import deepcopy

from ..utils import utc_now
from ..workspace import InputError
from .checkpoint import binding, ensure_compatible
from .liepin_login_policy import native_human_contract


def validate(state, adapter, browser):
    if state.get('platform') != 'liepin' or state.get('backend', 'bridge') != 'bridge':
        raise InputError('此交接入口仅用于猎聘的原有桥接任务。')
    if browser not in {'msedge', 'chrome'}:
        raise InputError('请先选择本机 Edge 或 Chrome；人工登录交接使用程序专用的独立浏览器。')
    ensure_compatible(state, adapter)
    native_human_contract(adapter)
    if state.get('native_login_handoff') is not None:
        raise InputError('此任务已有交接记录，请继续原任务。')


def prepare(state, adapter, browser):
    validate(state, adapter, browser)
    changed = deepcopy(state)
    changed['native_login_handoff'] = {
        'version': 1, 'from_backend': 'bridge', 'to_backend': 'native',
        'at': utc_now(), 'previous_binding': deepcopy(state.get('execution_binding')),
    }
    changed.update(backend='native', human_login=True, session_reused=False,
                   authentication='not_checked', login_continuation='off')
    # Old in-memory bridge cookies cannot be donated to the new controller.
    # Existing persistent snapshots remain in their original backend partition.
    changed.pop('session_source_task', None)
    changed['execution_binding'] = binding(changed, adapter)
    return changed
