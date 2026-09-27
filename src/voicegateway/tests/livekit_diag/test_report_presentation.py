"""Presentation rules that are logic, not layout.

The page wording and order are pinned by the snapshots in test_load_report.py.
What stays here is what a snapshot would hide the reason for: the duration unit
boundary (62 s and 66 s both used to read "1.1 min"), an absent value never
rendering as 0, an empty refusal reason still saying something, and fleet-wide
gates naming their scope instead of printing a blank subject cell.
"""

from __future__ import annotations

import pytest

from voicegateway.livekit_diag import gates, run_report
from voicegateway.livekit_diag.run_report import _duration_cell


@pytest.mark.parametrize(
    ("ms", "cell"),
    [
        # Seconds below ten minutes, so 62 s and 66 s are distinguishable.
        (62_005, "62.0&nbsp;s"),
        (66_000, "66.0&nbsp;s"),
        (599_000, "599.0&nbsp;s"),
        # Minutes from the boundary up: an hour in seconds is not readable.
        (600_001, "10.0&nbsp;min"),
        (3_600_000, "60.0&nbsp;min"),
        # Absent is words, never 0.
        (None, '<span class="nm">not measured</span>'),
    ],
)
def test_duration_cell(ms, cell) -> None:
    assert _duration_cell(ms) == cell


def test_a_refusal_with_no_reason_still_says_something() -> None:
    """An empty reason is a gap in whoever built the payload, and says so."""
    html = run_report.render_load_html(
        run_report.build_load_payload(
            run={"id": "r", "artifact_sha256": "a" * 64},
            tests=[],
            capacity={"calls_per_node": None, "reason": ""},
        )
    )
    assert "no reason was recorded" in html


@pytest.mark.parametrize("gate_fn", [gates.node_cpu_gates, gates.node_memory_gates])
def test_a_fleet_wide_gate_names_its_scope(gate_fn) -> None:
    """No node reported, so the finding is fleet-scoped rather than nameless."""
    [gate] = gate_fn([])
    assert gate.subject == gates.FLEET_SUBJECT
    assert gate.status == gates.UNKNOWN


def test_no_gate_row_leaves_the_subject_blank() -> None:
    results = [
        *gates.node_cpu_gates([]),
        *gates.node_memory_gates([]),
        gates.establishment_gate(
            attempted=1,
            succeeded=1,
            threshold=gates.MIN_ESTABLISHMENT_RATIO,
            subject="ramp-20",
        ),
    ]
    for gate in results:
        assert gate.subject, gate


def test_naming_the_fleet_does_not_claim_a_node_reported() -> None:
    """The subject is scope, not evidence. The detail still says nothing came."""
    [gate] = gates.node_cpu_gates([])
    assert "no node was sampled" in gate.detail
    assert gate.value is None


def test_a_measured_node_still_names_itself() -> None:
    """Non-vacuous: the fleet subject is only for the nothing-reported case."""
    [gate] = gates.node_cpu_gates(
        [
            gates.NodeUtilisationReading(
                node="sfu-1", source="node-exporter", utilisation=0.5, samples=9
            )
        ]
    )
    assert gate.subject == "sfu-1/node-exporter"
