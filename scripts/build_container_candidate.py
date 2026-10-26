"""Build once on the native runner; retain the tested image and small evidence."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import platform
import re
import subprocess
from container_release import (BASE, VERSION, archive_config, digest_file, git_source, load,
    pinned_base, require, save, validate_candidate, validate_loaded_image)
from run_container_smoke import smoke

ROOT = Path(__file__).resolve().parents[1]

_OUTPUT_SCAN_LIMIT = 65536
_ERROR_SIGNALS = {
    "unknown_flag_message": ("unknown flag", "unknown shorthand flag"),
    "daemon_connection_message": ("cannot connect to the docker daemon",),
    "connection_refused_message": ("connection refused",),
    "network_unreachable_message": ("network is unreachable", "no route to host"),
    "dns_lookup_message": ("no such host", "temporary failure in name resolution"),
    "certificate_message": ("x509:", "certificate verify failed"),
    "registry_denial_message": ("unauthorized:", "denied:",),
    "registry_rate_limit_message": ("toomanyrequests",),
    "disk_space_message": ("no space left on device",),
    "platform_manifest_message": ("no matching manifest",),
    "network_timeout_message": ("i/o timeout", "tls handshake timeout"),
}


def _output_summary(value):
    # Keep lengths and a fixed vocabulary, never command output or extracted values.
    if not isinstance(value, (str, bytes)):
        return {"available": False, "length": None, "unit": None,
                "scan_complete": False, "observed_signals": []}
    bounded = value[-_OUTPUT_SCAN_LIMIT:]
    if isinstance(bounded, bytes):
        bounded = bounded.decode("utf-8", errors="replace")
    bounded = bounded.lower()
    return {"available": True, "length": len(value),
            "unit": "bytes" if isinstance(value, bytes) else "characters",
            "scan_complete": len(value) <= _OUTPUT_SCAN_LIMIT,
            "observed_signals": [name for name, phrases in _ERROR_SIGNALS.items()
                                 if any(phrase in bounded for phrase in phrases)]}


def _record_command_failure(out, identity, stage, outcome, exit_code, stdout, stderr):
    try:
        save(out / "failure.json", {**identity, "schema_version": 1,
             "status": "container_candidate_failed", "diagnostic_only": True,
             "root_cause": "unknown", "stage": stage, "outcome": outcome,
             "exit_code": exit_code, "stdout": _output_summary(stdout),
             "stderr": _output_summary(stderr)})
    except Exception:
        # Diagnostics must never replace the original command failure.
        pass


def _candidate_command(out, identity, stage, args, *, cwd=None, timeout=600):
    try:
        result = subprocess.run(args, cwd=cwd, env=None, input=None,
                                capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    except subprocess.TimeoutExpired as error:
        _record_command_failure(out, identity, stage, "timeout", None, error.stdout, error.stderr)
        raise
    except OSError:
        _record_command_failure(out, identity, stage, "spawn_error", None, None, None)
        raise
    if result.returncode != 0:
        _record_command_failure(out, identity, stage, "nonzero_exit", result.returncode,
                                result.stdout, result.stderr)
    require(result.returncode == 0, "Command failed: " + Path(args[0]).name)
    return result.stdout


def build(out, architecture, repository, base):
    require(not out.exists(), "Candidate output exists; preserve the previous attempt")
    require(BASE.fullmatch(base), "An immutable official Python base digest is required")
    require(platform.machine().lower() in {"amd64": {"x86_64", "amd64"}, "arm64": {"aarch64", "arm64"}}[architecture],
            "Candidate must run on its native architecture")
    revision, tree, source_digest = git_source(ROOT)
    declared = os.environ.get("RADAR_SOURCE_SHA", "")
    require(declared == revision, "Checkout differs from the workflow source")
    text = (ROOT / "src/vibe_job_radar/_version.py").read_text(encoding="utf-8")
    matched = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
    require(matched is not None and VERSION.fullmatch(matched.group(1)), "Invalid package version")
    version = matched.group(1)
    run_id, attempt = int(os.environ.get("GITHUB_RUN_ID", "0")), int(os.environ.get("GITHUB_RUN_ATTEMPT", "0"))
    require(run_id > 0 and attempt == 1, "Only an original candidate run is eligible")
    out.mkdir(parents=True)
    tag = "vibe-radar-candidate:" + revision + "-" + architecture
    failure_identity = {"version": version, "source_revision": revision, "source_tree": tree,
        "source_payload_sha256": source_digest, "base_image": base, "architecture": architecture,
        "candidate_run": run_id, "candidate_attempt": attempt}
    _candidate_command(out, failure_identity, "build", ["docker", "build", "--provenance=false", "--platform", "linux/" + architecture,
        "--build-arg", "PYTHON_BASE=" + base, "--build-arg", "RADAR_REVISION=" + revision,
        "--build-arg", "RADAR_VERSION=" + version, "--build-arg", "RADAR_SOURCE=https://github.com/" + repository,
        "--tag", tag, "."], cwd=ROOT, timeout=600)
    info = json.loads(_candidate_command(out, failure_identity, "inspect", ["docker", "image", "inspect", tag]))[0]
    archive = out / "image.tar"
    _candidate_command(out, failure_identity, "save", ["docker", "save", "--output", str(archive), tag], timeout=120)
    config_digest, config = archive_config(archive, tag)
    identity = {"schema_version": 1, "repository": repository, "version": version,
        "source_revision": revision, "source_tree": tree, "source_payload_sha256": source_digest,
        "base_image": base, "architecture": architecture, "image_id": info["Id"],
        "candidate_run": run_id, "candidate_attempt": attempt, "config_digest": config_digest}
    validate_loaded_image(info, identity, config_digest=config_digest)
    proof = smoke(tag, identity, out / "smoke.json")
    manifest = {**identity, "status": "container_candidate_not_release", "file": archive.name, "image_tag": tag,
                "size_bytes": archive.stat().st_size, "sha256": digest_file(archive)}
    validate_candidate(manifest, proof, archive=archive)
    save(out / "manifest.json", manifest)
    print(json.dumps({"success": True, "version": version, "architecture": architecture,
                      "source_revision": revision, "checks": len(proof["checks"])}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolve-base", action="store_true")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--architecture", choices=("amd64", "arm64"))
    parser.add_argument("--repository")
    parser.add_argument("--base")
    args = parser.parse_args()
    if args.resolve_base:
        print(pinned_base())
    else:
        require(all((args.out, args.architecture, args.repository, args.base)), "Missing candidate inputs")
        build(args.out, args.architecture, args.repository, args.base)
