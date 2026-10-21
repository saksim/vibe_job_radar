"""Artificial DOM snapshots: no site, browser, login or credentials needed."""
from __future__ import annotations

import unittest

from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot


class DOMSearchConsistencyTests(unittest.TestCase):
    def cases(self):
        for site, path in (('boss', '/job_detail/{}.html'),
                           ('51job', '/shanghai/{}.html'),
                           ('liepin', '/job/{}.shtml')):
            yield builtins().get(site), path

    def test_inert_cards_do_not_win_deduplication_or_consume_result_limit(self):
        for adapter, path in self.cases():
            with self.subTest(site=adapter.key):
                hidden = ''.join('<a href="' + path.format(i) + '">stale</a>'
                                 for i in range(301))
                markup = ('<template>' + hidden + '</template>'
                          '<section hidden>' + hidden + '</section>'
                          '<a href="' + path.format(0) + '">current title</a>'
                          '<a href="' + path.format(999) + '">last visible job</a>')
                cards = adapter.cards(PageSnapshot(adapter.search_url('test'), markup))
                self.assertEqual([c.title for c in cards], ['current title', 'last visible job'])

    def test_rendered_style_copy_matches_browser_observation(self):
        for adapter, path in self.cases():
            with self.subTest(site=adapter.key):
                raw = ('<a class="stale" href="' + path.format(1) + '">old title</a>'
                       '<a href="' + path.format(1) + '">visible title</a>')
                rendered = raw.replace('class="stale"', 'style="display:none!important"')
                page = PageSnapshot(adapter.search_url('test'), raw,
                                    visible_text='visible title', rendered_html=rendered)
                self.assertEqual([c.title for c in adapter.cards(page)], ['visible title'])
                self.assertEqual(page.html, raw)

    def test_visible_titles_exclude_hidden_descendants_and_keep_existing_title_attribute(self):
        for adapter, path in self.cases():
            with self.subTest(site=adapter.key):
                markup = ('<a href="' + path.format(1) + '">visible<span hidden>stale</span></a>'
                          '<a style="visibility:hidden" href="' + path.format(2) + '">'
                          'hidden<span style="visibility:visible">restored</span></a>'
                          '<a title="full title" href="' + path.format(3) + '">short title</a>'
                          '<a title="hidden title" style="visibility:hidden" href="'
                          + path.format(4) + '">hidden</a>'
                          '<a title="icon title" style="visibility:hidden" href="'
                          + path.format(5) + '"><img style="visibility:visible" alt="job icon"></a>')
                cards = adapter.cards(PageSnapshot(adapter.search_url('test'), markup))
                self.assertEqual([c.title for c in cards], ['visible', 'restored', 'full title', 'icon title'])

    def test_unpaired_rendered_observation_is_rejected(self):
        for adapter, path in self.cases():
            with self.subTest(site=adapter.key):
                with self.assertRaises(CrawlError) as caught:
                    adapter.cards(PageSnapshot(adapter.search_url('test'),
                        '<a href="' + path.format(1) + '">old title</a>', rendered_html=''))
                self.assertEqual(caught.exception.code, 'invalid_page_observation')

    def test_malformed_href_does_not_discard_other_search_results(self):
        for adapter, path in self.cases():
            with self.subTest(site=adapter.key):
                markup = ('<a href="' + path.format(1) + '">first</a>'
                          '<a href="https://[broken/path">broken address</a>'
                          '<a href="' + path.format(2) + '">second</a>')
                cards = adapter.cards(PageSnapshot(adapter.search_url('test'), markup))
                self.assertEqual([c.title for c in cards], ['first', 'second'])

    def test_malformed_accepted_url_has_existing_error_contract(self):
        for adapter, _ in self.cases():
            with self.subTest(site=adapter.key):
                with self.assertRaises(CrawlError) as caught:
                    adapter.accept_url('https://[broken/path', detail=True)
                self.assertEqual(caught.exception.code, 'invalid_url')


if __name__ == '__main__':
    unittest.main()
