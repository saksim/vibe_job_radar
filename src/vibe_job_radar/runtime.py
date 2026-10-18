"""Runtime presentation and immutable portable-component boundary."""
import os
from pathlib import Path
import sys
import subprocess
import webbrowser

PORTABLE_GUIDANCE = ('此便携包自带 Python、Playwright、配套 Chromium 和 Windows 证书验证组件。'
    '请完整解压到较短目录，再检查浏览器；组件缺失或需要更新时，先停止任务/计划并关闭工作台，再完整解压已验证的新候选包。'
    '保留原工作区和备份，不覆盖正在运行的文件；也可明确检查并选择本机已安装的 Edge 或 Chrome。')


POSIX_GUIDANCE = ('此程序包自带 Python、Playwright、Chromium、PAC 和 NTLMv2 组件。'
    '请完整解压并保留目录、符号链接和可执行权限；Linux 仍需要兼容的浏览器系统库。'
    '组件更新时先停止任务并关闭程序，备份工作区，把已核验的新包解压到另一个目录。'
    '可在“版本与升级”中按平台核验下载，明确选择备份和自动重启；也可手动准备完整包。'
    '系统密钥库可保存已有代理和团队凭据；Windows DPAPI 和 Windows 登录启动不适用于此包。')

def portable_guidance():
    return PORTABLE_GUIDANCE if sys.platform=='win32' else POSIX_GUIDANCE

def open_workbench_browser(url):
    if not is_portable() or sys.platform=='win32':
        return webbrowser.open(url)
    command={'linux':'/usr/bin/xdg-open','darwin':'/usr/bin/open'}.get(sys.platform)
    if not command or not Path(command).is_file():
        return False
    environment=dict(os.environ)
    if sys.platform=='linux':
        # PyInstaller prepends its private libraries; system desktop tools use
        # their own ABI. Restore only the documented inherited library path.
        original=environment.pop('LD_LIBRARY_PATH_ORIG',None)
        if original is None:environment.pop('LD_LIBRARY_PATH',None)
        else:environment['LD_LIBRARY_PATH']=original
    try:
        process=subprocess.Popen([command,url],env=environment,stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        return process.poll() in (None,0)
    except OSError:
        return False


def is_portable():
    return bool(getattr(sys,'frozen',False) and hasattr(sys,'_MEIPASS'))


def description():
    return {'kind':'portable' if is_portable() else 'source',
            'components_mutable':not is_portable(),
            'guidance':portable_guidance() if is_portable() else ''}


def require_source_install():
    if is_portable():
        from .workspace import InputError
        raise InputError(portable_guidance())


def prepare_portable():
    if not is_portable():
        raise RuntimeError('This entry point is reserved for the packaged application.')
    # Keep Chromium out of the deeply nested Python package layout so ordinary
    # Windows ZIP tools can extract it without enabling system long paths.
    # Bind to the executable, never the launching cwd. Explicit caches remain
    # explicit; this changes only this application process's environment.
    bundled=str(Path(sys.executable).resolve().parent/'browsers')
    if 'PLAYWRIGHT_BROWSERS_PATH' not in os.environ:
        os.environ['PLAYWRIGHT_BROWSERS_PATH']=bundled
        os.environ['VIBE_RADAR_BUNDLED_BROWSER_PATH']=bundled
