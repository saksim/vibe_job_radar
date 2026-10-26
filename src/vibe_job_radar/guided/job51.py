"""51job DOM adapter with the known keyword search boundary.

This does not resolve robots HTML or authorize additional routes and resources.
"""
from .adapters import DOMAdapter
from .keyword_scope import KeywordSearchScope


class Job51Adapter(KeywordSearchScope, DOMAdapter):
    pass
