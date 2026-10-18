"""Native package selection; unknown architectures never receive another package."""
import platform
import sys

POSIX_PLATFORMS = ('linux-x64', 'macos-arm64')


def current_platform():
    machine = platform.machine().lower()
    if sys.platform == 'win32' and machine in {'amd64', 'x86_64'}:
        return 'windows-x64'
    if sys.platform == 'linux' and machine in {'amd64', 'x86_64'}:
        return 'linux-x64'
    if sys.platform == 'darwin' and machine == 'arm64':
        return 'macos-arm64'
    return None


def executable_name(selected):
    return 'VibeJobRadar' if selected.get('platform') in POSIX_PLATFORMS else 'VibeJobRadar.exe'
