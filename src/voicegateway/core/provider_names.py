"""Canonical provider ids. A leaf module: it imports nothing.

``inference.session.capture`` and ``inference.pipecat.observer`` both derive a
provider name from a live plugin instance, by different means, and both write
it to the same ``requests.provider`` column. Before this module they disagreed:
the module-segment path produced ``cartesia`` while the name-attribute fallback
produced ``CartesiaTTSService``, so one provider was stored under two spellings
and the Call detail modal rendered a chip for each (#279).

The rule lives here, in one place, rather than in each caller. Two derivations
that must agree cannot be kept in agreement by convention; the accounting
review made the same point about a rule enforced at one door and walked around
at another.

This module must never import from ``inference``, ``repository``, ``server`` or
``schemas``: ``capture`` is on the hot metering path and pulls in nothing else.
That is also why the alias table is a literal dict and not a registry lookup.
"""

from __future__ import annotations

#: Framework class-name suffixes stripped before comparison. Pipecat services
#: are named ``CartesiaTTSService``/``OpenAILLMService``; the brand is the
#: prefix. Ordered longest-first so ``ttsservice`` wins over ``service``.
_SERVICE_SUFFIXES: tuple[str, ...] = (
    "ttsservice",
    "sttservice",
    "llmservice",
    "service",
)

#: Branded or framework-specific spellings mapped to the id VG stores. Keep
#: this small and evidence-driven: add an entry when a spelling is actually
#: observed in the wild, not because it seems plausible. A wrong alias merges
#: two real providers into one and is much harder to notice than a duplicate.
_ALIASES: dict[str, str] = {
    # LiveKit's module segment is ``google``; the model family is branded
    # Gemini and some paths report that instead. Observed in #279.
    "gemini": "google",
    "googlegenai": "google",
    "google_genai": "google",
}


def canonical_provider(raw: str | None) -> str:
    """Return the canonical, storable provider id for a raw derivation.

    Lowercases, strips a pipecat service-class suffix, and applies the alias
    table. An empty or unusable input returns ``""``, which callers treat as
    "no provider" rather than as a provider named empty string.

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
    name = raw.strip()
    if not name:
        return ""
    # Defensive: pipecat processor names carry a ``#N`` instance suffix. The
    # caller strips it today, but this function is the last thing between a
    # derivation and the database.
    name = name.split("#", 1)[0].strip()
    lowered = name.lower()
    for suffix in _SERVICE_SUFFIXES:
        if lowered.endswith(suffix) and len(lowered) > len(suffix):
            lowered = lowered[: -len(suffix)]
            break
    lowered = lowered.strip("_-. ")
    if not lowered:
        return ""
    return _ALIASES.get(lowered, lowered)


__all__ = ["canonical_provider"]
