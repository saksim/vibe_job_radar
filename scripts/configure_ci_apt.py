"""Use the hosted runner's existing HTTPS Ubuntu mirrors for CI dependencies.

The runner image puts an HTTP Azure mirror ahead of its two official HTTPS
fallbacks. A slow successful HTTP transfer never triggers that failover. Remove
only that first mirror; keep all source suites, components and signature policy.
"""
from __future__ import annotations

import os
from pathlib import Path
import platform
import subprocess


MIRROR_LIST = Path("/etc/apt/apt-mirrors.txt")
AZURE_MIRROR = "http://azure.archive.ubuntu.com/ubuntu"
HTTPS_MIRRORS = {
    "https://archive.ubuntu.com/ubuntu",
    "https://security.ubuntu.com/ubuntu",
}


def prefer_https_mirrors(contents: str) -> str:
    lines = contents.splitlines(keepends=True)

    def uri(line: str) -> str:
        parts = line.split()
        return parts[0].rstrip("/") if parts and not parts[0].startswith("#") else ""

    if not HTTPS_MIRRORS.issubset({uri(line) for line in lines}):
        raise RuntimeError("Hosted Ubuntu mirror list lacks the official HTTPS fallbacks")
    return "".join(line for line in lines if uri(line) != AZURE_MIRROR)


def main() -> None:
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
        or os.environ.get("RUNNER_OS") != "Linux"
        or platform.freedesktop_os_release().get("ID") != "ubuntu"
    ):
        raise RuntimeError("This configuration applies only to GitHub-hosted Ubuntu CI")
    original = MIRROR_LIST.read_text(encoding="utf-8")
    updated = prefer_https_mirrors(original)
    if updated != original:
        subprocess.run(
            ["sudo", "tee", str(MIRROR_LIST)],
            input=updated,
            text=True,
            check=True,
            stdout=subprocess.DEVNULL,
        )
    print("CI APT mirrors: official Ubuntu HTTPS fallbacks; source/signature settings unchanged")


if __name__ == "__main__":
    main()
