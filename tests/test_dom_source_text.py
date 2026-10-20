"""Synthetic parser regressions, not live-site or human quality certification."""
import json
from pathlib import Path
import tempfile
import unittest

from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot
from vibe_job_radar.html_parser import ParseError, parse_job_html
from vibe_job_radar.ingest import iter_items
from vibe_job_radar.models import JobRecord
from vibe_job_radar.pipeline import analyze
from vibe_job_radar.quality_packet import create_packet, load_packet
from vibe_job_radar.store import Store

FIXED = "2026-10-20T00:00:00+00:00"
TITLE = "人工架构师"
PREFIX = "岗位职责：负责系统架构设计、研发与自动化测试。任职要求："
ENCODED = PREFIX + "掌握 std::vector&lt;T&gt; 和 Map&lt;String, Integer&gt;，使用 &lt;script&gt; 示例，保留 &amp;lt; 原文。"
BODY = PREFIX + "掌握 std::vector<T> 和 Map<String, Integer>，使用 <script> 示例，保留 &lt; 原文。"
SITES = [
    ("boss", "https://www.zhipin.com/job_detail/fixture.html", "job-sec-text"),
    ("51job", "https://we.51job.com/pc/jobdetail?jobId=123", "job_msg"),
]


def markup(body=ENCODED, container="job-description"):
    return f'<h1>{TITLE}</h1><div class="{container}">{body}</div>'


