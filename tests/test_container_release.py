"""Container publication must preserve tested bytes, source and visibility gates."""
import copy
import hashlib
import json
import io
import tarfile
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import container_release as proof
import prepare_container_release as release


def candidate(arch):
    manifest = {"schema_version": 1, "repository": "saksim/vibe_job_radar", "version": "0.18.0",
        "source_revision": "a" * 40, "source_tree": "b" * 40,
        "source_payload_sha256": "sha256:" + "c" * 64,
        "base_image": "python:3.12-slim@sha256:" + "d" * 64,
        "architecture": arch, "image_id": "sha256:" + ("e" if arch == "amd64" else "f") * 64,
                "config_digest": "sha256:" + ("e" if arch == "amd64" else "f") * 64,
        "image_tag": "vibe-radar-candidate:" + "a" * 40 + "-" + arch,
        "candidate_run": 123, "candidate_attempt": 1,
        "status": "container_candidate_not_release", "file": "image.tar",
        "size_bytes": 5, "sha256": "sha256:" + hashlib.sha256(b"image").hexdigest()}
    smoke = {"schema_version": 1, **{key: manifest[key] for key in proof.IDENTITY},
        "success": True, "external_collection": False,
        "checks": [{"name": name, "passed": True} for name in proof.CHECKS],
        "report_id": "authored-report", "report_sha256": "sha256:" + "1" * 64}
    return {"manifest": manifest, "proof": smoke}


def image_info(manifest):
    return {"Id": manifest["image_id"], "Os": "linux", "Architecture": manifest["architecture"],
            "RepoDigests": ["ghcr.io/saksim/vibe_job_radar@sha256:" + "9" * 64],
            "Config": {"User": "10001:10001", "Labels": {
                "org.opencontainers.image.source": "https://github.com/" + manifest["repository"],
                "org.opencontainers.image.revision": manifest["source_revision"],
                "org.opencontainers.image.version": manifest["version"]}}}


