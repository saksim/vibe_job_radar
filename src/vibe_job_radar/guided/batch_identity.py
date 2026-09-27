"""Versioned entity identity for new batches, without rewriting saved selections.

The observed URL remains the navigation target. An entity key does not grant
permission, construct a different URL, or merge different Liepin URL families.
"""
from __future__ import annotations

import hashlib
from dataclasses import replace

from .contracts import CrawlError

ENTITY_V1 = 'platform_entity_v1'
LEGACY = 'observed_url_v1'


def strategy(adapter):
    return ENTITY_V1 if callable(getattr(adapter, 'job_identity', None)) else LEGACY


def batch_cards(state, adapter, cards):
    mode = state.get('identity_strategy', LEGACY)
    if mode == LEGACY:
        return cards
    identity = getattr(adapter, 'job_identity', None)
    if mode != ENTITY_V1 or not callable(identity):
        raise CrawlError('batch_identity_unsupported')
    unique = {}
    for card in cards:
        entity = identity(card.url)
        ident = hashlib.sha256(entity.encode('utf-8')).hexdigest()[:24]
        unique.setdefault(ident, replace(card, id=ident))
    return list(unique.values())


def page_signature(state, cards):
    identifiers = [c.id for c in cards]
    if state.get('identity_strategy', LEGACY) == ENTITY_V1:
        identifiers.sort()
    return hashlib.sha256('\n'.join(identifiers).encode()).hexdigest()
