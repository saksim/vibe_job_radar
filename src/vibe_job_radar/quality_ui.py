"""Versioned independent JD labels in the local workbench; reports stay immutable."""
from __future__ import annotations

from contextlib import closing
import copy
import io
import json
import re
import sqlite3
import uuid
import zipfile

from .evidence_ui import Conflict
from .quality_evaluation import evaluate_annotations, validate_annotations
from .quality_packet import PACKET_FILES, create_packet, decode_json, load_packet, sha256
from .utils import json_text, utc_now
from .workspace import InputError, text_field

IDENT = re.compile(r"[a-f0-9]{32}")


class QualityService:
    def __init__(self, workspace):
        self.workspace = workspace
        self.root = workspace.root / "quality"

    def _connect(self):
        if self.root.is_symlink():
            raise InputError("质量复核目录不能使用符号链接。")
        self.root.mkdir(exist_ok=True, mode=0o700)
        db = self.root / "history.sqlite"
        if db.is_symlink():
            raise InputError("质量复核历史不能使用符号链接。")
        existed = db.exists()
        connection = sqlite3.connect(db.resolve().as_uri() + ("?mode=rw" if existed else "?mode=rwc"), uri=True, timeout=15)
        try:
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not existed and version == 0 and not tables:
                connection.execute("CREATE TABLE packets(report_id TEXT PRIMARY KEY, packet_name TEXT NOT NULL)")
                connection.execute("CREATE TABLE revisions(report_id TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(report_id, revision))")
                connection.execute("PRAGMA user_version=1")
            elif version != 1 or tables != {"packets", "revisions"}:
                raise InputError("质量复核历史损坏或版本不兼容；未重建或清空标注，请保留文件并检查工作区。")
            connection.commit()
        except Exception:
            connection.close()
            raise
        return connection

    def _report(self, data):
        run_id = text_field(data, "report_id", required=True, limit=32)
        if not IDENT.fullmatch(run_id):
            raise InputError("请选择有效报告。")
        report = self.workspace.report_file(run_id, "run_manifest.json").parent
        return run_id, report

    def _packet(self, connection, run_id, report, *, create=False):
        row = connection.execute("SELECT packet_name FROM packets WHERE report_id=?", (run_id,)).fetchone()
        parent = self.root / run_id
        if parent.is_symlink():
            raise InputError("质量包路径无效。")
        if row is None:
            if not create:
                raise InputError("请先打开这份报告的质量复核。")
            name = uuid.uuid4().hex
            packet = parent / name
            create_packet(report, packet)
            raw = (packet / "annotations.json").read_text(encoding="utf-8")
            connection.execute("INSERT INTO packets VALUES(?,?)", (run_id, name))
            connection.execute("INSERT INTO revisions VALUES(?,?,?,?)", (run_id, 0, raw, utc_now()))
        else:
            name = row[0]
            if not isinstance(name, str) or not IDENT.fullmatch(name):
                raise InputError("质量包索引无效。")
            packet = parent / name
        if packet.is_symlink() or (packet / "packet_manifest.json").is_symlink():
            raise InputError("质量包不能使用符号链接。")
        manifest, contents = load_packet(packet)
        return packet, manifest, contents

    @staticmethod
    def _current(connection, run_id):
        row = connection.execute(
            "SELECT revision,body FROM revisions WHERE report_id=? ORDER BY revision DESC LIMIT 1",
            (run_id,)).fetchone()
        if row is None:
            raise InputError("质量标注历史不完整。")
        return row[0], row[1].encode("utf-8")

    @staticmethod
    def _response(run_id, revision, manifest, contents, raw):
        annotations = decode_json(raw)
        result = evaluate_annotations(manifest, contents, raw)
        # Independent labels start with the source, without showing model predictions.
        corpus = [{key: value for key, value in document.items() if key != "predicted_roles"}
                  for document in contents["corpus"]]
        return {"report_id": run_id, "revision": revision, "packet_id": manifest["packet_id"],
                "corpus": corpus, "labels": contents["labels"], "annotations": annotations,
                "evaluation": result, "source_mode": contents["source"]["mode"]}

    def open(self, data):
        run_id, report = self._report(data)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            _, manifest, contents = self._packet(connection, run_id, report, create=True)
            revision, raw = self._current(connection, run_id)
            result = self._response(run_id, revision, manifest, contents, raw)
        return result

    def _write(self, data, mutate):
        run_id, report = self._report(data)
        expected = data.get("expected_revision")
        if type(expected) is not int or expected < 0:
            raise InputError("缺少有效的标注版本，请刷新。")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            _, manifest, contents = self._packet(connection, run_id, report)
            revision, previous = self._current(connection, run_id)
            if revision != expected:
                raise Conflict("其他窗口已保存标注；本页输入仍保留，请重新打开报告核对。")
            annotations = decode_json(previous)
            value = mutate(annotations)
            validate_annotations(value, manifest, contents)
            raw = (json_text(value) + "\n").encode("utf-8")
            result = self._response(run_id, revision + 1, manifest, contents, raw)
            connection.execute("INSERT INTO revisions VALUES(?,?,?,?)",
                               (run_id, revision + 1, raw.decode("utf-8"), utc_now()))
        return result

    def save(self, data):
        row = data.get("document")
        if not isinstance(row, dict):
            raise InputError("缺少当前 JD 的标注。")
        if row.get("review_state") == "human_confirmed" and data.get("confirm_human") is not True:
            raise InputError("请由复核者本人明确确认；草稿不计入质量指标。")

        def mutate(annotations):
            for index, existing in enumerate(annotations["documents"]):
                if existing["record_id"] == row.get("record_id"):
                    saved = copy.deepcopy(row)
                    saved["reviewed_at"] = utc_now()
                    annotations["documents"][index] = saved
                    return annotations
            raise InputError("标注记录不属于这份报告。")
        return self._write(data, mutate)

    def save_pair(self, data):
        row = data.get("pair")
        if not isinstance(row, dict) or not all(isinstance(row.get(key), str) for key in ("left", "right")):
            raise InputError("请选择两份有效的 JD。")
        if row.get("review_state") == "human_confirmed" and data.get("confirm_human") is not True:
            raise InputError("文档对需要复核者本人明确确认；草稿不计入指标。")

        def mutate(annotations):
            saved = copy.deepcopy(row)
            saved["reviewed_at"] = utc_now()
            identity = tuple(sorted((saved["left"], saved["right"])))
            for index, existing in enumerate(annotations["dedup_pairs"]):
                if tuple(sorted((existing["left"], existing["right"]))) == identity:
                    annotations["dedup_pairs"][index] = saved
                    return annotations
            annotations["dedup_pairs"].append(saved)
            return annotations
        return self._write(data, mutate)

    def import_annotations(self, data):
        value = data.get("annotations")
        if "annotations_json" in data:
            if value is not None or not isinstance(data["annotations_json"], str):
                raise InputError("标注输入格式无效。")
            value = decode_json(data["annotations_json"])
        if not isinstance(value, dict):
            raise InputError("请选择有效的 annotations.json。")
        if any(isinstance(row, dict) and row.get("review_state") == "human_confirmed"
               for key in ("documents", "dedup_pairs")
               if isinstance(value.get(key), list) for row in value[key]) and data.get("confirm_human") is not True:
            raise InputError("导入含真人确认的标注，需要明确确认其来源。")
        return self._write(data, lambda previous: copy.deepcopy(value))

    def export(self, data):
        run_id, report = self._report(data)
        expected = data.get("expected_revision")
        if type(expected) is not int or expected < 0:
            raise InputError("缺少有效的导出版本，请重新打开报告。")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN")
            packet, manifest, contents = self._packet(connection, run_id, report)
            revision, raw = self._current(connection, run_id)
            if revision != expected:
                raise Conflict("标注版本已更新；请重新打开报告核对后再导出。")
            evaluation = evaluate_annotations(manifest, contents, raw)
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name in PACKET_FILES:
                    path = packet / name
                    content = path.read_bytes()
                    if path.is_symlink() or sha256(content) != manifest["files_sha256"][name]:
                        raise InputError("质量包内容已变化，不能导出。")
                    archive.writestr(name, content)
                archive.writestr("packet_manifest.json", json_text(manifest) + "\n")
                archive.writestr("annotations.json", raw)
                archive.writestr("quality-evaluation.json", json_text(evaluation) + "\n")
                archive.writestr("review-version.json", json_text({
                    "report_id": run_id, "revision": revision,
                    "annotations_sha256": sha256(raw)}) + "\n")
        return buffer.getvalue()
