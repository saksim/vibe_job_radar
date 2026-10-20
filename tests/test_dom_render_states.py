import unittest
from vibe_job_radar.html_parser import Document, ParseError, parse_job_html

BODY = "岗位职责：负责系统架构设计、研发与自动化测试。任职要求：熟悉系统设计和数据库。"


def posting(title="Engineer"):
    return f'<h1>{title}</h1><div class="job-description">{BODY}</div>'


class RenderStateTests(unittest.TestCase):
    def test_svg_nonrendered_definitions_are_not_job_containers(self):
        for tag in ("metadata", "defs", "symbol", "clipPath", "marker", "mask",
                    "pattern", "linearGradient", "radialGradient", "desc", "title"):
            with self.subTest(tag=tag):
                stale = (f'<svg><{tag} class="job-description" style="display:block!important">'
                         + BODY + f'</{tag}></svg>')
                with self.assertRaises(ParseError):
                    parse_job_html("<h1>Engineer</h1>" + stale)
                self.assertEqual(parse_job_html(stale + posting())["title"], "Engineer")
        html = "<svg><foreignObject>" + posting() + "</foreignObject></svg>"
        self.assertEqual(parse_job_html(html)["title"], "Engineer")

    def test_second_open_anchor_does_not_inherit_the_first_hidden_state(self):
        for middle in ("", "<span>", "<div>"):
            with self.subTest(middle=middle), self.assertRaises(ParseError):
                parse_job_html(posting() + "<a hidden>old" + middle + "<a>登录后查看</a>")
        html = posting() + "<a hidden><table><tr><td><a>登录后查看</a></td></tr></table></a>"
        self.assertEqual(parse_job_html(html)["title"], "Engineer")

    def test_closed_details_expose_only_the_first_direct_summary(self):
        for content in (posting("Stale"), "登录后查看", "<div>登录后查看</div>",
                        "<summary>More</summary><summary>登录后查看</summary>"):
            with self.subTest(content=content):
                html = posting() + "<details>" + content + "</details>"
                self.assertEqual(parse_job_html(html)["title"], "Engineer")
        for attrs, content in (("open", "登录后查看"), ("", "<summary>登录后查看</summary>"),
                               ('open="false"', "登录后查看")):
            with self.subTest(attrs=attrs, content=content), self.assertRaises(ParseError):
                parse_job_html(posting() + f"<details {attrs}>" + content + "</details>")
        html = posting() + "<details><div><summary>登录后查看</summary></div><summary>More</summary></details>"
        self.assertEqual(parse_job_html(html)["title"], "Engineer")

    def test_html_image_alias_is_void_without_changing_svg_image(self):
        for tag in ("image", "basefont", "bgsound", "keygen", "frame"):
            for suffix in (">", "/>"):
                with self.subTest(tag=tag, suffix=suffix):
                    self.assertEqual(parse_job_html("<" + tag + " hidden" + suffix
                                     + posting())["title"], "Engineer")
        doc = Document("<svg><image hidden><title>Hidden description</title></image>"
                       "<text>Visible</text></svg>")
        self.assertIn("Visible", doc.root.text(visible_only=True))
        self.assertNotIn("Hidden description", doc.root.text(visible_only=True))

    def test_inline_content_visibility_follows_the_supported_cascade(self):
        hidden = ("content-visibility:hidden",
                  "content-visibility:hidden!important;content-visibility:visible",
                  "content-visibility:hidden;content-visibility:invalid",
                  "all:initial;content-visibility:hidden")
        for style in hidden:
            with self.subTest(style=style):
                html = '<section style="' + style + '">' + posting("Stale") + "</section>"
                self.assertEqual(parse_job_html(html + posting())["title"], "Engineer")
                with self.assertRaises(ParseError):
                    parse_job_html(html)
        for style in ("content-visibility:visible", "content-visibility:auto",
                      "content-visibility:hidden;all:initial",
                      "content-visibility:hidden;content-visibility:visible!important"):
            with self.subTest(style=style):
                self.assertEqual(parse_job_html('<section style="' + style + '">'
                                 + posting() + "</section>")["title"], "Engineer")
        html = ('<section style="content-visibility:hidden"><div style="content-visibility:visible">'
                + posting("Stale") + "</div></section>" + posting())
        self.assertEqual(parse_job_html(html)["title"], "Engineer")


    def test_unopened_dialog_uses_the_display_cascade(self):
        for attrs in ("", 'style="display:revert"', 'style="display:garbage"'):
            for content in ("登录后查看", posting("Stale")):
                with self.subTest(attrs=attrs, content=content):
                    html = f"<dialog {attrs}>" + content + "</dialog>"
                    self.assertEqual(parse_job_html(html + posting())["title"], "Engineer")
                    with self.assertRaises(ParseError):
                        parse_job_html(html)
        for attrs in ("open", 'open="false"', 'style="display:block"',
                      'style="all:initial"', 'style="display:none;display:block!important"'):
            with self.subTest(attrs=attrs):
                self.assertEqual(parse_job_html(f"<dialog {attrs}>" + posting()
                                 + "</dialog>")["title"], "Engineer")
                with self.assertRaises(ParseError):
                    parse_job_html(posting() + f"<dialog {attrs}>登录后查看</dialog>")

    def test_visible_walk_does_not_rebuild_descendant_text(self):
        from unittest.mock import patch
        from vibe_job_radar.html_parser import Node
        class CountedChildren(list):
            visits = 0
            def __iter__(self):
                for item in super().__iter__():
                    CountedChildren.visits += 1
                    yield item
        depth = 150
        doc = Document('<section style="visibility:hidden">' * depth
                       + '<div class="job-description"><span style="visibility:visible">'
                       + BODY + '</span></div>' + '</section>' * depth)
        nodes = list(doc.root.walk())
        for node in nodes:
            node.children = CountedChildren(node.children)
        with patch.object(Node, "text", side_effect=AssertionError("walk rebuilt text")):
            visible = list(doc.root.walk(visible_only=True))
        self.assertEqual([n.tag for n in visible].count("section"), depth)
        self.assertIn("job-description", [n.attrs.get("class") for n in visible])
        self.assertLessEqual(CountedChildren.visits, len(nodes) * 3)
        self.assertEqual(doc.root.text(visible_only=True), BODY + "\n" * (depth + 1))
        empty = Document('<section style="visibility:hidden"><span style="visibility:visible"> '
                         '</span><nav style="visibility:visible">ignored</nav></section>')
        self.assertNotIn("section", [n.tag for n in empty.root.walk(visible_only=True)])
        empty.feed('<section style="visibility:hidden"><span style="visibility:visible">new</span></section>')
        self.assertEqual([n.tag for n in empty.root.walk(visible_only=True)].count("section"), 1)
