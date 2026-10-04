"""Explicit per-user login startup for the current Windows portable application."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys

from .collection import writer_lock
from .runtime import is_portable
from .utils import atomic_json
from .workspace import InputError

RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
CONSENT = 'windows-portable-login-v1'
MODE_CONSENT = 'windows-portable-login-v2'
MODES = ('workbench', 'public_worker')
_FIELDS = {'schema_version', 'workspace', 'executable', 'command', 'value_name'}


class StartupChanged(InputError):
    pass


def _digest(value):
    return hashlib.sha256(repr(value).encode('utf-8', errors='surrogatepass')).hexdigest()


def command_line(executable, workspace, mode='workbench'):
    """Quote both fixed arguments for Windows; never invoke a command shell."""
    if type(mode) is not str or mode not in MODES:
        raise InputError('登录启动方式无效；仅支持工作台或已确认公开计划进程。')
    def quoted(value):
        text = str(value)
        if not text or text.startswith('\\\\') or any(ord(c) < 32 or c in '\"%' for c in text):
            raise InputError('启动路径包含不支持的字符；请使用普通本地目录。')
        return '"' + text + '\\' * (len(text) - len(text.rstrip('\\'))) + '"'
    result = quoted(executable) + (' --public-worker' if mode == 'public_worker' else '') + ' --workspace ' + quoted(workspace)
    if len(result.encode('utf-16-le')) // 2 > 260:
        raise InputError('程序和工作区路径合计过长；请先改用较短目录，再登记登录启动。')
    return result


class WindowsRun:
    """Only this application's named HKCU Run value; no machine-wide changes."""
    def read(self, name):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_QUERY_VALUE | winreg.KEY_WOW64_64KEY) as key:
                value, kind = winreg.QueryValueEx(key, name)
                return kind, value
        except FileNotFoundError:
            return None

    def create(self, name, command):
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                               winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
            try:
                winreg.QueryValueEx(key, name)
            except FileNotFoundError:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)
            else:
                raise StartupChanged('登录启动项已被另一操作建立；请刷新后核对。')

    def remove(self, name, command):
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                           winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
            if winreg.QueryValueEx(key, name) != (command, winreg.REG_SZ):
                raise StartupChanged('登录启动项已被修改；为保留该设置，未删除。')
            winreg.DeleteValue(key, name)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate field')
        result[key] = value
    return result


