"""BOSS DOM adapter with the known keyword search boundary.

Dynamic extraction and live certification still require publisher observations.
"""
from .adapters import DOMAdapter
from .keyword_scope import KeywordSearchScope


class BossAdapter(KeywordSearchScope, DOMAdapter):
    def _keyword_search_paths(self):
        # The official homepage links this national list with an explicit query.
        # No other city path or replacement default route is inferred here.
        return (*super()._keyword_search_paths(), '/c100010000')
