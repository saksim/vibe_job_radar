"""Check a PR's promotion route and release outcomes; no network or writes."""
from __future__ import annotations

import json
import os
from pathlib import Path

from release_summary import validate_summary


def validate_promotion(event):
    pr = event["pull_request"]
    base = pr["base"]["ref"]
    if base == "dev":
        return "Feature changes target dev"
    expected = {"release": "dev", "main": "release"}.get(base)
    if expected is None:
        return "Non-promotion branch"
    head = pr["head"]
    repository = event["repository"]["full_name"]
    if (head["ref"] != expected
            or (head.get("repo") or {}).get("full_name") != repository
            or (pr["base"].get("repo") or {}).get("full_name") != repository):
        raise ValueError(f"Promotion to {base} must come from this repository's {expected} branch")
    validate_summary(pr.get("body"))
    return f"Validated promotion: {expected} -> {base}"


def main():
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        print(validate_promotion(event))
    except (KeyError, TypeError, ValueError, OSError) as error:
        print(f"Promotion refused: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
