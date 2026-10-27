"""51job DOM adapter with the known keyword search boundary.

This does not resolve robots HTML or authorize additional routes and resources.
"""
from ..utils import canonical_url
from .adapters import DOMAdapter
from .keyword_scope import KeywordSearchScope


class Job51Adapter(KeywordSearchScope, DOMAdapter):
    def job_identity(self, url: str) -> str:
        # Reuse existing persisted-job identity, preserving unknown query fields.
        return self.key + ':' + canonical_url(self.accept_url(url, detail=True))
