"""Publish tested native images, then separately verify public pull before Latest."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import tempfile
import urllib.error
import urllib.request

from container_release import (ARCHES, BASE, CANDIDATE_WORKFLOW, PUBLISH_WORKFLOW,
    Registry, NoRedirect, command, image_name, load, require, save,
    validate_candidate, validate_index, validate_loaded_image, validate_pair)
from prepare_portable_release import api, qualified_runs
from release_artifacts import select_artifacts, download_artifact
from run_container_smoke import smoke


def resolved_tag(repository, tag):
    obj = api(repository, "git/ref/tags/" + tag).get("object", {})
    for _ in range(5):
        if obj.get("type") == "commit":
            return obj.get("sha")
        require(obj.get("type") == "tag", "Release tag does not point to a commit")
        obj = api(repository, "git/tags/" + obj["sha"]).get("object", {})
    raise ValueError("Release tag indirection exceeds limit")


def released_source(repository, sha, version):
    require(api(repository, "git/ref/heads/main").get("object", {}).get("sha") == sha,
            "Main advanced; qualify the current main")
    tags = {}
    for prefix in ("v", "python-v", "desktop-v"):
        tag = prefix + version
        release = api(repository, "releases/tags/" + tag)
        require(release.get("tag_name") == tag and release.get("draft") is False
                and release.get("prerelease") is False and release.get("target_commitish") == sha
                and resolved_tag(repository, tag) == sha, "Three matching formal releases are required")
        tags[tag] = {"id": release["id"], "url": release["html_url"], "commit": sha}
    return tags


def qualifying_runs(repository, selected):
    require(selected.get("path") == CANDIDATE_WORKFLOW and selected.get("event") == "push"
            and selected.get("head_branch") == "main" and selected.get("run_attempt") == 1
            and selected.get("status") == "completed" and selected.get("conclusion") == "success"
            and selected.get("head_repository", {}).get("full_name") == repository,
            "Select an original successful container run on exact main")
    sha = selected["head_sha"]
    runs = api(repository, "actions/runs?head_sha=" + sha + "&event=push&per_page=100")
    rows = runs.get("workflow_runs", [])
    require(runs.get("total_count") == len(rows), "Incomplete main workflow listing")
    containers = [row for row in rows if row.get("path") == CANDIDATE_WORKFLOW
                  and row.get("event") == "push" and row.get("head_branch") == "main"]
    require(len(containers) == 1 and containers[0]["id"] == selected["id"]
            and containers[0]["run_attempt"] == selected["run_attempt"], "Ambiguous container main run")
    windows = [row for row in rows if row.get("path") == ".github/workflows/windows-portable.yml"
               and row.get("event") == "push" and row.get("head_branch") == "main"]
    require(len(windows) == 1, "Ambiguous Windows qualification source")
    # Existing eleven mandatory workflows remain unchanged.
    result = qualified_runs(repository, windows[0])
    result[CANDIDATE_WORKFLOW] = {"id": selected["id"], "attempt": selected["run_attempt"]}
    return result


def package_exists(repository):
    owner, name = repository.split("/")
    secret = os.environ.get("GH_TOKEN", "")
    require(secret, "Missing workflow credential")
    request = urllib.request.Request(
        "https://api.github.com/users/" + owner + "/packages/container/" + name.lower(),
        headers={"Authorization": "Bearer " + secret, "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            value = json.loads(response.read(100_001))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise ValueError("Cannot establish package inventory (HTTP " + str(error.code) + ")") from None
    require(value.get("package_type") == "container"
            and value.get("name", "").lower() == name.lower()
            and value.get("owner", {}).get("login", "").lower() == owner.lower(),
            "Unexpected package namespace")
    return True


def require_unused(registry, tags):
    for tag in tags:
        require(registry.manifest(tag, missing_ok=True) is None, "Container tag already exists: " + tag)


def docker_login(environment, repository):
    image_name(repository)
    actor, secret = os.environ.get("GITHUB_ACTOR", ""), os.environ.get("GH_TOKEN", "")
    require(actor and secret, "Missing workflow registry credential")
    command(["docker", "login", "ghcr.io", "--username", actor, "--password-stdin"],
            input_text=secret, env=environment, timeout=60)


def create_index(image, tag, children, environment):
    # Build a manifest list from immutable child digests, never mutable platform tags.
    command(["docker", "manifest", "create", image + ":" + tag,
             *(image + "@" + children[arch] for arch in ARCHES)], env=environment, timeout=120)
    command(["docker", "manifest", "push", image + ":" + tag], env=environment, timeout=120)


def verify_registry_index(registry, reference, children, candidates):
    result = registry.manifest(reference)
    validate_index(result["manifest"], children)
    for arch in ARCHES:
        child = registry.manifest(children[arch])["manifest"]
        require(child.get("config", {}).get("digest") == candidates[arch]["manifest"]["config_digest"],
                "Published image config differs from tested image")
    return result["digest"]


def prepare(repository, run_id, out, *, publish=False):
    require(not out.exists(), "Publication output exists; inspect the previous result")
    out.mkdir(parents=True)
    selected = api(repository, "actions/runs/" + str(run_id))
    qualification = qualifying_runs(repository, selected)
    sha = selected["head_sha"]
    names = tuple(prefix + arch for arch in ARCHES
                  for prefix in ("container-candidate-", "container-evidence-"))
    listing = api(repository, "actions/runs/" + str(run_id) + "/artifacts?per_page=100")
    artifacts = select_artifacts(selected, listing, names)
    candidates = {}
    for arch in ARCHES:
        for prefix in ("container-candidate-", "container-evidence-"):
            name = prefix + arch
            download_artifact(repository, artifacts[name], out / name)
        candidate, evidence = out / ("container-candidate-" + arch), out / ("container-evidence-" + arch)
        manifest, proof = load(candidate / "manifest.json"), load(evidence / "smoke.json")
        require(manifest == load(evidence / "manifest.json"), "Candidate and evidence manifest differ")
        validate_candidate(manifest, proof, archive=candidate / "image.tar")
        require(manifest["repository"] == repository and manifest["source_revision"] == sha
                and manifest["candidate_run"] == run_id and manifest["candidate_attempt"] == selected["run_attempt"],
                "Candidate does not belong to selected run")
        candidates[arch] = {"manifest": manifest, "proof": proof}
    identity = validate_pair(candidates)
    require(api(repository, "git/commits/" + sha).get("tree", {}).get("sha") == identity["source_tree"],
            "Image source tree differs from main commit")
    releases = released_source(repository, sha, identity["version"])
    image = image_name(repository)
    result = {"schema_version": 1, "status": "qualified_not_published", "repository": repository,
              "image": image, "version": identity["version"], "source_revision": sha,
              "source_tree": identity["source_tree"], "source_payload_sha256": identity["source_payload_sha256"],
              "base_image": identity["base_image"], "candidate_run": run_id,
              "candidate_attempt": selected["run_attempt"], "ci": qualification, "releases": releases,
              "artifacts": artifacts, "candidates": candidates, "rebuilt_for_release": False,
              "anonymous_pull_verified": False, "latest_updated": False}
    save(out / "qualified.json", result)
    if not publish:
        return result
    require(int(os.environ.get("GITHUB_RUN_ID", "0")) > 0
            and os.environ.get("GITHUB_RUN_ATTEMPT") == "1",
            "Only an original publication run may mutate registry tags")
    require(qualifying_runs(repository, api(repository, "actions/runs/" + str(run_id))) == qualification,
            "Main qualification changed")
    released_source(repository, sha, identity["version"])
    version_tag, sha_tag = identity["version"], "sha-" + sha
    platform_tags = {arch: "candidate-" + str(run_id) + "-" + sha + "-" + arch for arch in ARCHES}
    registry = Registry(repository)
    if package_exists(repository):
        require_unused(registry, (version_tag, sha_tag, *platform_tags.values()))
    children = {}
    with tempfile.TemporaryDirectory(prefix="radar-container-publish-auth-") as auth:
        environment = {**os.environ, "DOCKER_CONFIG": auth}
        docker_login(environment, repository)
        for arch in ARCHES:
            candidate = out / ("container-candidate-" + arch)
            manifest = candidates[arch]["manifest"]
            command(["docker", "load", "--input", str(candidate / "image.tar")], env=environment, timeout=120)
            info = json.loads(command(["docker", "image", "inspect", manifest["image_tag"]], env=environment))[0]
            validate_loaded_image(info, manifest, config_digest=manifest["config_digest"])
            reference = image + ":" + platform_tags[arch]
            command(["docker", "tag", manifest["image_tag"], reference], env=environment)
            command(["docker", "push", reference], env=environment, timeout=300)
            published = registry.manifest(platform_tags[arch])
            require(published["manifest"].get("config", {}).get("digest") == manifest["config_digest"],
                    "Pushed platform differs from tested config")
            children[arch] = published["digest"]
            save(out / ("pushed-" + arch + ".json"),
                 {"tag": platform_tags[arch], "digest": published["digest"], "config_digest": manifest["config_digest"]})
        # In a previously absent package, the first push established namespace access.
        # Check final immutable tags again before touching either of them.
        require_unused(registry, (version_tag, sha_tag))
        released_source(repository, sha, identity["version"])
        create_index(image, version_tag, children, environment)
        digest = verify_registry_index(registry, version_tag, children, candidates)
        require_unused(registry, (sha_tag,))
        create_index(image, sha_tag, children, environment)
        require(verify_registry_index(registry, sha_tag, children, candidates) == digest,
                "Version and source tags differ")
    result.update(status="published_pending_public_verification", digest=digest,
                  reference=image + "@" + digest, platform_digests=children,
                  version_tag=version_tag, source_tag=sha_tag, platform_tags=platform_tags,
                  publication_run=int(os.environ["GITHUB_RUN_ID"]),
                  publication_attempt=int(os.environ["GITHUB_RUN_ATTEMPT"]))
    require(result["publication_attempt"] == 1, "Publication reruns require inspection")
    save(out / "publication.json", result)
    return result


def read_publication(repository, run_id, out):
    selected = api(repository, "actions/runs/" + str(run_id))
    require(selected.get("path") == PUBLISH_WORKFLOW and selected.get("event") == "workflow_dispatch"
            and selected.get("head_branch") == "main" and selected.get("run_attempt") == 1
            and selected.get("status") == "completed" and selected.get("conclusion") == "success"
            and selected.get("head_repository", {}).get("full_name") == repository,
            "Select an original successful container publication on main")
    name = "container-release-preparation"
    listing = api(repository, "actions/runs/" + str(run_id) + "/artifacts?per_page=100")
    artifact = select_artifacts(selected, listing, (name,))[name]
    require(artifact["size_in_bytes"] <= 2_000_000, "Publication metadata too large")
    download_artifact(repository, artifact, out / "publication-evidence")
    publication = load(out / "publication-evidence/publication.json")
    require(publication.get("schema_version") == 1
            and publication.get("status") == "published_pending_public_verification"
            and publication.get("repository") == repository and publication.get("image") == image_name(repository)
            and publication.get("source_revision") == selected["head_sha"]
            and publication.get("publication_run") == run_id and publication.get("publication_attempt") == 1
            and publication.get("rebuilt_for_release") is False
            and publication.get("latest_updated") is False,
            "Publication identity mismatch")
    identity = validate_pair(publication["candidates"])
    require(identity["source_revision"] == selected["head_sha"]
            and identity["repository"] == repository
            and all(publication.get(key) == identity[key] for key in (
                "version", "source_tree", "source_payload_sha256", "base_image",
                "candidate_run", "candidate_attempt"))
            and publication.get("version_tag") == identity["version"]
            and publication.get("source_tag") == "sha-" + identity["source_revision"],
            "Publication source mismatch")
    released_source(repository, identity["source_revision"], identity["version"])
    require(qualifying_runs(repository, api(repository, "actions/runs/" + str(identity["candidate_run"])))
            == publication["ci"], "Main qualification changed before public verification")
    return publication, artifact


def public_registry(publication):
    registry = Registry(publication["repository"], anonymous=True)
    digest = verify_registry_index(registry, publication["digest"], publication["platform_digests"],
                                   publication["candidates"])
    require(digest == publication["digest"]
            and publication["reference"] == publication["image"] + "@" + digest
            and registry.manifest(publication["version_tag"])["digest"] == digest
            and registry.manifest(publication["source_tag"])["digest"] == digest,
            "Published tags moved before public verification")
    return registry


def verify_public(repository, run_id, out, architecture):
    import platform
    require(not out.exists(), "Public verification output exists")
    require(architecture in ARCHES and platform.machine().lower() in {
        "amd64": {"amd64", "x86_64"}, "arm64": {"arm64", "aarch64"}}[architecture],
        "Anonymous runtime verification must use its native runner")
    verification_run = int(os.environ.get("GITHUB_RUN_ID", "0"))
    require(verification_run > 0 and os.environ.get("GITHUB_RUN_ATTEMPT") == "1",
            "Only the original public verification run is eligible")
    out.mkdir(parents=True)
    publication, artifact = read_publication(repository, run_id, out)
    public_registry(publication)
    manifest = publication["candidates"][architecture]["manifest"]
    with tempfile.TemporaryDirectory(prefix="radar-anonymous-pull-") as empty:
        environment = {**os.environ, "DOCKER_CONFIG": empty}
        # A new config directory contains no registry credentials or credential helper.
        command(["docker", "pull", "--platform", "linux/" + architecture, publication["reference"]],
                env=environment, timeout=300)
        info = json.loads(command(["docker", "image", "inspect", publication["reference"]], env=environment))[0]
        validate_loaded_image(info, manifest, config_digest=manifest["config_digest"], reference=publication["reference"])
        proof = smoke(publication["reference"], manifest, out / "anonymous-smoke.json", docker_env=environment)
        validate_candidate(manifest, proof)
    result = {**publication, "status": "public_pull_verified", "anonymous_pull_verified": True,
              "anonymous_runtime_architecture": architecture, "anonymous_smoke": proof,
              "publication_artifact": artifact, "latest_updated": False,
              "verification_run": verification_run, "verification_attempt": 1}
    save(out / "public-verification.json", result)
    return result


def validate_public_pair(publication, proofs, verification_run):
    require(set(proofs) == set(ARCHES), "Both anonymous native runtime proofs are required")
    changed = {"status", "anonymous_pull_verified", "latest_updated"}
    for arch in ARCHES:
        value = proofs[arch]
        require(isinstance(value, dict)
                and all(value.get(key) == expected for key, expected in publication.items() if key not in changed)
                and value.get("status") == "public_pull_verified"
                and value.get("anonymous_pull_verified") is True
                and value.get("anonymous_runtime_architecture") == arch
                and value.get("latest_updated") is False
                and value.get("verification_run") == verification_run
                and value.get("verification_attempt") == 1, "Anonymous platform proof mismatch")
        validate_candidate(publication["candidates"][arch]["manifest"], value.get("anonymous_smoke"))


def update_verified_latest(publication, proofs, verification_run):
    # This gate is intentionally before credentials or any mutable registry operation.
    validate_public_pair(publication, proofs, verification_run)
    released_source(publication["repository"], publication["source_revision"], publication["version"])
    registry = public_registry(publication)
    with tempfile.TemporaryDirectory(prefix="radar-container-latest-auth-") as auth:
        environment = {**os.environ, "DOCKER_CONFIG": auth}
        docker_login(environment, publication["repository"])
        create_index(publication["image"], "latest", publication["platform_digests"], environment)
    require(registry.manifest("latest")["digest"] == publication["digest"],
            "Latest digest differs from verified image")


def finalize_public(repository, publication_run, verification_run, out, *, update_latest=False):
    require(not out.exists(), "Public finalization output exists")
    require(str(verification_run) == os.environ.get("GITHUB_RUN_ID")
            and os.environ.get("GITHUB_RUN_ATTEMPT") == "1",
            "Finalization must belong to this original public verification run")
    out.mkdir(parents=True)
    publication, artifact = read_publication(repository, publication_run, out)
    selected = api(repository, "actions/runs/" + str(verification_run))
    require(selected.get("path") == ".github/workflows/verify-container-public.yml"
            and selected.get("event") == "workflow_dispatch" and selected.get("head_branch") == "main"
            and selected.get("head_sha") == publication["source_revision"]
            and selected.get("run_attempt") == 1
            and selected.get("head_repository", {}).get("full_name") == repository,
            "Public verification workflow/source mismatch")
    jobs = api(repository, "actions/runs/" + str(verification_run) + "/attempts/1/jobs?per_page=100")
    require(jobs.get("total_count") == len(jobs.get("jobs", [])), "Incomplete public verification jobs")
    listing = api(repository, "actions/runs/" + str(verification_run) + "/artifacts?per_page=100")
    proofs, artifacts = {}, {}
    for arch in ARCHES:
        matches = [job for job in jobs["jobs"] if job.get("name") == "anonymous-pull-" + arch]
        require(len(matches) == 1 and matches[0].get("status") == "completed"
                and matches[0].get("conclusion") == "success"
                and matches[0].get("run_id") == verification_run
                and matches[0].get("head_sha") == publication["source_revision"],
                "Both original anonymous jobs must have succeeded")
        job = matches[0]
        # The enclosing workflow is still running its final job. The completed native
        # producer's actual time window, not a synthetic whole-run success, binds this artifact.
        producer = {**selected, "status": job["status"], "conclusion": job["conclusion"],
                    "run_started_at": job["started_at"], "updated_at": job["completed_at"]}
        name = "container-public-" + arch
        record = select_artifacts(producer, listing, (name,))[name]
        require(record["size_in_bytes"] <= 2_000_000, "Anonymous metadata too large")
        folder = out / name
        download_artifact(repository, record, folder)
        value = load(folder / "public-verification.json")
        require(value.get("anonymous_smoke") == load(folder / "anonymous-smoke.json"),
                "Public smoke file differs from its platform proof")
        proofs[arch] = value
        artifacts[arch] = {**record, "producer_job": job["id"]}
    validate_public_pair(publication, proofs, verification_run)
    if update_latest:
        update_verified_latest(publication, proofs, verification_run)
    else:
        public_registry(publication)
    result = {**publication, "status": "public_multiplatform_verified",
              "anonymous_pull_verified": True, "anonymous_runtime_architectures": list(ARCHES),
              "latest_updated": update_latest, "verification_run": verification_run,
              "verification_attempt": 1, "publication_artifact": artifact,
              "public_artifacts": artifacts, "public_proofs": proofs}
    save(out / "public-verification.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--run-id", type=int)
    choice.add_argument("--publication-run-id", type=int)
    parser.add_argument("--verification-run-id", type=int)
    parser.add_argument("--architecture", choices=ARCHES)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--update-latest", action="store_true")
    args = parser.parse_args()
    if args.run_id:
        require(not any((args.publication_run_id, args.verification_run_id, args.architecture, args.update_latest)),
                "Invalid candidate publication mode")
        result = prepare(args.repository, args.run_id, args.out, publish=args.publish)
    elif args.verification_run_id:
        require(not args.architecture and not args.publish, "Invalid public finalization mode")
        result = finalize_public(args.repository, args.publication_run_id, args.verification_run_id,
                                 args.out, update_latest=args.update_latest)
    else:
        require(args.architecture and not args.publish and not args.update_latest,
                "Anonymous jobs cannot change registry tags")
        result = verify_public(args.repository, args.publication_run_id, args.out, args.architecture)
    print(json.dumps({key: result.get(key) for key in
                      ("status", "version", "source_revision", "reference", "anonymous_pull_verified", "latest_updated")}))
