"""Report snapshot readers use authored fixtures, real storage and local HTTP."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vibe_job_radar.evidence_ui import EvidenceService
from vibe_job_radar.workspace import InputError, Workspace
import test_workbench as legacy

BODY = "🧪 合成岗位材料。负责数据库设计与服务维护，熟悉版本控制、离线测试和发布流程。<img src=x onerror=alert(1)>"
def job(number=1, **changes):
    return legacy.capture(title="架构师 "+str(number), text=BODY+str(number),
                          url="https://www.zhipin.com/job_detail/source-"+str(number)+".html", **changes)

class ReportSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.w = Workspace(self.tmp.name)
        self.w.add_job(job())
        self.report = self.w.analyze({})
        self.run_id = self.report["id"]
        self.e = EvidenceService(self.w)
        self.record = json.loads(self.w.report_file(self.run_id,"jobs.jsonl").read_text(encoding="utf8"))

    def test_zero_ai_sources_preserve_full_text_without_writes_or_requests(self):
        self.assertEqual(self.report["manifest"]["stats"]["requirement_rows"],0)
        files=[self.w.db,self.e.db,*self.w.report_file(self.run_id,"jobs.jsonl").parent.iterdir()]
        before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        with patch("socket.create_connection",side_effect=AssertionError("no network")), \
                patch("vibe_job_radar.evidence_ui.analyze",side_effect=AssertionError("no reanalysis")):
            result=self.e.sources({"run_id":self.run_id})
            self.assertEqual((result["total"],len(result["rows"])),(1,1))
            self.assertNotIn("text",result["rows"][0])
            body=self.e.source({"run_id":self.run_id,"record_id":self.record["record_id"]})
            self.assertEqual(body["run_id"],self.run_id)
            self.assertEqual(body["record"]["text"],BODY+"1")
        self.assertEqual(before,{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files})
        self.assertEqual(self.e.state()["revision"],0)

    def test_source_membership_is_bound_to_the_frozen_report(self):
        self.w.add_job(job(2))
        later=self.w.analyze({})
        new=[r for r in self.e.sources({"run_id":later["id"]})["rows"] if r["record_id"]!=self.record["record_id"]][0]
        self.assertEqual(self.e.sources({"run_id":self.run_id})["total"],1)
        with self.assertRaises(InputError):
            self.e.source({"run_id":self.run_id,"record_id":new["record_id"]})
        self.assertEqual(self.e.source({"run_id":later["id"],"record_id":new["record_id"]})["record"]["text"],BODY+"2")

    def test_pagination_is_bounded_and_snippets_keep_their_level(self):
        for i in range(2,22):
            self.w.add_job(job(i,evidence_level="snippet",full_text_confirmed=False))
        report=self.w.analyze({})
        first=self.e.sources({"run_id":report["id"],"page":0})
        last=self.e.sources({"run_id":report["id"],"page":1})
        self.assertEqual((first["total"],first["page_size"],len(first["rows"]),len(last["rows"])),(21,20,20,1))
        self.assertEqual(len({r["record_id"] for r in first["rows"]+last["rows"]}),21)
        snippet=next(r for r in first["rows"]+last["rows"] if r["evidence_level"]=="snippet")
        self.assertEqual(self.e.source({"run_id":report["id"],"record_id":snippet["record_id"]})["record"]["evidence_level"],"snippet")
        for page in (-1,True,1.5,"1",2,100001):
            with self.subTest(page=page),self.assertRaises(InputError):
                self.e.sources({"run_id":report["id"],"page":page})

    def test_metadata_bound_does_not_truncate_the_requested_body(self):
        self.w.add_job(legacy.capture(title="架构师"+"a"*1500,text="职责："+("原文"*74000),
                                     url="https://www.zhipin.com/job_detail/long-source.html"))
        report=self.w.analyze({})
        row=next(r for r in self.e.sources({"run_id":report["id"]})["rows"] if r["title_is_excerpt"])
        self.assertEqual(len(row["title"]),500)
        body=self.e.source({"run_id":report["id"],"record_id":row["record_id"]})["record"]["text"]
        self.assertEqual(body,"职责："+("原文"*74000))

    def test_tampered_or_missing_snapshots_are_not_replaced_with_database_rows(self):
        for name in ("jobs.jsonl","requirements.jsonl","effective_config.json"):
            p=self.w.report_file(self.run_id,name);original=p.read_bytes()
            try:
                p.write_bytes(original+b" ")
                for method,data in ((self.e.sources,{"run_id":self.run_id}),
                                    (self.e.source,{"run_id":self.run_id,"record_id":self.record["record_id"]})):
                    with self.subTest(file=name,method=method.__name__),self.assertRaises(InputError):
                        method(data)
            finally:p.write_bytes(original)
        p=self.w.report_file(self.run_id,"jobs.jsonl");p.unlink()
        with self.assertRaises(InputError):self.e.sources({"run_id":self.run_id})

    def test_duplicate_record_ids_are_ambiguous_even_when_file_hash_matches(self):
        p=self.w.report_file(self.run_id,"jobs.jsonl");p.write_bytes(p.read_bytes()*2)
        m=self.w.report_file(self.run_id,"run_manifest.json");manifest=json.loads(m.read_bytes())
        manifest["output_files_sha256"]["jobs.jsonl"]=hashlib.sha256(p.read_bytes()).hexdigest()
        m.write_text(json.dumps(manifest),encoding="utf8")
        with self.assertRaisesRegex(InputError,"重复岗位"):
            self.e.sources({"run_id":self.run_id})
        with self.assertRaisesRegex(InputError,"重复岗位"):
            self.e.source({"run_id":self.run_id,"record_id":self.record["record_id"]})

    def test_invalid_scope_and_demo_inputs_do_not_select_another_report(self):
        for data in ({},{"run_id":"../outside"},{"run_id":"a"*32},{"run_id":self.run_id,"url":"https://example.test"},
                     {"run_id":self.run_id,"record_id":self.record["record_id"]}):
            with self.subTest(data=data),self.assertRaises(InputError):self.e.sources(data)
        for rid in ("",0,"j_"+"0"*24,"../jobs.sqlite"):
            with self.subTest(rid=rid),self.assertRaises(InputError):
                self.e.source({"run_id":self.run_id,"record_id":rid})
        demo=self.w.analyze({"dataset":"demo"})
        with self.assertRaises(InputError):self.e.sources({"run_id":demo["id"]})

class ReportSourceHTTPTests(unittest.TestCase):
    setUp=legacy.HTTPTests.setUp
    tearDown=legacy.HTTPTests.tearDown
    call=legacy.HTTPTests.call

    def test_read_endpoints_keep_token_and_origin_checks_and_no_store(self):
        self.server.workspace.add_job(job())
        report=self.server.workspace.analyze({})
        data={"run_id":report["id"]}
        self.assertEqual(self.call("/api/evidence/sources",data,authorized=False)[0],403)
        self.assertEqual(self.call("/api/evidence/sources",data,headers={"Origin":"https://evil.example"})[0],403)
        code,headers,raw=self.call("/api/evidence/sources",data)
        self.assertEqual(code,200);self.assertEqual(headers["Cache-Control"],"no-store")
        row=json.loads(raw)["rows"][0]
        data["record_id"]=row["record_id"]
        self.assertEqual(self.call("/api/evidence/source",data,authorized=False)[0],403)
        self.assertEqual(self.call("/api/evidence/source",data,headers={"Origin":"https://evil.example"})[0],403)
        code,_,raw=self.call("/api/evidence/source",data)
        self.assertEqual(code,200);self.assertEqual(json.loads(raw)["record"]["text"],BODY+"1")
        code,headers,raw=self.call("/report-sources.js",authorized=False)
        self.assertEqual(code,200);self.assertIn(b"createReportSources",raw)
        self.assertEqual(self.server.evidence.state()["revision"],0)
