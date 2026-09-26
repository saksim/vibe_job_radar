from __future__ import annotations

import html
import json
from pathlib import Path
from .utils import atomic_text, write_csv

ZH = {
    "requirement_id": "要求ID", "platform": "来源平台", "company": "招聘公司", "title": "职位名称",
    "roles": "岗位类别", "capability": "能力标签", "quote": "逐条原文", "relation": "关联类型",
    "strength": "要求强度", "tools": "明确提及工具", "evidence_level": "来源证据等级",
    "review_status": "复核状态", "url": "原始链接", "source_urls": "全部来源链接",
    "start": "原文起始索引", "end": "原文结束索引", "record_id": "对应正文ID",
    "context_quote": "上下文锚点", "is_synthetic": "是否合成样例", "notes": "审计说明",
}
LABELS = {"direct": "直接要求", "contextual": "上下文相关/待复核", "role_related": "普通岗位能力/非Vibe证据",
          "required": "硬性/明确要求", "expected": "职责期待", "preferred": "优先/加分", "unspecified": "强度未明确",
          "not_required": "不要求", "prohibited": "禁止/限制", "rule_accepted": "规则接收/非人工验真",
          "needs_review": "待人工复核", "approved": "人工已确认", "rejected": "人工已排除",
          "full_text": "职位正文", "snippet": "搜索摘要/仅线索"}


def write_requirements_zh(path: Path, rows: list[dict], config: dict) -> None:
    converted = []
    for row in rows:
        r = {}
        for k, label in ZH.items():
            value = row.get(k, "")
            if k == "capability":
                value = config["capabilities"][value]["label"]
            elif k == "platform":
                value = config["platforms"].get(value, {}).get("label", value)
            elif k == "roles":
                value = [config["roles"][x]["label"] for x in value]
            elif isinstance(value, str):
                value = LABELS.get(value, value)
            r[label] = value
        converted.append(r)
    write_csv(path, converted, list(ZH.values()))


def dashboard(path: Path, manifest: dict, summary: list[dict], rows: list[dict], claims: str, config: dict) -> None:
    esc = lambda x: html.escape(str(x), quote=True)
    is_demo = manifest["mode"] == "synthetic_demo"
    banner = "合成演示 · 非真实招聘调研 · 非本人履历" if is_demo else "样本分析 · 搜索摘要不进入正文统计 · 非全市场普查"
    stats = manifest["stats"]
    cards = [("纳入职位正文（去重后）", stats["full_text_job_groups"]),
             ("有已接收AI编程证据的职位", stats["vibe_evidence_job_groups"]),
             ("逐条能力要求/含待复核", stats["requirement_rows"]),
             ("待人工复核", stats["review_queue_rows"])]
    cards_html = "".join(f'<div class="card"><span>{esc(k)}</span><strong>{v}</strong></div>' for k, v in cards)
    summ = "".join(f'<tr><td>{esc(r["label"])}</td><td>{r["job_count"]}/{r["denominator_jobs"]}</td>'
                   f'<td><div class="bar"><i style="width:{r["sample_frequency"]*100:.1f}%"></i></div></td>'
                   f'<td>{esc(r["bucket"])}</td></tr>' for r in summary if r["scope"] == "all")
    body = []
    for r in rows:
        link = f'<a href="{esc(r["url"])}" rel="noopener noreferrer" target="_blank">来源</a>' if r["url"] else "本地导入"
        body.append('<tr>' + ''.join(f'<td>{esc(v)}</td>' for v in
                    [r["platform"], r["title"], config["capabilities"][r["capability"]]["label"], r["quote"],
                     LABELS.get(r["strength"], r["strength"]), LABELS.get(r["review_status"], r["review_status"])])
                    + f'<td>{link}<br><small>{esc(r["requirement_id"])}</small></td></tr>')
    role_rows = []
    brief = manifest.get("research_brief", {})
    for role in brief.get("roles", []):
        counts = role.get("sample_counts")
        if counts is None:
            values = [role["label"], *["未记录"] * 6, "本报告未记录方向样本计数。"]
        else:
            values = [role["label"], counts["selected_source_records"], counts["full_text_job_groups"],
                      counts["vibe_evidence_job_groups"], counts["requirement_rows"], counts["review_queue_rows"],
                      str(counts["rule_accepted_positive_rows"]) + " / " + str(counts["human_approved_positive_rows"]),
                      role["sample_note"]]
        role_rows.append("<tr>" + "".join("<td>" + esc(v) + "</td>" for v in values) + "</tr>")
    role_coverage = ('<section><h2>各方向样本覆盖</h2><p class="note">' +
                     esc(brief.get("role_sample_note", "旧报告没有记录方向样本计数。")) +
                     '</p><div class="scroll"><table id="role-sample-coverage"><thead><tr><th>方向</th>'
                     '<th>纳入来源记录</th><th>完整正文岗位（去重）</th><th>含AI证据岗位</th>'
                     '<th>要求行</th><th>待复核行</th><th>正向AI要求：规则接收 / 人工确认</th><th>下一步</th>'
                     '</tr></thead><tbody>' + "".join(role_rows) + '</tbody></table></div></section>') if role_rows else ""
    document = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Vibe Job Radar · 要求与证据</title>