class CandidateEvidenceTests(unittest.TestCase):
    def test_native_pair_requires_same_source_base_and_original_attempt(self):
        rows = {arch: candidate(arch) for arch in proof.ARCHES}
        proof.validate_pair(rows)
        for key, value in (("source_revision", "1" * 40), ("source_tree", "2" * 40),
                           ("source_payload_sha256", "sha256:" + "3" * 64),
                           ("base_image", "python:3.12-slim@sha256:" + "4" * 64),
                           ("candidate_run", 456), ("candidate_attempt", 2)):
            changed = copy.deepcopy(rows)
            changed["arm64"]["manifest"][key] = changed["arm64"]["proof"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                proof.validate_pair(changed)

    def test_missing_failed_or_duplicate_checks_and_live_collection_are_rejected(self):
        row = candidate("amd64")
        for change in ("missing", "failed", "duplicate", "live", "mismatched-image"):
            broken = copy.deepcopy(row["proof"])
            if change == "missing":
                broken["checks"].pop()
            elif change == "failed":
                broken["checks"][0]["passed"] = False
            elif change == "duplicate":
                broken["checks"][-1] = broken["checks"][0]
            elif change == "live":
                broken["external_collection"] = True
            else:
                broken["image_id"] = "sha256:" + "0" * 64
            with self.subTest(change=change), self.assertRaises(ValueError):
                proof.validate_candidate(row["manifest"], broken)

    def test_changed_archive_bytes_are_not_publishable(self):
        row = candidate("amd64")
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "image.tar"
            config = {"os": "linux", "architecture": "amd64", "config": image_info(row["manifest"])["Config"]}
            raw = json.dumps(config).encode()
            row["manifest"]["config_digest"] = row["proof"]["config_digest"] = "sha256:" + hashlib.sha256(raw).hexdigest()
            with tarfile.open(archive, "w") as content:
                for name, value in (("manifest.json", json.dumps([{"Config": "config.json",
                    "RepoTags": [row["manifest"]["image_tag"]]}]).encode()), ("config.json", raw)):
                    entry = tarfile.TarInfo(name); entry.size = len(value)
                    content.addfile(entry, io.BytesIO(value))
            row["manifest"]["size_bytes"] = archive.stat().st_size
            row["manifest"]["sha256"] = proof.digest_file(archive)
            proof.validate_candidate(row["manifest"], row["proof"], archive=archive)
            archive.write_bytes(b"other")
            with self.assertRaisesRegex(ValueError, "archive changed"):
                proof.validate_candidate(row["manifest"], row["proof"], archive=archive)

    def test_loaded_image_requires_native_platform_revision_and_nonroot_user(self):
        manifest = candidate("arm64")["manifest"]
        original = image_info(manifest)
        proof.validate_loaded_image(original, manifest)
        for change in ("config", "platform", "revision", "root"):
            info = copy.deepcopy(original)
            if change == "config":
                info["Id"] = "sha256:" + "0" * 64
            elif change == "platform":
                info["Architecture"] = "amd64"
            elif change == "revision":
                info["Config"]["Labels"]["org.opencontainers.image.revision"] = "9" * 40
            else:
                info["Config"]["User"] = "0:0"
            with self.subTest(change=change), self.assertRaises(ValueError):
                proof.validate_loaded_image(info, manifest)

    def test_containerd_descriptor_is_distinct_from_saved_config_identity(self):
        manifest = candidate("amd64")["manifest"]
        info = image_info(manifest)
        info["Id"] = "sha256:" + "8" * 64
        info["Descriptor"] = {"digest": info["Id"], "mediaType": "application/vnd.oci.image.index.v1+json"}
        with self.assertRaises(ValueError):
            proof.validate_loaded_image(info, manifest)
        proof.validate_loaded_image(info, manifest, config_digest=manifest["config_digest"],
                                    reference=info["RepoDigests"][0])
        with self.assertRaises(ValueError):
            proof.validate_loaded_image(info, manifest, config_digest=manifest["config_digest"],
                                        reference="ghcr.io/saksim/vibe_job_radar@sha256:" + "0" * 64)

    def test_index_cannot_omit_swap_or_duplicate_architecture(self):
        children = {"amd64": "sha256:" + "1" * 64, "arm64": "sha256:" + "2" * 64}
        index = {"schemaVersion": 2, "manifests": [
            {"digest": digest, "platform": {"os": "linux", "architecture": arch}}
            for arch, digest in children.items()]}
        proof.validate_index(index, children)
        for change in ("missing", "wrong-digest", "duplicate", "wrong-os"):
            value = copy.deepcopy(index)
            if change == "missing":
                value["manifests"].pop()
            elif change == "wrong-digest":
                value["manifests"][0]["digest"] = children["arm64"]
            elif change == "duplicate":
                value["manifests"][1] = value["manifests"][0]
            else:
                value["manifests"][0]["platform"]["os"] = "windows"
            with self.subTest(change=change), self.assertRaises(ValueError):
                proof.validate_index(value, children)

    def test_existing_version_refuses_overwrite_before_publication(self):
        with patch.object(proof.Registry, "manifest", return_value={"digest": "sha256:" + "a" * 64}):
            with self.assertRaisesRegex(ValueError, "already exists"):
                release.require_unused(proof.Registry("saksim/vibe_job_radar"), ("0.18.0",))

    def test_anonymous_registry_never_uses_workflow_credentials(self):
        observed = []
        def read(url, headers):
            observed.append(headers)
            return b'{"token":"anonymous-test-token"}', {}
        with patch.dict(os.environ, {"GITHUB_ACTOR": "owner", "GH_TOKEN": "private-test-token"}):
            registry = proof.Registry("saksim/vibe_job_radar", anonymous=True)
            with patch.object(registry, "_request", side_effect=read):
                self.assertEqual(registry.token(), "anonymous-test-token")
        self.assertEqual(observed, [{}])

    def test_registry_token_is_renewed_before_long_pushes_can_expire_it(self):
        registry = proof.Registry("saksim/vibe_job_radar", anonymous=True)
        with patch.object(registry, "_request", side_effect=[
            (b'{"token":"first","expires_in":60}', {}),
            (b'{"token":"second","expires_in":60}', {})]) as read, patch.object(
                proof.time, "monotonic", side_effect=[100.0, 131.0, 132.0]):
            self.assertEqual(registry.token(), "first")
            self.assertEqual(registry.token(), "second")
            self.assertEqual(read.call_count, 2)

    def test_registry_digest_uses_actual_response_bytes(self):
        body = b'{"schemaVersion":2}'
        registry = proof.Registry("saksim/vibe_job_radar", anonymous=True)
        with patch.object(registry, "token", return_value="test"), patch.object(
                registry, "_request", return_value=(body, {"Docker-Content-Digest": "sha256:" + "0" * 64})):
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                registry.manifest("0.18.0")

    def test_annotated_tag_resolves_to_actual_commit(self):
        sha = "a" * 40
        with patch.object(release, "api", side_effect=[
            {"object": {"type": "tag", "sha": "b" * 40}},
            {"object": {"type": "commit", "sha": sha}}]):
            self.assertEqual(release.resolved_tag("saksim/vibe_job_radar", "v0.18.0"), sha)

    def test_formal_release_with_wrong_tag_commit_is_rejected(self):
        sha = "a" * 40
        def api(repository, path):
            if path == "git/ref/heads/main":
                return {"object": {"sha": sha}}
            return {"tag_name": path.rsplit("/", 1)[-1], "draft": False,
                    "prerelease": False, "target_commitish": sha, "id": 1, "html_url": "https://example.invalid"}
        with patch.object(release, "api", side_effect=api), patch.object(
                release, "resolved_tag", return_value="b" * 40):
            with self.assertRaisesRegex(ValueError, "matching formal"):
                release.released_source("saksim/vibe_job_radar", sha, "0.18.0")


class PublicPullGates(unittest.TestCase):
    def fixture(self):
        candidates = {arch: candidate(arch) for arch in proof.ARCHES}
        identity = candidates["amd64"]["manifest"]
        digest = "sha256:" + "9" * 64
        return {"schema_version": 1, "status": "published_pending_public_verification",
                "repository": identity["repository"], "image": proof.image_name(identity["repository"]),
                "version": identity["version"], "source_revision": identity["source_revision"],
                "source_tree": identity["source_tree"], "source_payload_sha256": identity["source_payload_sha256"],
                "base_image": identity["base_image"], "candidate_run": 123, "candidate_attempt": 1,
                "publication_run": 456, "publication_attempt": 1, "candidates": candidates,
                "rebuilt_for_release": False, "latest_updated": False, "ci": {"existing": True},
                "digest": digest, "reference": proof.image_name(identity["repository"]) + "@" + digest,
                "platform_digests": {"amd64": "sha256:" + "1" * 64, "arm64": "sha256:" + "2" * 64},
                "version_tag": identity["version"], "source_tag": "sha-" + identity["source_revision"]}

    def platform_proofs(self, publication):
        return {arch: {**copy.deepcopy(publication), "status": "public_pull_verified",
                       "anonymous_pull_verified": True, "anonymous_runtime_architecture": arch,
                       "anonymous_smoke": copy.deepcopy(publication["candidates"][arch]["proof"]),
                       "verification_run": 789, "verification_attempt": 1, "latest_updated": False}
                for arch in proof.ARCHES}

    def test_each_native_public_job_uses_empty_auth_and_cannot_write_tags(self):
        publication = self.fixture()
        for arch, machine in (("amd64", "x86_64"), ("arm64", "aarch64")):
            events = []
            def run(args, **kwargs):
                if args[1] == "pull":
                    self.assertEqual(list(Path(kwargs["env"]["DOCKER_CONFIG"]).iterdir()), [])
                    self.assertEqual(args[3], "linux/" + arch)
                    events.append("anonymous-pull")
                if args[1:3] == ["image", "inspect"]:
                    return json.dumps([image_info(publication["candidates"][arch]["manifest"])])
                return ""
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as temp, \
                 patch.dict(os.environ, {"GITHUB_RUN_ID": "789", "GITHUB_RUN_ATTEMPT": "1"}), \
                 patch("platform.machine", return_value=machine), \
                 patch.object(release, "read_publication", return_value=(publication, {"id": 456})), \
                 patch.object(release, "public_registry"), \
                 patch.object(release, "command", side_effect=run), \
                 patch.object(release, "smoke", return_value=publication["candidates"][arch]["proof"]), \
                 patch.object(release, "docker_login") as login, \
                 patch.object(release, "create_index") as latest:
                result = release.verify_public(publication["repository"], 456, Path(temp) / "out", arch)
                self.assertTrue(result["anonymous_pull_verified"])
                self.assertFalse(result["latest_updated"])
                self.assertEqual(result["anonymous_runtime_architecture"], arch)
                self.assertEqual(events, ["anonymous-pull"])
                login.assert_not_called()
                latest.assert_not_called()

    def test_failed_public_smoke_never_authenticates_or_moves_latest(self):
        publication = self.fixture()
        with tempfile.TemporaryDirectory() as temp, \
             patch.dict(os.environ, {"GITHUB_RUN_ID": "789", "GITHUB_RUN_ATTEMPT": "1"}), \
             patch("platform.machine", return_value="x86_64"), \
             patch.object(release, "read_publication", return_value=(publication, {"id": 456})), \
             patch.object(release, "public_registry"), \
             patch.object(release, "command", side_effect=["", json.dumps([
                 image_info(publication["candidates"]["amd64"]["manifest"])])]), \
             patch.object(release, "smoke", side_effect=ValueError("actual smoke failed")), \
             patch.object(release, "docker_login") as login, \
             patch.object(release, "create_index") as latest:
            with self.assertRaisesRegex(ValueError, "actual smoke failed"):
                release.verify_public(publication["repository"], 456, Path(temp) / "out", "amd64")
            login.assert_not_called()
            latest.assert_not_called()

    def test_missing_or_bad_arm64_proof_stops_before_credentials(self):
        publication = self.fixture()
        for change in ("missing", "failed", "wrong-run", "wrong-image"):
            proofs = self.platform_proofs(publication)
            if change == "missing":
                del proofs["arm64"]
            elif change == "failed":
                proofs["arm64"]["anonymous_smoke"]["checks"][0]["passed"] = False
            elif change == "wrong-run":
                proofs["arm64"]["verification_run"] = 1234
            else:
                proofs["arm64"]["digest"] = "sha256:" + "0" * 64
            with self.subTest(change=change), patch.object(release, "docker_login") as login, \
                 patch.object(release, "create_index") as latest, self.assertRaises(ValueError):
                release.update_verified_latest(publication, proofs, 789)
            login.assert_not_called()
            latest.assert_not_called()

    def test_latest_moves_only_after_both_actual_native_proofs(self):
        publication = self.fixture()
        proofs = self.platform_proofs(publication)
        events = []
        with patch.object(release, "released_source", side_effect=lambda *a: events.append("formal-tags")), \
             patch.object(release, "public_registry") as registry, \
             patch.object(release, "docker_login", side_effect=lambda *a: events.append("authenticate")), \
             patch.object(release, "create_index", side_effect=lambda *a: events.append("latest")):
            registry.return_value.manifest.return_value = {"digest": publication["digest"]}
            release.update_verified_latest(publication, proofs, 789)
        self.assertEqual(events, ["formal-tags", "authenticate", "latest"])

    def test_finalizer_binds_both_completed_native_producers_to_current_run(self):
        publication = self.fixture()
        proofs = self.platform_proofs(publication)
        selected = {"id": 789, "path": ".github/workflows/verify-container-public.yml",
                    "event": "workflow_dispatch", "head_branch": "main", "run_attempt": 1,
                    "head_sha": publication["source_revision"],
                    "head_repository": {"full_name": publication["repository"]}, "status": "in_progress"}
        jobs = {"total_count": 2, "jobs": [
            {"id": n, "name": "anonymous-pull-" + arch, "status": "completed", "conclusion": "success",
             "run_id": 789, "head_sha": publication["source_revision"],
             "started_at": "2026-10-24T01:00:00Z", "completed_at": "2026-10-24T01:01:00Z"}
            for n, arch in enumerate(proof.ARCHES, 1)]}
        listing = {"total_count": 2, "artifacts": [
            {"id": n, "name": "container-public-" + arch, "size_in_bytes": 1000, "expired": False,
             "digest": "sha256:" + str(n) * 64, "created_at": "2026-10-24T01:00:30Z",
             "workflow_run": {"id": 789, "head_sha": publication["source_revision"]}}
            for n, arch in enumerate(proof.ARCHES, 1)]}
        for bad_run in (False, True):
            actual_jobs = copy.deepcopy(jobs)
            if bad_run:
                actual_jobs["jobs"][1]["run_id"] = 788
            def api(repository, path):
                return actual_jobs if "/jobs?" in path else listing if "/artifacts?" in path else selected
            def load(path):
                arch = path.parent.name.split("-")[-1]
                return proofs[arch] if path.name == "public-verification.json" else proofs[arch]["anonymous_smoke"]
            with self.subTest(bad_run=bad_run), tempfile.TemporaryDirectory() as temp, \
                 patch.dict(os.environ, {"GITHUB_RUN_ID": "789", "GITHUB_RUN_ATTEMPT": "1"}), \
                 patch.object(release, "read_publication", return_value=(publication, {"id": 456})), \
                 patch.object(release, "api", side_effect=api), \
                 patch.object(release, "download_artifact"), \
                 patch.object(release, "load", side_effect=load), \
                 patch.object(release, "public_registry"), \
                 patch.object(release, "update_verified_latest") as update:
                if bad_run:
                    with self.assertRaisesRegex(ValueError, "original anonymous jobs"):
                        release.finalize_public(publication["repository"], 456, 789, Path(temp) / "out", update_latest=True)
                    update.assert_not_called()
                else:
                    result = release.finalize_public(publication["repository"], 456, 789, Path(temp) / "out", update_latest=True)
                    self.assertEqual(result["anonymous_runtime_architectures"], ["amd64", "arm64"])
                    self.assertEqual(set(result["public_artifacts"]), set(proof.ARCHES))
                    update.assert_called_once()


if __name__ == "__main__":
    unittest.main()