class WindowsStartup:
    def __init__(self, workspace, *, registry=None, executable=None, portable=None, platform=None):
        self.workspace = workspace.root
        self.root = self.workspace / 'windows-startup'
        self.receipt_path = self.root / 'registration-v1.json'
        self.executable = Path(sys.executable if executable is None else executable)
        self.supported = (sys.platform if platform is None else platform) == 'win32' and (
            is_portable() if portable is None else portable)
        self.registry = registry if registry is not None else WindowsRun()
        self.name = 'VibeJobRadar.' + _digest(os.path.normcase(str(self.workspace.resolve())))[:24]

    def _receipt(self):
        if self.root.is_symlink() or self.root.resolve() != self.workspace / 'windows-startup':
            raise InputError('登录启动记录目录不可靠；请保留原设置并检查工作区。')
        path = self.receipt_path
        if path.is_symlink():
            raise InputError('登录启动回执不能使用符号链接。')
        if not path.exists():
            return None
        if not path.is_file() or path.stat().st_size > 8192:
            raise InputError('登录启动回执损坏；请保留原文件并检查工作区。')
        try:
            value = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique)
            if not isinstance(value, dict):raise ValueError('invalid receipt')
            version = value.get('schema_version')
            mode = value.get('mode', 'workbench')
            if (type(version) is not int or version not in (1, 2)
                    or set(value) != (_FIELDS if version == 1 else _FIELDS | {'mode'})
                    or (version == 2 and mode != 'public_worker')
                    or any(type(value[k]) is not str for k in _FIELDS - {'schema_version'})
                    or value['workspace'] != str(self.workspace)
                    or value['value_name'] != self.name
                    or not Path(value['executable']).is_absolute()
                    or Path(value['executable']).name.lower() != 'vibejobradar.exe'
                    or value['command'] != command_line(value['executable'], value['workspace'], mode)):
                raise ValueError('invalid receipt')
            return value
        except (ValueError, UnicodeError, InputError):
            raise InputError('登录启动回执损坏或属于其他工作区；未修改系统设置。') from None

    def _inspect(self):
        base = dict(supported=self.supported, can_enable=False, can_disable=False,
                    registered=False, revision='', workspace=str(self.workspace),
                    executable=str(self.executable), value_name=self.name, mode='workbench')
        if not self.supported:
            return {**base, 'status': 'unsupported', 'message': '登录启动目前仅支持 Windows 便携包；此源码版或系统需手动启动工作台。'}, None, None, None
        receipt = self._receipt()
        actual = self.registry.read(self.name)
        command, path_error = None, ''
        try:
            if (not self.executable.is_absolute() or self.executable.is_symlink()
                    or self.executable.resolve() != self.executable
                    or self.executable.name.lower() != 'vibejobradar.exe'
                    or not self.executable.is_file()):
                raise InputError('当前便携程序路径不可用；请完整解压后重新打开。')
            # Only an owned registration selects the mode used to compare
            # executable locations. An old worker receipt without a Run value
            # never silently chooses worker mode for a new registration.
            owned = receipt is not None and actual == (1, receipt['command'])
            command = command_line(self.executable, self.workspace,
                                   receipt.get('mode', 'workbench') if owned else 'workbench')
        except InputError as exc:
            path_error = str(exc)
        base['revision'] = _digest((receipt, actual, command, str(self.executable)))
        if actual is None:
            state = {**base, 'status': 'disabled' if command else 'unavailable',
                     'can_enable': bool(command), 'message': path_error or '尚未登记；登录 Windows 后不会由本设置打开工作台。'}
        elif receipt is None or actual != (1, receipt['command']):
            state = {**base, 'status': 'conflict', 'message': '同名登录启动项与本机回执不符；未覆盖或删除，请在 Windows 启动应用设置中核对。'}
        else:
            same = command == receipt['command']
            state = {**base, 'status': 'registered' if same else 'moved',
                     'registered': True, 'can_disable': True,
                     'mode': receipt.get('mode', 'workbench'),
                     'message': ('已登记登录启动：' + ('只运行已确认公开计划。' if receipt.get('mode') == 'public_worker' else '打开工作台。')
                                 + 'Windows 可能延迟或禁用启动，请同时核对系统的启动应用设置。'
                                 if same else '已登记的是旧程序位置；请先关闭旧登记，再从当前便携包明确启用。')}
        return state, receipt, actual, command

    def state(self):
        try:
            return self._inspect()[0]
        except (OSError, InputError) as exc:
            return dict(supported=self.supported, can_enable=False, can_disable=False,
                        registered=False, revision='', status='unavailable',
                        workspace=str(self.workspace), executable=str(self.executable), value_name=self.name, mode='workbench',
                        message=str(exc) if isinstance(exc, InputError) else
                        '无法读取登录启动设置；未修改系统配置，请检查工作区或当前用户权限后刷新。')

    def _change(self, data, *, enable):
        allowed = {'revision', 'consent', 'consent_version'} if enable else {'revision'}
        mode_request = enable and type(data) is dict and 'mode' in data
        if mode_request:allowed = allowed | {'mode'}
        if (type(data) is not dict or set(data) != allowed
                or type(data.get('revision')) is not str
                or not re.fullmatch('[a-f0-9]{64}', data['revision'])):
            raise InputError('请刷新登录启动设置后重试；不接受自定义程序或参数。')
        mode = data['mode'] if mode_request else 'workbench'
        if enable and (data['consent'] is not True
                       or data['consent_version'] != (MODE_CONSENT if mode_request else CONSENT)
                       or type(mode) is not str or mode not in MODES):
            raise InputError('请明确同意登录 Windows 后按所选方式运行当前程序和工作区。')
        if not self.supported:
            raise InputError('登录启动目前仅支持 Windows 便携包。')
        # Reads never create metadata; an explicit change owns this directory.
        self._receipt()
        self.root.mkdir(exist_ok=True)
        with writer_lock(self.root):
            state, receipt, actual, command = self._inspect()
            if data['revision'] != state['revision']:
                raise StartupChanged('登录启动设置已变化；请刷新、核对路径后重新选择。')
            if enable:
                if not state['can_enable']:
                    raise InputError(state['message'])
                command = command_line(self.executable, self.workspace, mode)
                receipt = dict(schema_version=1, workspace=str(self.workspace),
                               executable=str(self.executable), command=command, value_name=self.name)
                if mode == 'public_worker':
                    # Preserve the original v1 workbench receipt byte schema.
                    # Older code rejects v2 instead of misreading worker consent.
                    receipt.update(schema_version=2, mode=mode)
                # Journal the exact intended value first: a crash after the OS
                # write remains attributable, and a failed OS write stays off.
                atomic_json(self.receipt_path, receipt)
                try:
                    self.registry.create(self.name, command)
                    if self.registry.read(self.name) != (1, command):
                        raise StartupChanged('登录启动写入后回读不一致；请刷新并核对系统设置。')
                except OSError:
                    raise InputError('未能登记登录启动；请检查当前用户权限后刷新，回执保留供核对。') from None
            else:
                if not state['can_disable']:
                    raise InputError(state['message'])
                try:
                    self.registry.remove(self.name, receipt['command'])
                    if self.registry.read(self.name) is not None:
                        raise StartupChanged('登录启动移除后仍有登记；请刷新并核对系统设置。')
                except OSError:
                    raise InputError('未能移除登录启动；请刷新并核对 Windows 启动应用设置。') from None
                # Retain the harmless old receipt for crash recovery and audit;
                # actual registration, never the receipt, decides current state.
            return self._inspect()[0]

    def enable(self, data):
        return self._change(data, enable=True)

    def disable(self, data):
        return self._change(data, enable=False)
