"""BOSS DOM adapter with the known keyword search boundary.

Dynamic extraction and live certification still require publisher observations.
"""
from .adapters import DOMAdapter
from .keyword_scope import KeywordSearchScope


class BossAdapter(KeywordSearchScope, DOMAdapter):
    pass
