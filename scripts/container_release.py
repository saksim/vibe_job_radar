"""Shared, bounded evidence checks for the server container release chain."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request

SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
VERSION = re.compile(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})")
BASE = re.compile(r"python:3\.12-slim@sha256:[0-9a-f]{64}")
CANDIDATE_WORKFLOW = ".github/workflows/container-image.yml"
PUBLISH_WORKFLOW = ".github/workflows/prepare-container-release.yml"
ARCHES = ("amd64", "arm64")
IDENTITY = ("repository", "version", "source_revision", "source_tree",
            "source_payload_sha256", "base_image", "architecture", "image_id", "config_digest",
            "candidate_run", "candidate_attempt")
CHECKS = (
    "non_root", "read_only_root", "no_new_privileges", "capabilities_dropped",
    "runtime_version", "runtime_architecture", "anonymous_health", "anonymous_private_denied",
    "password_login", "cookie_flags", "authenticated_bootstrap", "server_mode",
    "authored_import", "original_report", "report_download",
    "first_stop_clean", "restart_old_session_denied", "restart_login",
    "persistent_original_report", "persistent_report_download", "logout_revoked",
    "second_stop_clean",
)


def require(value, message):
    if not value:
        raise ValueError(message)


def command(args, *, cwd=None, env=None, input_text=None, timeout=600):
    result = subprocess.run(args, cwd=cwd, env=env, input=input_text,
                            capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    # Do not expose command output: it could contain private HTTP or registry details.
    require(result.returncode == 0, "Command failed: " + Path(args[0]).name)
    return result.stdout


def load(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 8_000_000,
            "Missing or oversized container metadata")
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, value):
    with Path(path).open("x", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")


def digest_file(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return "sha256:" + result.hexdigest()


def image_name(repository):
    require(isinstance(repository, str) and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository),
            "Invalid repository")
    return "ghcr.io/" + repository.lower()


def validate_identity(value):
    require(isinstance(value, dict) and value.get("schema_version") == 1, "Unsupported container evidence")
    image_name(value.get("repository"))
    require(isinstance(value.get("version"), str) and VERSION.fullmatch(value["version"])
            and isinstance(value.get("source_revision"), str) and SHA.fullmatch(value["source_revision"])
            and isinstance(value.get("source_tree"), str) and SHA.fullmatch(value["source_tree"])
            and isinstance(value.get("source_payload_sha256"), str)
            and DIGEST.fullmatch(value["source_payload_sha256"])
            and isinstance(value.get("base_image"), str) and BASE.fullmatch(value["base_image"])
            and value.get("architecture") in ARCHES
            and isinstance(value.get("image_id"), str) and DIGEST.fullmatch(value["image_id"])
            and isinstance(value.get("config_digest"), str) and DIGEST.fullmatch(value["config_digest"])
            and type(value.get("candidate_run")) is int and value["candidate_run"] > 0
            and value.get("candidate_attempt") == 1, "Invalid container source identity")


def validate_candidate(manifest, proof, *, archive=None):
    validate_identity(manifest)
    require(manifest.get("status") == "container_candidate_not_release"
            and manifest.get("file") == "image.tar"
            and manifest.get("image_tag") == "vibe-radar-candidate:" + manifest["source_revision"] + "-" + manifest["architecture"]
            and type(manifest.get("size_bytes")) is int and 0 < manifest["size_bytes"] <= 750_000_000
            and isinstance(manifest.get("sha256"), str) and DIGEST.fullmatch(manifest["sha256"]),
            "Invalid image archive metadata")
    require(isinstance(proof, dict) and proof.get("schema_version") == 1
            and all(proof.get(key) == manifest[key] for key in IDENTITY)
            and proof.get("success") is True and proof.get("external_collection") is False,
            "Container smoke/source mismatch")
    rows = proof.get("checks")
    require(isinstance(rows, list) and len(rows) == len(CHECKS)
            and {row.get("name") for row in rows if isinstance(row, dict)} == set(CHECKS)
            and all(row.get("passed") is True for row in rows), "Incomplete container smoke")
    require(isinstance(proof.get("report_sha256"), str) and DIGEST.fullmatch(proof["report_sha256"])
            and isinstance(proof.get("report_id"), str) and proof["report_id"], "Missing persisted report proof")
    if archive is not None:
        path = Path(archive)
        require(path.is_file() and not path.is_symlink() and path.name == "image.tar"
                and path.stat().st_size == manifest["size_bytes"]
                and digest_file(path) == manifest["sha256"], "Candidate archive changed")
        config_digest, config = archive_config(path, manifest["image_tag"])
        require(config_digest == manifest["config_digest"], "Saved image config digest mismatch")
        validate_config(config, manifest)
    return manifest


def validate_pair(candidates):
    require(set(candidates) == set(ARCHES), "Both native architectures are required")
    left = candidates["amd64"]
    for arch, row in candidates.items():
        validate_candidate(row["manifest"], row["proof"])
        require(row["manifest"]["architecture"] == arch, "Architecture artifact mismatch")
        for key in IDENTITY:
            if key not in {"architecture", "image_id", "config_digest"}:
                require(row["manifest"][key] == left["manifest"][key], "Mixed container inputs: " + key)
    return left["manifest"]


def archive_config(archive, tag):
    """Read actual Docker-save config bytes; engine inspect.Id may be an OCI index."""
    with tarfile.open(archive, "r:") as content:
        members = content.getmembers()
        require(len(members) <= 5000, "Too many image archive members")
        def read(name):
            path = PurePosixPath(name)
            require(not path.is_absolute() and ".." not in path.parts and "\\\\" not in name,
                    "Unsafe image metadata path")
            matches = [row for row in members if row.name == name]
            require(len(matches) == 1 and matches[0].isfile() and 0 < matches[0].size <= 8_000_000,
                    "Missing, duplicate or oversized image metadata")
            return content.extractfile(matches[0]).read()
        entries = json.loads(read("manifest.json"))
        require(isinstance(entries, list) and len(entries) == 1
                and isinstance(entries[0], dict) and entries[0].get("RepoTags") == [tag]
                and isinstance(entries[0].get("Config"), str),
                "Save archive must contain exactly the selected image")
        raw = read(entries[0]["Config"])
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        config = json.loads(raw)
        require(isinstance(config, dict), "Invalid saved image config")
        return digest, config


def validate_config(config, manifest):
    require(config.get("os") == "linux" and config.get("architecture") == manifest["architecture"],
            "Saved config platform mismatch")
    settings = config.get("config", {})
    labels = settings.get("Labels") or {}
    require(labels.get("org.opencontainers.image.source") == "https://github.com/" + manifest["repository"]
            and labels.get("org.opencontainers.image.revision") == manifest["source_revision"]
            and labels.get("org.opencontainers.image.version") == manifest["version"]
            and settings.get("User") == "10001:10001", "Saved config source labels or user mismatch")


def validate_loaded_image(info, manifest, *, config_digest=None, reference=None):
    # Classic engines expose config digest as Id; containerd engines can expose a
    # manifest/index descriptor instead. Byte identity comes from the checked save
    # archive, or from the pulled immutable registry reference and child manifest.
    actual_config = config_digest if config_digest is not None else info.get("Id")
    require(actual_config == manifest["config_digest"]
            and isinstance(info.get("Id"), str) and DIGEST.fullmatch(info["Id"])
            and info.get("Os") == "linux" and info.get("Architecture") == manifest["architecture"],
            "Loaded image identity mismatch")
    descriptor = info.get("Descriptor") or {}
    require(info["Id"] == actual_config or (
        descriptor.get("digest") == info["Id"] and descriptor.get("mediaType") in {
            "application/vnd.oci.image.index.v1+json", "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.docker.distribution.manifest.v2+json"}),
        "Unknown engine image identity")
    if reference is not None:
        require(reference in info.get("RepoDigests", []), "Pulled image is not bound to requested digest")
    validate_config({"os": info["Os"], "architecture": info["Architecture"],
                     "config": info.get("Config", {})}, manifest)


def validate_index(index, children):
    require(isinstance(index, dict) and index.get("schemaVersion") == 2, "Invalid registry index")
    rows = index.get("manifests")
    require(isinstance(rows, list) and len(rows) == 2, "Container index must contain two images")
    found = {}
    for row in rows:
        platform = row.get("platform", {})
        arch = platform.get("architecture")
        require(platform.get("os") == "linux" and arch in ARCHES and arch not in found
                and isinstance(row.get("digest"), str) and DIGEST.fullmatch(row["digest"]),
                "Unexpected or duplicate container platform")
        found[arch] = row["digest"]
    require(found == children, "Registry index differs from tested images")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Registry:
    """GHCR manifest reads only; credentials never follow registry redirects."""
    def __init__(self, repository, *, anonymous=False):
        image_name(repository)
        self.repository = repository.lower()
        self.anonymous = anonymous
        self._token = None
        self._expires_at = 0.0
        self.opener = urllib.request.build_opener(NoRedirect())

    def _request(self, url, headers):
        with self.opener.open(urllib.request.Request(url, headers=headers), timeout=30) as response:
            data = response.read(8_000_001)
            require(len(data) <= 8_000_000, "Registry metadata too large")
            return data, response.headers

    def token(self):
        if self._token is None or time.monotonic() >= self._expires_at:
            headers = {}
            if not self.anonymous:
                actor, secret = os.environ.get("GITHUB_ACTOR", ""), os.environ.get("GH_TOKEN", "")
                require(actor and secret, "Missing scoped workflow credentials")
                headers["Authorization"] = "Basic " + base64.b64encode(
                    (actor + ":" + secret).encode()).decode()
            query = urllib.parse.urlencode({"service": "ghcr.io",
                    "scope": "repository:" + self.repository + ":pull"})
            raw, _ = self._request("https://ghcr.io/token?" + query, headers)
            body = json.loads(raw)
            self._token = body.get("token") or body.get("access_token")
            require(isinstance(self._token, str) and self._token, "Missing registry read token")
            lifetime = body.get("expires_in", 60)
            require(type(lifetime) is int and lifetime > 0, "Invalid registry token lifetime")
            self._expires_at = time.monotonic() + max(1, min(lifetime, 300) - 30)
        return self._token

    def manifest(self, reference, *, missing_ok=False):
        require(isinstance(reference, str) and re.fullmatch(r"[A-Za-z0-9_.:-]+", reference),
                "Invalid registry reference")
        try:
            raw, headers = self._request(
                "https://ghcr.io/v2/" + self.repository + "/manifests/" + reference,
                {"Authorization": "Bearer " + self.token(),
                 "Accept": ", ".join((
                     "application/vnd.oci.image.index.v1+json",
                     "application/vnd.docker.distribution.manifest.list.v2+json",
                     "application/vnd.oci.image.manifest.v1+json",
                     "application/vnd.docker.distribution.manifest.v2+json"))})
        except urllib.error.HTTPError as error:
            if missing_ok and error.code == 404:
                return None
            raise ValueError("Registry manifest unavailable (HTTP " + str(error.code) + ")") from None
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        require(headers.get("Docker-Content-Digest") == digest
                and (not reference.startswith("sha256:") or reference == digest),
                "Registry manifest digest mismatch")
        return {"digest": digest, "manifest": json.loads(raw)}


def git_source(root):
    revision = command(["git", "rev-parse", "HEAD"], cwd=root).strip()
    tree = command(["git", "rev-parse", "HEAD^{tree}"], cwd=root).strip()
    require(SHA.fullmatch(revision) and SHA.fullmatch(tree), "Invalid checkout identity")
    paths = ["Dockerfile", ".dockerignore", "pyproject.toml", "README.md", "LICENSE", "src"]
    command(["git", "diff", "--exit-code", "HEAD", "--", *paths], cwd=root)
    names = command(["git", "ls-files", "-z", "--", *paths], cwd=root).split("\0")
    digest = hashlib.sha256()
    for name in sorted(filter(None, names)):
        path = root / name
        require(path.is_file() and not path.is_symlink(), "Invalid container build input")
        digest.update(name.encode() + b"\0" + bytes.fromhex(digest_file(path)[7:]))
    return revision, tree, "sha256:" + digest.hexdigest()


def pinned_base():
    output = command(["docker", "buildx", "imagetools", "inspect", "python:3.12-slim"], timeout=120)
    matches = re.findall(r"(?m)^Digest:\s+(sha256:[0-9a-f]{64})\s*$", output)
    require(len(matches) == 1, "Cannot bind the official Python base index")
    return "python:3.12-slim@" + matches[0]
