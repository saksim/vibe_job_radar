"""Published reports, independent of live source records and UI transports."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from itertools import islice
from pathlib import Path
import re

from .application_errors import InputError

RUN_ID = re.compile(r"[a-f0-9]{32}")


class ReportRepository:
    def __init__(self, root: Path | str):
        self.root = Path(root)

    def list(self) -> list[dict]:
        """Keep the existing published-only, newest-thirty report catalogue."""
        runs = []
        for path in self.root.iterdir():
            if not RUN_ID.fullmatch(path.name) or path.is_symlink():
                continue
            manifest = path / "run_manifest.json"
            if manifest.is_file():
                try:
                    report = json.loads(manifest.read_text(encoding="utf-8"))
                    runs.append({"id": path.name, "created_at": report["created_at"],
                                 "mode": report["mode"], "stats": report["stats"]})
                except (ValueError, KeyError, OSError):
                    continue
        return sorted(runs, key=lambda x: x["created_at"], reverse=True)[:30]

    def file(self, run_id: str, name: str) -> Path:
        if not RUN_ID.fullmatch(run_id) or not name or Path(name).name != name or "\\" in name:
            raise InputError("无效报告路径。")
        directory = self.root / run_id
        root = self.root.resolve()
        path = (directory / name).resolve()
        if (directory.is_symlink() or not path.is_relative_to(root / run_id)
                or not (directory / "run_manifest.json").is_file() or not path.is_file()):
            raise InputError("报告不存在或路径不在允许目录内。")
        return path

    def _verified_bytes(self, run_id: str, name: str, manifest: dict) -> bytes:
        raw = self.file(run_id, name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != manifest["output_files_sha256"].get(name):
            raise InputError("个人报告内容已改变；请保留原文件并检查报告，未重新生成或覆盖。")
        return raw

    def read(self, run_id: str) -> dict:
        manifest_path = self.file(run_id, "run_manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        stats = manifest["stats"]
        if stats["full_text_job_groups"] == 0:
            message = "没有纳入完整 JD：请补齐正文、核对岗位匹配和采集新鲜度。不能据此归纳市场要求。"
        elif stats["accepted_positive_requirement_rows"] == 0:
            message = "已有正文，但没有已接受的正向 Vibe Coding 要求；请查看原文、否定条款与复核队列。"
        else:
            message = "报告已生成。通用能力并集不等于个人完全满足；没有本人证据的描述仍是待填模板。"
        with self.file(run_id, "requirements.jsonl").open(encoding="utf-8") as stream:
            requirements = [json.loads(line) for line in islice(stream, 100) if line.strip()]
        result = {"id": run_id, "manifest": manifest, "message": message, "requirements": requirements,
                  "descriptions": self.file(run_id, "descriptions.md").read_text(encoding="utf-8"),
                  "files": sorted(p.name for p in manifest_path.parent.iterdir() if p.is_file() and not p.is_symlink())}
        # A loose file is not a personal-report marker. Only the lineage already
        # bound into the published manifest enables its existing coverage view.
        if "evidence_revision.json" in manifest.get("output_files_sha256", {}):
            self._verified_bytes(run_id, "evidence_revision.json", manifest)
            for key, name, limit in (("coverage", "job_coverage.csv", None),
                                     ("matrix", "requirement_evidence_matrix.csv", 200)):
                raw = self._verified_bytes(run_id, name, manifest)
                rows = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
                result[key] = list(rows) if limit is None else list(islice(rows, limit))
        return result
