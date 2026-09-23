"""Runtime presentation and immutable portable-component boundary."""
import os
import sys

PORTABLE_GUIDANCE = ('此便携包自带 Python、Playwright、配套 Chromium 和 Windows 证书验证组件。'
    '检查浏览器即可使用；组件缺失或需要更新时，先停止任务/计划并关闭工作台，再完整解压已验证的新候选包。'
    '保留原工作区和备份，不覆盖正在运行的文件；也可明确检查并选择本机已安装的 Edge。')


def is_portable():
    return bool(getattr(sys,'frozen',False) and hasattr(sys,'_MEIPASS'))


def description():
    return {'kind':'portable' if is_portable() else 'source',
            'components_mutable':not is_portable(),
            'guidance':PORTABLE_GUIDANCE if is_portable() else ''}


def require_source_install():
    if is_portable():
        from .workspace import InputError
        raise InputError(PORTABLE_GUIDANCE)


def prepare_portable():
    if not is_portable():
        raise RuntimeError('This entry point is reserved for the packaged application.')
    # Matches Playwright's documented bundled-browser layout. An explicitly
    # selected cache remains explicit; no OS or user environment is modified.
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH','0')
