"""Canonical provider ids, resolved against the price catalog.

``inference.session.capture`` and ``inference.pipecat.observer`` both derive a
provider name from a live plugin instance, by different means, and both write
it to the same ``requests.provider`` column. Before this module they disagreed:
the module-segment path produced ``cartesia`` while the name-attribute fallback
produced ``CartesiaTTSService``, so one provider was stored under two spellings
and the Call detail modal rendered a chip for each (#279).

**The catalog is the authority, not this module.** ``voice_prices`` already
lowercases, strips, and carries a per-provider matcher that resolves branded
and framework spellings: ``Cartesia``, ``CartesiaTTSService`` and ``gemini``
all resolve there, the last to ``google``. ``component_identity`` builds
``model_id`` as ``f"{provider}/{model}"``, so the provider half is the key the
catalog is later asked to price. Deriving it from a second, hand-maintained
alias table would mean two vocabularies that must agree and nothing making them
agree; the catalog would rename a provider and VG would keep the old id.

So this function asks the catalog first and only normalizes locally for
providers the catalog has never heard of. Those still record (VG meters what it
sees, priced or not), they just get a predictable id instead of a class name.

This module must never import from ``inference``, ``repository``, ``server`` or
``schemas``: ``capture`` is on the hot metering path. ``voice_prices`` is a base
dependency of that same path and its snapshot is cached, so the lookup costs
about 140ns.
"""

from __future__ import annotations

from voice_prices.data_snapshot import find_provider_by_id, get_snapshot

#: Framework class-name suffixes stripped on the fallback path only. The
#: catalog's matcher already handles these for providers it knows; this is what
#: keeps an unknown ``SarvamTTSService`` from recording as
#: ``sarvamttsservice``. Ordered longest-first so ``ttsservice`` wins over
#: ``service``.
_SERVICE_SUFFIXES: tuple[str, ...] = (
    "ttsservice",
    "sttservice",
    "llmservice",
    "service",
)


def _local_normalize(name: str) -> str:
    """Lowercase and strip a framework suffix. Used only off-catalog."""
    lowered = name.lower()
    for suffix in _SERVICE_SUFFIXES:
        if lowered.endswith(suffix) and len(lowered) > len(suffix):
            lowered = lowered[: -len(suffix)]
            break
    return lowered.strip("_-. ")


def canonical_provider(raw: str | None) -> str:
    """Return the canonical, storable provider id for a raw derivation.

    Resolves against the price catalog where it can, so VG stores the same id
    the catalog prices. Falls back to a lowercased, suffix-stripped form for a
    provider the catalog does not carry. An empty or unusable input returns
    ``""``, which callers treat as "no provider" rather than as a provider
    named empty string.

    >>> canonical_provider("Cartesia")
    'cartesia'
    >>> canonical_provider("CartesiaTTSService#0")
    'cartesia'
    >>> canonical_provider("Gemini")
    'google'
    >>> canonical_provider("  ")
    ''
    """
    if not raw:
        return ""
    # Pipecat processor names carry a ``#N`` instance suffix. The caller strips
    # it today, but this function is the last thing between a derivation and
    # the database.
    name = raw.strip().split("#", 1)[0].strip()
    if not name:
        return ""
    provider = find_provider_by_id(get_snapshot().providers, name)
    if provider is not None:
        return str(provider.id)
    return _local_normalize(name)


__all__ = ["canonical_provider"]
