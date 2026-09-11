"""One provider, one stored id, whatever derived it (#279).

The Call detail modal rendered six chips for three providers because the two
derivation paths disagreed about spelling and nothing normalized before the
write. These tests pin the agreement rather than the implementation: each case
is a pair of spellings that must collapse to the same id.
"""

from __future__ import annotations

import pytest

from voicegateway.core.provider_names import canonical_provider


@pytest.mark.parametrize(
    ("module_derived", "name_derived"),
    [
        ("cartesia", "Cartesia"),
        ("cartesia", "CartesiaTTSService"),
        ("cartesia", "CartesiaTTSService#0"),
        ("deepgram", "Deepgram"),
        ("deepgram", "DeepgramSTTService#1"),
        ("openai", "OpenAILLMService"),
        ("google", "Gemini"),
        ("google", "GoogleLLMService"),
    ],
)
def test_both_derivations_collapse_to_one_id(module_derived, name_derived):
    """The module segment and the class-name fallback must agree."""
    assert canonical_provider(module_derived) == canonical_provider(name_derived)


def test_the_agreed_id_is_the_lowercase_module_segment():
    """Agreement is not enough: they must agree on the storable form."""
    assert canonical_provider("CartesiaTTSService#0") == "cartesia"
    assert canonical_provider("DeepgramSTTService") == "deepgram"
    assert canonical_provider("Gemini") == "google"


@pytest.mark.parametrize("raw", ["", "   ", None, "#0", "___"])
def test_unusable_input_is_empty_not_a_provider(raw):
    """EOU rows write an empty provider; it must never become a chip."""
    assert canonical_provider(raw) == ""


def test_a_bare_suffix_is_not_stripped_to_nothing():
    """``Service`` alone is a real (if odd) name, not an empty provider."""
    assert canonical_provider("Service") == "service"


def test_an_unknown_provider_passes_through_lowercased():
    """No allowlist: a provider VG has never seen still records."""
    assert canonical_provider("SomeNewVendor") == "somenewvendor"


def test_already_canonical_input_is_unchanged():
    """Idempotent, so re-normalizing a stored value is safe."""
    for value in ("cartesia", "deepgram", "google", "openai"):
        assert canonical_provider(value) == value
        assert canonical_provider(canonical_provider(value)) == value
