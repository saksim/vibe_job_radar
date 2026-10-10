"""Real local browser; artificial zero-AI JDs, frozen reports and no remote egress."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.workbench import LocalServer

BODY="🧪 人工软件岗位。负责数据库设计与服务维护，熟悉版本控制、离线测试和发布流程。<img src=x onerror=alert(1)>"
def source(i):
    return {"title":"软件架构师"+("x"*600 if i==2 else ""), "text":BODY+str(i),
            "platform":"manual","company":"ARTIFICIAL FIXTURE",
            "url":"https://fixture.example/job/"+str(i),
            "rights_note":"Authored offline test data, not real recruiting evidence.",
            "evidence_level":"full_text","full_text_confirmed":True}

def main():
    from playwright.sync_api import expect,sync_playwright
    out=ROOT/'browser-acceptance/report-sources';out.mkdir(parents=True,exist_ok=True)
    result={"success":False,"checks":[],"page_errors":[],"external_browser_requests":[],
            "scope":"Artificial materials in actual local app/browser. No personal evidence or real platform acceptance."}
    def hashes(files):return {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    try:
        with tempfile.TemporaryDirectory(prefix="report-sources-") as tmp:
            w=Workspace(tmp);w.add_job(source(1));first=w.analyze({})
            w.add_job(source(2));second=w.analyze({})
            a=json.loads(w.report_file(first["id"],"jobs.jsonl").read_text(encoding="utf8"))
            rows=[json.loads(line) for line in w.report_file(second["id"],"jobs.jsonl").read_text(encoding="utf8").splitlines()]
            b=next(row for row in rows if row["record_id"]!=a["record_id"])
            assert first["manifest"]["stats"]["requirement_rows"]==second["manifest"]["stats"]["requirement_rows"]==0
            server=LocalServer(w);worker=threading.Thread(target=server.serve_forever,kwargs={"poll_interval":.01},daemon=True);worker.start()
            files=[w.db,server.evidence.db,*(w.root/"reports").rglob("*")]
            files=[p for p in files if p.is_file()];before=hashes(files)
            try:
                with sync_playwright() as pw:
                    options={"headless":True}
                    if os.environ.get("RADAR_TEST_CHROMIUM"):options["executable_path"]=os.environ["RADAR_TEST_CHROMIUM"]
                    browser=pw.chromium.launch(**options)
                    try:
                        context=browser.new_context(viewport={"width":1280,"height":960})
                        def local(route):
                            if route.request.url.startswith(server.origin+"/"):route.continue_()
                            else:result["external_browser_requests"].append(route.request.url);route.abort()
                        context.route("**/*",local)
                        page=context.new_page();page.on("pageerror",lambda e:result["page_errors"].append(str(e)))
                        # Load the report together with the initial local session;
                        # a same-document hash change does not reload app startup.
                        page.goto(server.entry_url+"&report="+first["id"])
                        expect(page.locator("#counts")).to_contain_text("真实记录 2")
                        expect(page.locator("#evidence-next")).to_have_attribute("href","/advanced#report="+first["id"])
                        viewer=page.locator("#report-sources")
                        viewer.get_by_role("button",name="查看本报告的岗位原文",exact=True).click()
                        expect(viewer.locator("details")).to_have_count(1)
                        viewer.locator("summary").click()
                        expect(viewer.locator('[data-role="source-text"]')).to_have_text(BODY+"1")
                        assert viewer.locator("img").count()==0
                        viewer.screenshot(path=str(out/"zero-ai-report.png"))
                        result["checks"].append("zero-AI report displays its exact saved JD as text, with no injected markup or borrowed later job")
                        page.locator("#evidence-next").click()
                        expect(page.locator("#source-run")).to_have_value(first["id"])
                        expect(page.locator("#load-requirements")).to_be_enabled()
                        expect(page.locator("#pagination")).to_contain_text("共 0 条")
                        viewer=page.locator("#report-sources")
                        viewer.get_by_role("button",name="查看本报告的岗位原文",exact=True).click()
                        expect(viewer.locator("details")).to_have_count(1)
                        viewer.locator("summary").click()
                        expect(viewer.locator('[data-role="source-text"]')).to_have_text(BODY+"1")
                        expect(page.locator("#revision")).to_contain_text("已保存证据 0")
                        viewer.screenshot(path=str(out/"zero-ai-evidence.png"))
                        result["checks"].append("same report passes into the personal evidence page automatically and remains readable with zero requirements or personal evidence")
                        page.locator("#source-run").select_option(second["id"])
                        expect(viewer).to_be_hidden()
                        page.locator("#load-requirements").click()
                        expect(viewer).to_be_visible()
                        viewer.get_by_role("button",name="查看本报告的岗位原文",exact=True).click()
                        expect(viewer.locator("details")).to_have_count(2)
                        current=viewer.locator('[data-record-id="'+b["record_id"]+'"]')
                        current.locator("summary").click()
                        expect(current.locator("pre")).to_have_text(BODY+"2")
                        page.set_viewport_size({"width":390,"height":844})
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                        viewer.screenshot(path=str(out/"long-title-mobile.png"))
                        result["checks"].append("explicit report switch resets the viewer; selected second snapshot and long titles remain usable on mobile")
                        # Deterministic delayed promises exercise races without slow
                        # networks, sleeps, reanalysis or polling supplier pages.
                        race=page.evaluate("""async ([first, second]) => {
                          const root=document.createElement('section');document.body.append(root);
                          const pending=[];
                          const viewer=window.createReportSources(root,(path,data)=>new Promise(resolve=>pending.push({path,data,resolve})));
                          const flush=()=>new Promise(resolve=>requestAnimationFrame(resolve));
                          const list=(row,run)=>({run_id:run,page:0,page_size:20,total:1,rows:[row]});
                          const show=()=>root.querySelector('button').click();
                          viewer.setRun(first.run);show();
                          viewer.setRun(second.run);show();
                          pending[1].resolve(list(second.row,second.run));await flush();
                          pending[0].resolve(list(first.row,first.run));await flush();
                          if(root.querySelector('details').dataset.recordId!==second.row.record_id)throw new Error('late list replaced current report');
                          root.querySelector('details').open=true;await flush();
                          if(pending.length!==3)throw new Error('body was not requested');
                          viewer.setRun(first.run);show();
                          pending[3].resolve(list(first.row,first.run));await flush();
                          root.querySelector('details').open=true;await flush();
                          pending[4].resolve({run_id:first.run,record:{...first.row,text:first.text}});await flush();
                          pending[2].resolve({run_id:second.run,record:{...second.row,text:second.text}});await flush();
                          if(root.querySelector('pre').textContent!==first.text)throw new Error('late body replaced current report');
                          root.remove();return true;
                        }""",[{"run":first["id"],"row":a,"text":BODY+"1"},{"run":second["id"],"row":b,"text":BODY+"2"}])
                        assert race
                        result["checks"].append("late list and body responses cannot replace a newly selected report")
                        assert not result["page_errors"] and not result["external_browser_requests"]
                        assert before==hashes(files)
                        assert server.evidence.state()["revision"]==0 and not server.evidence.state()["evidence"]
                        result["checks"].append("all original JD/report/evidence files remain byte-identical; no remote requests or personal evidence writes")
                        result["success"]=True
                    finally:browser.close()
            finally:server.shutdown();server.server_close();worker.join(timeout=5)
    finally:(out/"results.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf8")
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=="__main__":main()
