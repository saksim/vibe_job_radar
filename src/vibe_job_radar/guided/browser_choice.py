"""Local, explicit browser choice and a minimal historical check (never a session).

No browser launch, package import, installation, network or environment mutation.
A previous green check is history only: every new process starts unverified.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

from ..collection import writer_lock
from ..utils import atomic_json, parse_time
from ..workspace import InputError

CHOICES = {'bundled': 'Playwright 配套 Chromium', 'msedge': '本机 Microsoft Edge（独立会话）',
           'chrome': '本机 Google Chrome（独立会话）'}


def validate_choice(value):
    if not isinstance(value, str) or value not in CHOICES:
        raise InputError('只接受配套 Chromium、本机 Edge 或 Chrome；不接受路径、命令或其他通道。')
    return value


def interpreter_id():
    # Compare environments without persisting usernames or private paths.
    return hashlib.sha256(os.path.normcase(os.path.abspath(sys.executable)).encode('utf-8')).hexdigest()


def _validate_probe(probe):
    if probe is None:
        return None
    keys = {'checked_at', 'channel', 'code', 'ready', 'playwright_version', 'interpreter_id', 'exit_hex'}
    if not isinstance(probe, dict) or set(probe) not in (keys, keys | {'network_backend'}):
        raise ValueError('invalid historical probe')
    if probe.get('network_backend', 'bridge') not in {'bridge', 'native'}:
        raise ValueError('invalid historical backend')
    validate_choice(probe['channel'])
    if type(probe['ready']) is not bool:
        raise ValueError('invalid historical readiness')
    for key in ('checked_at', 'code', 'playwright_version', 'interpreter_id', 'exit_hex'):
        if not isinstance(probe[key], str) or len(probe[key]) > 100:
            raise ValueError('invalid historical field')
    parse_time(probe['checked_at'])
    if not re.fullmatch(r'[a-z_]{1,80}', probe['code']):
        raise ValueError('invalid historical code')
    if not re.fullmatch(r'[0-9a-f]{64}', probe['interpreter_id']):
        raise ValueError('invalid interpreter fingerprint')
    if probe['exit_hex'] and not re.fullmatch(r'0x[0-9A-F]{8}', probe['exit_hex']):
        raise ValueError('invalid exit code')
    if not re.fullmatch(r'[A-Za-z0-9.+!-]{0,100}', probe['playwright_version']):
        raise ValueError('invalid SDK version')
    return dict(probe)


class BrowserChoice:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / 'browser-choice.json'

    def read(self):
        try:
            if self.path.is_symlink():
                raise ValueError('symbolic link')
            if not self.path.exists():
                return {'schema_version': 1, 'selected': 'bundled', 'last_check': None}
            if self.path.stat().st_size > 8192:
                raise ValueError('oversized')
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if (not isinstance(data, dict) or set(data) != {'schema_version', 'selected', 'last_check'}
                    or type(data['schema_version']) is not int or data['schema_version'] != 1):
                raise ValueError('schema')
            validate_choice(data['selected'])
            _validate_probe(data['last_check'])
            return data
        except (OSError, ValueError, TypeError) as exc:
            raise InputError('本机浏览器选择记录无法读取；未自动换用其他浏览器。请保留文件并检查权限或备份。') from exc

    def record(self, report, channel, *, select=False):
        """Only an explicit successful check can change the saved choice."""
        validate_choice(channel)
        if select and report.get('ready') is not True:
            raise InputError('所选浏览器尚未通过启动检查，不保存为采集浏览器。')
        probe = _validate_probe({
            'checked_at': report['checked_at'], 'channel': channel,
            'network_backend': report.get('network_backend', 'bridge'),
            'code': report['code'], 'ready': report['ready'],
            'playwright_version': report.get('playwright_version') or '',
            'interpreter_id': interpreter_id(), 'exit_hex': report.get('process_exit_hex', ''),
        })
        with writer_lock(self.root):
            data = self.read()
            data['last_check'] = probe
            if select:
                data['selected'] = channel
            atomic_json(self.path, data)
        return data

    @staticmethod
    def historical_view(data, version):
        last = data['last_check']
        if last is None:
            return None
        return {**last, 'matches_environment': last['interpreter_id'] == interpreter_id()
                and last['playwright_version'] == (version or ''),
                'historical_only': True}