<style>
:root{font-family:system-ui,"Microsoft YaHei",sans-serif;color:#182b40;background:#f3f6f9}
*{box-sizing:border-box}body{margin:0}header{background:#13273b;color:#fff;padding:40px max(24px,5vw)}
header p{color:#b3cddd;max-width:1000px;line-height:1.8}h1{font-size:32px;margin:8px 0}small{word-break:break-all}
.brand{letter-spacing:3px;font-size:12px;color:#88d9cf}.banner{display:inline-block;background:#254052;padding:8px 13px;border-radius:7px}
main{max-width:1500px;margin:auto;padding:28px}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}
.card{background:white;border:1px solid #dbe5ed;border-radius:12px;padding:20px}.card span{font-size:13px;color:#597080}.card strong{font-size:34px;display:block;margin-top:12px}
section{background:white;border:1px solid #dbe5ed;border-radius:12px;padding:24px;margin-top:24px}h2{font-size:20px;margin:0 0 18px}
.note{line-height:1.8;color:#597080;font-size:14px}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:14px}
th{text-align:left;background:#edf3f7;white-space:nowrap}th,td{padding:12px;border-bottom:1px solid #e2eaf0;vertical-align:top;line-height:1.6}
td:nth-child(4){min-width:280px;max-width:580px}a{color:#087f82}input{border:1px solid #bfcfdc;border-radius:8px;padding:12px;width:100%;margin-bottom:16px;font:inherit}
.bar{height:10px;width:220px;background:#eaf1f4;border-radius:8px}.bar i{display:block;height:100%;background:#12847f;border-radius:8px}
pre{white-space:pre-wrap;word-break:break-word;font:14px/1.9 system-ui,"Microsoft YaHei",sans-serif;color:#29465b}footer{margin:25px 0;color:#607586;font-size:13px}
@media(max-width:800px){.cards{grid-template-columns:repeat(2,1fr)}main{padding:16px}header{padding:26px}.card{padding:15px}}
</style></head><body><header><div class="brand">WEIYU AI STUDIO / VIBE JOB RADAR</div>
<h1>从招聘要求，到可举证的能力表达</h1><p>逐条证据 · 能力并集与共同项 · 分岗位措辞 · 指标口径 · 缺口复核</p>
<div class="banner">__BANNER__</div></header><main><div class="cards">__CARDS__</div>__ROLE_COVERAGE__
<section><h2>样本内能力频次</h2><p class="note">分母为当前样本内含已接收正向 AI 编程正文证据的去重职位数，不代表市场需求比例。规则接收不等于人工验真。普通岗位能力、摘要与待复核项不计入。</p>
<div class="scroll"><table><thead><tr><th>能力</th><th>职位数 / 分母</th><th>样本频次</th><th>归纳层次</th></tr></thead><tbody>__SUMMARY__</tbody></table></div></section>
<section><h2>逐条招聘要求与证据</h2><input id="query" placeholder="筛选岗位、工具、原文、状态……" aria-label="筛选要求">
<div class="scroll"><table id="requirements"><thead><tr><th>平台</th><th>职位</th><th>能力标签</th><th>原文</th><th>要求强度</th><th>复核状态</th><th>来源 / ID</th></tr></thead><tbody>__ROWS__</tbody></table></div></section>
<section><h2>可量化描述：模板与事实分离</h2><pre>__CLAIMS__</pre></section>
<footer>本页无外部脚本/CDN，不会自动上传职位或个人证据。点击来源链接会访问对应网站。__TIME__</footer></main>
<script>document.getElementById('query').addEventListener('input',function(){const q=this.value.toLowerCase();for(const r of document.querySelectorAll('#requirements tbody tr')){r.hidden=!r.textContent.toLowerCase().includes(q);}});</script>
</body></html>'''
    # Replace in one pass: source text cannot introduce a second-stage template placeholder.
    import re
    values = {"BANNER": esc(banner), "CARDS": cards_html, "SUMMARY": summ, "ROWS": "".join(body),
              "CLAIMS": esc(claims), "TIME": esc(manifest["created_at"]), "ROLE_COVERAGE": role_coverage}
    document = re.sub(r"__(BANNER|CARDS|SUMMARY|ROWS|CLAIMS|TIME|ROLE_COVERAGE)__", lambda m: values[m.group(1)], document)
    atomic_text(path, document)