class DOMSourceTextTests(unittest.TestCase):
    def test_decoded_dom_text_is_not_parsed_as_html_again(self):
        self.assertEqual(parse_job_html(markup())["text"], BODY)

    def test_original_html_report_and_review_packet_keep_exact_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "job.html"
            original = markup().encode("utf-8")
            source.write_bytes(original)
            source.with_name("job.html.meta.json").write_text(json.dumps({
                "collected_at": FIXED, "is_synthetic": True,
                "source_mode": "synthetic", "platform": "manual",
                "url": "https://example.com/job/fixture",
            }), encoding="utf-8")
            rows = list(iter_items(source))
            self.assertEqual(len(rows), 1)
            record = rows[0][1]
            self.assertIsInstance(record, JobRecord)
            self.assertEqual(record.text, BODY)
            with Store(root / "jobs.sqlite") as store:
                store.add(record)
            report = root / "report"
            analyze(root / "jobs.sqlite", report, demo_mode=True, as_of=FIXED)
            saved = json.loads((report / "jobs.jsonl").read_text(encoding="utf-8").strip())
            self.assertEqual(saved["text"], BODY)
            packet = root / "review"
            result = create_packet(report, packet)
            self.assertEqual(result["human_confirmed_documents"], 0)
            _, contents = load_packet(packet)
            self.assertEqual(contents["corpus"][0]["text"], BODY)
            self.assertEqual(source.read_bytes(), original)

    def test_explicitly_hidden_login_templates_do_not_block_detail(self):
        for site, url, container in SITES:
            for hidden in (
                '<div hidden>登录后查看完整内容</div>',
                '<template>安全验证</template>',
                '<div style="display: none !important">登录后查看</div>',
                '<section style="visibility:collapse"><div>安全验证</div></section>',
            ):
                for observed in (None, TITLE + "\n" + BODY):
                    with self.subTest(site=site, hidden=hidden, observed=observed is not None):
                        page = PageSnapshot(url, markup(container=container) + hidden,
                                            visible_text=observed)
                        self.assertEqual(builtins().get(site).detail(page)["text"], BODY)

    def test_rendered_surface_excludes_css_dialog_but_offline_stays_conservative(self):
        hidden = '<style>.dialog{display:none}</style><div class="dialog">登录后查看</div>'
        for site, url, container in SITES:
            adapter = builtins().get(site)
            html = markup(container=container) + hidden
            with self.subTest(site=site):
                result = adapter.detail(PageSnapshot(url, html, visible_text=TITLE + "\n" + BODY))
                self.assertEqual(result["text"], BODY)
                with self.assertRaisesRegex(CrawlError, "manual_required"):
                    adapter.detail(PageSnapshot(url, html))

    def test_hidden_ancestors_and_inert_duplicates_are_not_title_or_body_candidates(self):
        for hidden in (
            '<section hidden>{}</section>', '<template>{}</template>',
            '<div style="visibility: hidden">{}</div>', '<noscript>{}</noscript>',
        ):
            with self.subTest(hidden=hidden):
                result = parse_job_html(hidden.format(markup("另一份不应进入本岗位的隐藏文本" * 3)) + markup())
                self.assertEqual(result["title"], TITLE)
                self.assertEqual(result["text"], BODY)

    def test_visible_ambiguity_and_hidden_only_body_are_still_rejected(self):
        for html in (markup() + markup(), '<div hidden>' + markup() + '</div>',
                     '<h1 hidden>隐藏标题</h1><div class="job-description">' + ENCODED + '</div>'):
            with self.subTest(html=html), self.assertRaises(ParseError):
                parse_job_html(html)

    def test_hidden_body_fragments_are_excluded_without_destroying_literal_markup(self):
        hidden = '<span hidden>登录后查看</span><template>另一份岗位</template><span style="display:none">错误要求</span>'
        result = parse_job_html(markup(ENCODED + hidden))
        self.assertEqual(result["text"], BODY)

    def test_visible_and_aria_only_login_gates_still_block(self):
        for site, url, container in SITES:
            for gate in ('<div>登录后查看</div>', '<div aria-hidden="true">安全验证</div>'):
                with self.subTest(site=site, gate=gate), self.assertRaisesRegex(CrawlError, "manual_required"):
                    builtins().get(site).detail(PageSnapshot(url, markup(container=container) + gate))
            with self.subTest(site=site, rendered=True), self.assertRaisesRegex(CrawlError, "manual_required"):
                builtins().get(site).detail(PageSnapshot(url, markup(container=container), visible_text="安全验证"))

    def test_unrelated_rendered_text_does_not_certify_a_login_placeholder_body(self):
        for site, url, container in SITES:
            with self.subTest(site=site), self.assertRaises(CrawlError):
                builtins().get(site).detail(PageSnapshot(
                    url, markup(PREFIX + "登录后查看完整职位描述", container),
                    visible_text=TITLE))

    def test_head_json_ld_keeps_one_pass_html_decoding_and_identity_guard(self):
        url = "https://example.com/job/fixture"
        posting = {"@type": "JobPosting", "url": url, "title": TITLE, "description": ENCODED}
        html = '<head><script type="application/ld+json">' + json.dumps(posting) + '</script></head>'
        result = parse_job_html(html, source_url=url)
        self.assertEqual(result["text"], BODY)
        with self.assertRaises(ParseError):
            parse_job_html(html, source_url="https://example.com/job/another")

    def test_inline_cascade_preserves_visible_dom_and_active_gates(self):
        visible = (
            "display:none; display:block",
            "display:none !important; display:block !important",
            "display:block !important; display:none",
            "visibility:hidden; visibility:visible",
            "visibility:visible !important; visibility:collapse",
            "display:none; all:initial",
        )
        for style in visible:
            with self.subTest(style=style):
                self.assertEqual(parse_job_html(f'<section style="{style}">{markup()}</section>')["text"], BODY)
                with self.assertRaises(ParseError):
                    parse_job_html(markup() + f'<div style="{style}">登录后查看</div>')
        for style in ("display:block;display:none", "display:none !important;display:block",
                      "visibility:visible;visibility:collapse", "all:initial;display:none"):
            with self.subTest(style=style), self.assertRaises(ParseError):
                parse_job_html(f'<section style="{style}">{markup()}</section>')

    def test_css_strings_comments_and_functions_are_not_declarations(self):
        for style in ("--example: ';display:none;'; color:blue",
                      "/*;display:none;*/color:blue",
                      "--example:fn(;display:none;); color:blue"):
            with self.subTest(style=style):
                self.assertEqual(parse_job_html(f'<section style="{style}">{markup()}</section>')["text"], BODY)

    def test_optional_head_end_tag_does_not_hide_body_content(self):
        head = '<html><head><title>Job board</title><meta charset="utf-8">'
        for start, end in (("<body>", "</body>"), ("", ""), ("职位页面", "")):
            with self.subTest(start=start):
                self.assertEqual(parse_job_html(head + start + markup() + end + "</html>")["text"], BODY)

    def test_inert_template_structured_postings_are_not_candidates(self):
        stale = {"@type": "JobPosting", "title": "隐藏旧岗位", "description": "另一份隐藏旧岗位要求" * 8}
        correct = {"@type": "JobPosting", "title": TITLE, "description": ENCODED}
        script = lambda value: '<script type="application/ld+json">' + json.dumps(value) + '</script>'
        for tag in ("template", "noscript"):
            hidden = f'<{tag}><div>登录后查看</div>{script(stale)}</{tag}>'
            for active in (markup(), "<head>" + script(correct) + "</head>"):
                with self.subTest(tag=tag, structured=active.startswith("<head>")):
                    result = parse_job_html(active + hidden)
                    self.assertEqual((result["title"], result["text"]), (TITLE, BODY))
            with self.subTest(tag=tag, hidden_only=True), self.assertRaises(ParseError):
                parse_job_html(hidden)

    def test_effective_visibility_keeps_existing_expansion_checks(self):
        for site, url, container in SITES:
            with self.subTest(site=site):
                html = markup(ENCODED + '<button style="display:none;display:block">展开全部</button>', container)
                with self.assertRaisesRegex(CrawlError, "jd_incomplete"):
                    builtins().get(site).detail(PageSnapshot(url, html))
                html = markup(ENCODED + '<button style="display:block;display:none">展开全部</button>', container)
                self.assertEqual(builtins().get(site).detail(PageSnapshot(url, html))["text"], BODY)

    def test_liepin_uses_the_same_inline_and_inert_template_rules(self):
        html = f'<h1>{TITLE}</h1><dl><dt>职位介绍</dt><dd style="display:none;display:block">{ENCODED}</dd></dl>'
        stale = {"@type": "JobPosting", "title": "隐藏旧岗位", "description": "另一份隐藏旧岗位要求" * 8}
        hidden = '<template><script type="application/ld+json">' + json.dumps(stale) + '</script></template>'
        adapter = builtins().get("liepin")
        for tail in ("", hidden):
            with self.subTest(template=bool(tail)):
                result = adapter.detail(PageSnapshot("https://www.liepin.com/job/123456.shtml", html + tail))
                self.assertEqual(result["text"], BODY)


    def test_invalid_inline_values_do_not_override_valid_hiding(self):
        for style in (
            "display:none;display:bogus",
            "display:none;display:bogus !important",
            "display:none;display:inline block",
            "display:none;display:block flex grid",
            "display:none;display:list-item flex",
            "visibility:hidden;visibility:bogus",
            "visibility:hidden;visibility:bogus !important",
            "visibility:hidden;visibility:visible hidden",
        ):
            with self.subTest(style=style):
                hidden = f'<div style="{style}">登录后查看{markup()}</div>'
                self.assertEqual(parse_job_html(hidden + markup())["text"], BODY)
        for value in ("inline flex", "flow-root block", "inline flow-root list-item",
                      "table-cell", "contents", "initial"):
            with self.subTest(valid_display=value):
                html = f'<section style="display:none;display:{value}">{markup()}</section>'
                self.assertEqual(parse_job_html(html)["text"], BODY)

    def test_visible_descendants_override_inherited_visibility(self):
        for ancestor in ("visibility:hidden", "visibility:collapse", "visibility:hidden!important"):
            for restored in ("visibility:visible", "visibility:initial", "all:initial"):
                with self.subTest(ancestor=ancestor, restored=restored):
                    # Hidden text stays hidden, even in a node selected directly
                    # as a title/body; the child restores only its own subtree.
                    html = (f'<section style="{ancestor}">登录后查看'
                            '<span style="visibility:inherit">安全验证</span>'
                            '<span style="visibility:unset">安全验证</span>'
                            f'<article style="{restored}">{markup()}</article></section>')
                    self.assertEqual(parse_job_html(html)["text"], BODY)
                    inner = (f'<span style="{ancestor}">隐藏正文'
                             f'<span style="{restored}">{ENCODED}</span></span>')
                    self.assertEqual(parse_job_html(markup(inner))["text"], BODY)
                    container = (f'<h1>{TITLE}</h1>'
                                 f'<div class="job-description" style="{ancestor}">隐藏正文'
                                 f'<span style="{restored}">{ENCODED}</span></div>')
                    self.assertEqual(parse_job_html(container)["text"], BODY)

    def test_visibility_cannot_restore_display_none_hidden_or_inert_subtrees(self):
        for start, end in (('<div style="display:none">', '</div>'),
                           ('<div hidden>', '</div>'), ('<template>', '</template>')):
            html = start + '<section style="visibility:visible;display:block">' + markup() + '</section>' + end
            with self.subTest(start=start):
                with self.assertRaises(ParseError):
                    parse_job_html(html)
                self.assertEqual(parse_job_html(html + markup())["text"], BODY)

    def test_restored_controls_still_block_and_inherited_hidden_controls_do_not(self):
        for site, url, container in SITES:
            adapter = builtins().get(site)
            with self.subTest(site=site):
                gate = ('<section style="visibility:hidden">'
                        '<div style="visibility:visible">登录后查看</div></section>')
                with self.assertRaisesRegex(CrawlError, "manual_required"):
                    adapter.detail(PageSnapshot(url, markup(container=container) + gate))
                restored = ('<section style="visibility:hidden">'
                            '<button style="visibility:visible">展开全部</button></section>')
                with self.assertRaisesRegex(CrawlError, "jd_incomplete"):
                    adapter.detail(PageSnapshot(url, markup(ENCODED + restored, container)))
                hidden = '<section style="visibility:hidden"><button>展开全部</button></section>'
                self.assertEqual(adapter.detail(PageSnapshot(
                    url, markup(ENCODED + hidden, container)))["text"], BODY)

    def test_liepin_restored_descendants_retain_semantic_body(self):
        adapter = builtins().get("liepin")
        html = (f'<h1>{TITLE}</h1><dl><dt>职位介绍</dt>'
                '<dd style="visibility:hidden">隐藏正文'
                f'<span style="visibility:visible">{ENCODED}</span></dd></dl>')
        self.assertEqual(adapter.detail(PageSnapshot(
            "https://www.liepin.com/job/123456.shtml", html))["text"], BODY)


if __name__ == "__main__":
    unittest.main()
