"""Optional developer/CI acceptance using real Chromium and artificial JD fixtures.

Requires Playwright only for this test, not for the application:
    python -m pip install playwright
    python -m playwright install --with-deps chromium
    python scripts/run_browser_acceptance.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from unittest.mock import patch
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace


def main() -> int:
    from playwright.sync_api import expect, sync_playwright

    output = ROOT / "browser-acceptance"
    output.mkdir(exist_ok=True)
    result = {"created_at": datetime.now(timezone.utc).isoformat(),
              "checkout_sha": os.environ.get("GITHUB_SHA", "local-checkout"),
              "playwright": version("playwright"), "success": False,
              "fixture_note": "Artificial fixtures exercise the real-input path; NOT actual recruitment data.",
              "external_application_requests": [], "checks": [], "page_errors": []}
    try:
        with tempfile.TemporaryDirectory(prefix="radar-browser-") as tmp:
            server = LocalServer(Workspace(tmp))
            thread = threading.Thread(target=server.serve_forever,
                                      kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    result["browser_version"] = browser.version
                    context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)

                    def local_only(route):
                        if route.request.url.startswith(server.origin + "/"):
                            route.continue_()
                        else:
                            result["external_application_requests"].append(route.request.url)
                            route.abort()

                    context.route("**/*", local_only)
                    page = context.new_page()
                    page.on("pageerror", lambda error: result["page_errors"].append(str(error)))
                    try:
                        page.goto(server.entry_url)
                        expect(page.locator("#counts")).to_contain_text("真实记录 0")
                        result["checks"].append("authenticated shell loads with zero real records")
                        page.locator('summary').filter(has_text='平台接入状态与账号说明').click()
                        liepin=page.locator('#sources tbody tr').filter(has_text='猎聘')
                        expect(liepin).to_contain_text('可选单次密码提交（受控验证）')
                        expect(liepin).to_contain_text('浏览器桥的真实搜索、正常登录与完整 JD 链路仍待验收')
                        expect(page.locator('#sources tbody tr').filter(has_text='BOSS直聘')).to_contain_text('在采集浏览器人工登录')
                        expect(page.locator('#sources')).not_to_contain_text('自动登录')
                        assert server.guided.state()['jobs']==[] and not server.workspace.db.exists()
                        page.locator('summary').filter(has_text='平台接入状态与账号说明').click()
                        result["checks"].append("homepage uses the registered login capability and live-verification message without creating jobs")
                        page.goto(server.origin+'/guided')
                        expect(page.locator('#environment')).to_contain_text('Playwright')
                        expect(page.locator('p.notice').filter(has_text='猎聘任务可选用上方单次密码表单')).to_contain_text('实站登录尚未验通')
                        expect(page.get_by_text('本次版本不接收或自动填写账号密码',exact=False)).to_have_count(0)
                        assert server.guided.state()['jobs']==[]
                        page.goto(server.entry_url)
                        expect(page.locator('#counts')).to_contain_text('真实记录 0')
                        result["checks"].append("guided explanation matches the optional one-shot form and does not claim live login certification")
                        page.locator("#doctor").click()
                        expect(page.locator("#notice")).to_contain_text("workspace_writable")
                        result["checks"].append("offline doctor works from browser")
                        page.locator("#demo").click()
                        expect(page.locator("#report-title")).to_contain_text("合成演示")
                        expect(page.locator("#counts")).to_contain_text("真实记录 0")
                        assert page.locator("#requirements tbody tr").count() > 0
                        result["checks"].append("demo generates requirements without populating real database")
                        page.screenshot(path=str(output / "desktop-demo.png"), full_page=True)

                        form = page.locator("#job-form")
                        form.locator("[name=title]").fill("时间序列算法工程师")
                        form.locator("[name=company]").fill("浏览器验收虚构样本（非真实招聘）")
                        form.locator("[name=platform]").select_option("boss")
                        form.locator("[name=url]").fill("https://www.zhipin.com/job_detail/browser-test-fixture.html")
                        form.locator("[name=text]").fill("负责时间序列预测。要求熟练使用 Cursor 进行 AI 辅助编程，编写单元测试并进行代码审查。")
                        form.locator("[name=rights_note]").fill("人工编写的浏览器测试样本，仅供本次验收，不是招聘市场事实。")
                        form.locator("[name=full_text_confirmed]").check()
                        form.locator("button[type=submit]").click()
                        expect(page.locator("#counts")).to_contain_text("真实记录 1")
                        result["checks"].append("JD form saves artificial fixture through real-input workflow")
                        page.locator("#analyze").click()
                        expect(page.locator("#report-title")).to_contain_text("真实输入样本")
                        expect(page.locator("#report-stats")).to_contain_text("正文岗位组 1")
                        assert page.locator("#requirements tbody tr").count() > 0
                        result["checks"].append("analysis button renders requirement rows from the original pipeline")
                        with page.expect_download() as pending:
                            page.get_by_role("button", name="requirements_zh.csv", exact=True).click()
                        download = pending.value
                        download.save_as(str(output / "fixture-requirements.csv"))
                        text = (output / "fixture-requirements.csv").read_text(encoding="utf-8-sig")
                        assert "Cursor" in text and len(text.splitlines()) > 1
                        result["checks"].append("authenticated browser download contains extracted fixture requirements")
                        page.reload()
                        expect(page.locator("#counts")).to_contain_text("真实记录 1")
                        expect(page.locator("#runs button")).to_have_count(2)
                        result["checks"].append("reload preserves session, saved record and both historical reports")
                        page.set_viewport_size({"width": 390, "height": 844})
                        page.screenshot(path=str(output / "mobile-layout.png"), full_page=True)
                        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                        result["checks"].append("390px viewport has no page-level horizontal overflow")
                        # New real browser path, controlled provider fixture only.
                        sys.path.insert(0, str(ROOT / "tests"))
                        from test_redirect_recovery import fixture_payload
                        from vibe_job_radar.network import SafeHTTP
                        with patch.object(SafeHTTP, 'json', return_value=fixture_payload()) as source:
                            page.once('dialog', lambda dialog: dialog.accept())
                            page.locator('#public-example').click()
                            expect(page.locator('#public-status')).to_contain_text('已从真实公开接口', timeout=15000)
                            expect(page.locator('#counts')).to_contain_text('真实记录 2')
                            expect(page.locator('#report-stats')).to_contain_text('正文岗位组 1')
                            page.once('dialog', lambda dialog: dialog.accept())
                            page.locator('#public-example').click()
                            expect(page.locator('#public-status')).to_contain_text('复用10分钟', timeout=15000)
                            assert source.call_count == 1
                        result['checks'].append('public example button: consent, queued job, isolated report and cache; provider is a fixture')
                        page.screenshot(path=str(output / 'public-example-mobile.png'), full_page=True)
                        assert result["page_errors"] == [], result["page_errors"]
                        assert result["external_application_requests"] == []
                        result["success"] = True
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
    finally:
        (output / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
