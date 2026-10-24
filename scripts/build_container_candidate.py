"""Build once on the native runner; retain the tested image and small evidence."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import platform
import re
from container_release import (BASE, VERSION, archive_config, command, digest_file, git_source, load,
    pinned_base, require, save, validate_candidate, validate_loaded_image)
from run_container_smoke import smoke

ROOT = Path(__file__).resolve().parents[1]


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
    command(["docker", "build", "--provenance=false", "--platform", "linux/" + architecture,
        "--build-arg", "PYTHON_BASE=" + base, "--build-arg", "RADAR_REVISION=" + revision,
        "--build-arg", "RADAR_VERSION=" + version, "--build-arg", "RADAR_SOURCE=https://github.com/" + repository,
        "--tag", tag, "."], cwd=ROOT, timeout=600)
    info = json.loads(command(["docker", "image", "inspect", tag]))[0]
    archive = out / "image.tar"
    command(["docker", "save", "--output", str(archive), tag], timeout=120)
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
