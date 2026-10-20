"""Table visibility regressions using synthetic postings only."""
import unittest
from vibe_job_radar.html_parser import Document, ParseError, parse_job_html

BODY = "岗位职责：负责系统架构设计、研发与自动化测试。任职要求：熟悉系统设计和数据库。"


def posting(title="Engineer"):
    return f'<h1>{title}</h1><div class="job-description">{BODY}</div>'


class TableVisibilityTests(unittest.TestCase):
    def test_collapsed_columns_and_groups_remove_corresponding_cells(self):
        columns = (
            '<col style="visibility:collapse"><col>',
            '<colgroup style="visibility:collapse"><col style="visibility:visible"></colgroup><col>',
            '<colgroup span="1" style="visibility:collapse"></colgroup><col>',
        )
        for column in columns:
            with self.subTest(column=column):
                html = '<table>' + column + '<tr><td style="visibility:visible">' + posting("Stale")
                html += '</td><td>' + posting() + '</td></tr></table>'
                self.assertEqual(parse_job_html(html)["title"], "Engineer")
                self.assertNotIn("Stale", Document(html).root.text(visible_only=True))
        with self.assertRaises(ParseError):
            parse_job_html('<table><col style="visibility:collapse"><tr><td>' + posting() + '</table>')

    def test_column_spans_rowspans_and_nested_tables_keep_visible_cells(self):
        fixtures = (
            '<table><col span="2" style="visibility:collapse"><col><tr><td colspan="2">' +
            posting("Stale") + '</td><td>' + posting() + '</td></tr></table>',
            '<table><col style="visibility:collapse"><col><tbody><tr><td rowspan="2">' +
            posting("Stale") + '</td><td>filler</td></tr><tr><td>' + posting() + '</td></tr></tbody></table>',
            '<table><col><col style="visibility:collapse"><tbody><tr><td rowspan="0">filler</td>' +
            '<td>stale</td></tr><tr><td>' + posting("Stale") + '</td></tr></tbody>' +
            '<tbody><tr><td>' + posting() + '</td></tr></tbody></table>',
            '<table><col><col style="visibility:collapse"><tr><td><table><tr><td>' +
            posting() + '</td></tr></table></td><td>' + posting("Stale") + '</td></tr></table>',
            '<table><col style="visibility:collapse"><col><tr><td colspan="2">' +
            posting() + '</td></tr></table>',
        )
        for html in fixtures:
            with self.subTest(html=html):
                self.assertEqual(parse_job_html(html)["title"], "Engineer")

    def test_fostered_posting_uses_real_parent_visibility(self):
        for prefix in ("", "<tbody>", "<tbody><tr>", "<colgroup>"):
            with self.subTest(prefix=prefix):
                html = '<section><table hidden>' + prefix + posting() + '</table></section>'
                self.assertEqual(parse_job_html(html)["text"], BODY)
        html = '<section hidden><table>' + posting() + '</table></section>'
        with self.assertRaises(ParseError):
            parse_job_html(html)
        html = '<template><table hidden>' + posting("Stale") + '</table></template>' + posting()
        self.assertEqual(parse_job_html(html)["title"], "Engineer")

    def test_fostered_text_and_controls_are_visible_but_valid_cells_stay_hidden(self):
        for content in ("登录后查看", "<div>登录后查看</div>", "<button>登录后查看</button>"):
            with self.subTest(content=content):
                with self.assertRaises(ParseError):
                    parse_job_html(posting() + '<table hidden>' + content + '</table>')
        self.assertEqual(parse_job_html(posting() +
            '<table hidden><tr><td>登录后查看</td></tr></table>')["title"], "Engineer")
        html = '<table hidden><div class="job-description"><span>' + BODY + '</span></div>' + \
               '<h1>Engineer</h1></table>'
        self.assertEqual(parse_job_html(html)["text"], BODY)

    def test_implicit_rows_cover_direct_cells_and_row_groups(self):
        for opening in ("", "<tbody>", "<thead>", "<tfoot>", "<colgroup></colgroup>"):
            for cell in ("td", "th"):
                with self.subTest(opening=opening, cell=cell):
                    html = '<table><col style="visibility:collapse"><col>' + opening
                    html += f'<{cell}>' + posting("Stale") + f'</{cell}><{cell}>' + posting() + '</table>'
                    self.assertEqual(parse_job_html(html)["title"], "Engineer")

    def test_explicit_end_tags_stay_in_template_and_table_scope(self):
        for inert in ("template", "noscript"):
            with self.subTest(inert=inert):
                html = f'<div hidden><{inert}></div></{inert}>' + posting() + '</div>'
                with self.assertRaises(ParseError):
                    parse_job_html(html)
        for start, end in (("<table><tr><td>", "</td></tr></table>"),
                           ("<ol><li>", "</li></ol>")):
            outer = "li" if "<ol>" in start else "div"
            html = f'<{outer} hidden>' + start + f'</{outer}>' + end + posting() + f'</{outer}>'
            with self.subTest(start=start):
                with self.assertRaises(ParseError):
                    parse_job_html(html)
        html = '<div hidden><template><div></div></template></div>' + posting()
        self.assertEqual(parse_job_html(html)["title"], "Engineer")

        # A heading would validly close p; inspect inline content before that.
        paragraph = Document('<p hidden><button></p></button><span>probe</span></p>')
        self.assertNotIn("probe", paragraph.root.text(visible_only=True))
        for inert in ("template", "noscript"):
            html = f'<{inert}><table><tr><td>inert</{inert}>' + posting()
            self.assertEqual(parse_job_html(html)["title"], "Engineer")
        for tag in ("body", "html"):
            with self.assertRaises(ParseError):
                parse_job_html(f'<{tag} hidden></{tag}>' + posting())


    def test_generic_end_tag_cannot_cross_special_elements(self):
        for tag in ("div", "section", "article", "main", "blockquote", "fieldset",
                    "details", "address", "pre", "dl", "figure", "form"):
            with self.subTest(tag=tag):
                html = f'<span hidden><{tag}></span></{tag}>' + posting() + '</span>'
                with self.assertRaises(ParseError):
                    parse_job_html(html)
        # Block end tags use their own scope rules, not the generic algorithm.
        html = '<div hidden><section></div>' + posting()
        self.assertEqual(parse_job_html(html)["title"], "Engineer")


    def test_nonvoid_self_closing_keeps_hidden_scope(self):
        for tag in ("div", "section", "article", "span", "template", "noscript",
                    "custom-card"):
            with self.subTest(tag=tag):
                stale = f'<{tag} hidden/>' + posting("Stale") + f'</{tag}>'
                self.assertEqual(parse_job_html(stale + posting())["title"], "Engineer")
                with self.assertRaises(ParseError):
                    parse_job_html(stale)
        for tag in ("script", "style"):
            with self.subTest(rawtext=tag):
                html = (f'<div hidden><{tag}/>"</div>"</{tag}>' + posting("Stale")
                        + '</div>' + posting())
                self.assertEqual(parse_job_html(html)["title"], "Engineer")
        with self.assertRaisesRegex(ParseError, "nesting too deep"):
            Document("<div/>" * 202)

    def test_void_and_foreign_self_closing_remain_empty(self):
        for tag in ("br", "hr", "img", "input", "wbr", "meta", "link", "svg", "math"):
            with self.subTest(tag=tag):
                self.assertEqual(parse_job_html(f'<{tag} hidden/>' + posting())["title"],
                                 "Engineer")
        for outer, child, content in (("svg", "g", "text"), ("math", "mrow", "mtext")):
            with self.subTest(outer=outer):
                doc = Document(f'<{outer}><{child} hidden/><{content}>visible</{content}></{outer}>')
                self.assertIn("visible", doc.root.text(visible_only=True))
        for outer, integration in (
                ("svg", "foreignObject"), ("svg", "desc"), ("svg", "title"),
                ("math", 'annotation-xml encoding="text/html"'), ("math", "mtext")):
            with self.subTest(integration=integration):
                end = integration.split()[0]
                html = (f'<{outer}><{integration}><div hidden/>' + posting("Stale")
                        + '</div>' + posting() + f'</{end}></{outer}>')
                if outer == "svg" and integration in {"desc", "title"}:
                    # SVG descriptive metadata is never visually rendered;
                    # HTML integration changes tokenization, not that rule.
                    with self.assertRaises(ParseError):
                        parse_job_html(html)
                    self.assertEqual(parse_job_html(html + posting("Outside"))["title"],
                                     "Outside")
                else:
                    self.assertEqual(parse_job_html(html)["title"], "Engineer")


    def test_table_form_does_not_remain_open(self):
        for group in ("", "<tbody>", "<tbody><tr>"):
            with self.subTest(group=group):
                html = "<table>" + group + '<form hidden>' + posting()
                self.assertEqual(parse_job_html(html)["title"], "Engineer")
                doc = Document("<table>" + group + '<form hidden><br>')
                form = next(n for n in doc.root.walk() if n.tag == "form")
                self.assertEqual(form.children, [])
        with self.assertRaises(ParseError):
            parse_job_html("<form hidden>" + posting() + "</form>")

    def test_nested_button_closes_only_an_in_scope_button(self):
        for middle in ("", "<span>", "<div>"):
            with self.subTest(middle=middle), self.assertRaises(ParseError):
                parse_job_html(posting() + "<button hidden>old" + middle
                               + "<button>登录后查看</button>")
        html = (posting() + "<button hidden><table><tr><td>"
                "<button>登录后查看</button></td></tr></table></button>")
        self.assertEqual(parse_job_html(html)["title"], "Engineer")

    def test_heading_tokens_share_their_scope_rules(self):
        for opening, ending in (("h1", "h2"), ("h3", "h6"), ("h6", "h1")):
            with self.subTest(ending=ending), self.assertRaises(ParseError):
                parse_job_html(posting() + f"<{opening} hidden>old</{ending}>登录后查看")
        for paragraph in ("", "<p>"):
            with self.subTest(paragraph=paragraph), self.assertRaises(ParseError):
                parse_job_html(posting() + "<h2 hidden>old" + paragraph
                               + "<h3>登录后查看</h3>")
        for hidden in ("<div hidden></h2>", "<h2 hidden><span><h3>"):
            with self.subTest(hidden=hidden):
                self.assertEqual(parse_job_html(posting() + hidden + "登录后查看")["title"],
                                 "Engineer")

    def test_foster_parent_work_does_not_rescan_growing_siblings(self):
        class CountedChildren(list):
            visits = 0
            def __iter__(self):
                for item in super().__iter__():
                    self.visits += 1
                    yield item
        doc = Document("")
        doc.root.children = CountedChildren()
        count = 1000
        doc.feed("<table>" + "<br>" * count + "</table>")
        doc.close()
        self.assertLessEqual(doc.root.children.visits, count * 4)
        self.assertEqual([n.tag for n in doc.root.children], ["br"] * count + ["table"])

        chunked = Document("<table>")
        chunked.feed("before<br></table>")
        chunked.close()
        self.assertEqual([n.tag if not isinstance(n, str) else "#text"
                          for n in chunked.root.children], ["#text", "br", "table"])


    def test_nested_table_closes_only_the_table_mode_scope(self):
        for group in ("", "<tbody>", "<tbody><tr>", "<colgroup>"):
            with self.subTest(group=group):
                html = "<table hidden>" + group + "<table>" + posting() + "</table>"
                self.assertEqual(parse_job_html(html)["title"], "Engineer")
        with self.assertRaises(ParseError):
            parse_job_html("<table hidden><tr><td><table>" + posting() + "</table></td></tr></table>")

    def test_form_pointer_ignores_nested_starts_and_removes_only_the_form(self):
        for before in ("<form hidden><form></form>",
                       "<table><form hidden></table><form hidden></form>",
                       "<form hidden><template><form></form></template></form>"):
            with self.subTest(before=before):
                self.assertEqual(parse_job_html(before + posting())["title"], "Engineer")
        with self.assertRaises(ParseError):
            parse_job_html("<form hidden><div></form>" + posting() + "</div>")
        with self.assertRaises(ParseError):
            parse_job_html("<table><form></form></table><form hidden>" + posting())

    def test_long_rowspans_have_bounded_cursor_work(self):
        from unittest.mock import patch
        import vibe_job_radar.html_parser as parser
        class Position(int):
            comparisons = 0
            def __add__(self, other):
                return Position(int(self) + int(other))
            __radd__ = __add__
            def __lt__(self, other):
                Position.comparisons += 1
                return int(self) < int(other)
            def __le__(self, other):
                Position.comparisons += 1
                return int(self) <= int(other)
            def __gt__(self, other):
                Position.comparisons += 1
                return int(self) > int(other)
            def __ge__(self, other):
                Position.comparisons += 1
                return int(self) >= int(other)
        original = Document._span
        def span(node, name, maximum=1000):
            return Position(original(node, name, maximum))
        html = ('<table><col style="visibility:collapse"><tbody>'
                + "".join(f'<tr><td rowspan="0">cell-{i}|</td></tr>' for i in range(100))
                + "</tbody></table>")
        with patch.object(Document, "_span", staticmethod(span)):
            doc = Document(html)
        self.assertNotIn("cell-0|", doc.root.text(visible_only=True))
        self.assertIn("cell-99|", doc.root.text(visible_only=True))
        self.assertLess(Position.comparisons, 10000)
        with patch.object(parser, "_TABLE_LAYOUT_WORK_LIMIT", 50, create=True):
            with self.assertRaisesRegex(ParseError, "table layout too complex"):
                Document(html)
