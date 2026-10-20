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
