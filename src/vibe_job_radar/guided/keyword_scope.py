"""Read-only protection of an existing, identifiable keyword search.

Other explicit manual-list routes retain their existing input contract. This
does not infer filter or pagination semantics or migrate saved task bindings.
"""
from urllib.parse import parse_qsl, urlsplit

from .contracts import CrawlError


class KeywordSearchScope:
    def validate_search_scope(self, expected_url, keyword, observed_url):
        expected = urlsplit(self.accept_url(expected_url))
        base = urlsplit(self.search_base)

        def route(value):
            return (value.scheme, value.hostname, value.port or 443,
                    value.path.rstrip('/'))

        def keywords(value):
            return [item for name, item in parse_qsl(value.query, keep_blank_values=True)
                    if name == self.keyword_param]

        expected_keywords = keywords(expected)
        # Explicit manual lists and a bare entry have no known URL keyword
        # contract. Do not replace their existing DOM path with a guessed one.
        if route(expected) != route(base) or not expected_keywords:
            return
        actual = urlsplit(self.accept_url(observed_url))
        if (expected_keywords != [keyword] or route(actual) != route(base)
                or keywords(actual) != [keyword]):
            raise CrawlError('search_scope_changed')
