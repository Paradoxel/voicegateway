"""One scope exclusion, ``pps``, and it is permanent.

RTP ports and network (``network_in`` / ``network_out``, one credit bucket per
direction) used to be excluded because nothing measured them. Exporters now
publish their columns, so the exclusion machinery, which derives an exclusion
from nothing publishing the columns, lifted both by itself.

``pps`` is different: no per-instance-type packets-per-second allowance is
published by anyone, so there is no denominator at any price. It lives in
:data:`judge.PERMANENT_HEADROOM_EXCLUSIONS` rather than being derived.

**The compensating disclosure is still enforced, not trusted.** Removing rows
can move a verdict off UNKNOWN, so the tests assert both halves: what is no
longer excluded is now MEASURED, and what is still excluded is still disclosed
in the payload and in the HTML.
"""

from __future__ import annotations

import pytest

from voicegateway.livekit_diag import gates, run_report
from voicegateway.loadtest import judge
from voicegateway.middleware.node_samples_worker_middleware import (
    any_source_publishes,
)

HEALTHY = {"name": "ramp-20", "attempted_calls": 100, "succeeded_calls": 100}
NOT_IN_SCOPE = "Not in scope for this report"
FLEET_PPS = f"{gates.FLEET_SUBJECT}/{gates.HEADROOM_PPS}"
MEASURED_NOW = (
    gates.HEADROOM_RTP_PORTS,
    gates.HEADROOM_NETWORK_IN,
    gates.HEADROOM_NETWORK_OUT,
)


def _payload(artifact_sha256="a" * 64, exclusions=None, **kw):
    return run_report.build_load_payload(
        run={"id": "ramp-500", "artifact_sha256": artifact_sha256},
        tests=[],
        scope_exclusions=(
            judge.excluded_headroom_resources() if exclusions is None else exclusions
        ),
        **kw,
    )


def _passing(**kw):
    return gates.establishment_gate(
        attempted=1000,
        succeeded=1000,
        threshold=gates.MIN_ESTABLISHMENT_RATIO,
        **kw,
    ).as_dict()


# --------------------------------------------------------------------------
# Derived, not listed
# --------------------------------------------------------------------------


def test_pps_is_the_only_exclusion_and_the_rest_follow_from_the_columns() -> None:
    """Exactly one, permanent; everything else is excluded iff unpublished.

    With the exact set pinned to pps, the loop proves rtp ports, both network
    directions and file descriptors are excluded by nothing and measured.
    """
    excluded = judge.excluded_headroom_resources()
    assert sorted(excluded) == [gates.HEADROOM_PPS]
    assert sorted(judge.PERMANENT_HEADROOM_EXCLUSIONS) == [gates.HEADROOM_PPS]
    assert gates.HEADROOM_PPS not in judge.HEADROOM_REQUIREMENTS
    for resource in (*MEASURED_NOW, gates.HEADROOM_FILE_DESCRIPTORS):
        assert resource in judge.HEADROOM_REQUIREMENTS, resource
    for resource, columns in judge.HEADROOM_REQUIREMENTS.items():
        assert (resource in excluded) is not any_source_publishes(*columns), resource


def test_the_derivation_still_bites_when_a_column_is_unpublished(monkeypatch) -> None:
    """Non-vacuous: with every requirement published, point one at a column no
    exporter publishes and the exclusion has to come back, naming it."""
    monkeypatch.setitem(
        judge.HEADROOM_REQUIREMENTS,
        gates.HEADROOM_RTP_PORTS,
        ("a_column_no_exporter_publishes",),
    )
    excluded = judge.excluded_headroom_resources()
    assert sorted(excluded) == [gates.HEADROOM_PPS, gates.HEADROOM_RTP_PORTS]
    assert "a_column_no_exporter_publishes" in excluded[gates.HEADROOM_RTP_PORTS]


def test_the_pps_event_is_still_counted_as_a_gate() -> None:
    """The exclusion bounds what can be quantified, not what is detected."""
    reason = judge.excluded_headroom_resources()[gates.HEADROOM_PPS]
    assert gates.NETWORK_ALLOWANCE_GATE in reason
    assert gates.NETWORK_ALLOWANCE_GATE in gates.ALL_GATES
    # A count, deliberately NOT a ratio: 9613 must not render as "961300%".
    assert gates.NETWORK_ALLOWANCE_GATE not in gates.RATIO_GATES


# --------------------------------------------------------------------------
# pps is a fleet row; the measurable three are per-node rows
# --------------------------------------------------------------------------


def _aggregate_with_readings():
    """One node whose window carried media ports and throughput both ways."""
    from voicegateway.loadtest.aggregation import TestAggregate
    from voicegateway.repository.node_correlation_repository import window_of

    return TestAggregate(
        window=window_of(1_785_661_201_000, 1_785_661_260_000),
        peak_cpu_utilisation=0.4,
        peak_memory_utilisation=0.4,
        node_samples_in_window=2,
        rtp_port_readings=[
            gates.HeadroomReading(
                node="sip-1",
                source="node-exporter",
                resource=gates.HEADROOM_RTP_PORTS,
                used=1200.0,
                limit=10001.0,
            )
        ],
        bandwidth_peaks={("sip-1", "in"): 400_000.0, ("sip-1", "out"): 500_000.0},
    )


def test_the_measurable_resources_are_per_node_rows() -> None:
    """Nothing correlated fabricates no per-node row; readings give one per node."""
    subjects = [r.subject or "" for r in judge.judge_test(HEALTHY)]
    for resource in MEASURED_NOW:
        assert not [s for s in subjects if s.endswith(f"/{resource}")], resource
    assert [s for s in subjects if s.endswith("/file_descriptors")]

    measured = judge.judge_test(
        HEALTHY,
        aggregate=_aggregate_with_readings(),
        network_baselines={"sip-1": {"in_bps": 1_000_000.0, "out_bps": 1_000_000.0}},
    )
    by_subject = {r.subject: r for r in measured}
    for subject in (
        "sip-1/node-exporter/rtp_ports",
        "sip-1/network_in",
        "sip-1/network_out",
    ):
        assert by_subject[subject].status == gates.PASS, subject
    assert by_subject["sip-1/network_in"] != by_subject["sip-1/network_out"]


def test_one_fleet_row_per_run_and_the_verdict_stays_unknown() -> None:
    """A three-step ramp emits ONE fleet/pps row, and UNKNOWN outranks PASS.

    The measurable three still get a fleet UNKNOWN row because this run scraped
    nothing, but they are not exclusions.
    """
    run = [
        {"name": f"ramp-{n}", "attempted_calls": 100, "succeeded_calls": 100}
        for n in (5, 10, 20)
    ]
    results = judge.judge_run(run)
    rows = [r for r in results if (r.subject or "").endswith(f"/{gates.HEADROOM_PPS}")]
    assert [(r.subject, r.status) for r in rows] == [(FLEET_PPS, gates.UNKNOWN)]
    for resource in MEASURED_NOW:
        [row] = [r for r in results if r.subject == f"{gates.FLEET_SUBJECT}/{resource}"]
        assert row.status == gates.UNKNOWN, resource
    assert judge.verdict_for(judge.judge_run([HEALTHY])) == gates.UNKNOWN


def test_it_remains_a_gate_so_a_waiver_can_attach() -> None:
    [pps] = [
        r
        for r in judge.unmeasurable_headroom_gates()
        if (r.subject or "").endswith(f"/{gates.HEADROOM_PPS}")
    ]
    waived = gates.waive(pps, reason="not funded for this engagement")
    assert waived.status == gates.WAIVED
    assert "not funded" in waived.detail


# --------------------------------------------------------------------------
# The compensating disclosure, enforced
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kw", "verdict"),
    [
        ({}, None),
        ({"gate_results": [_passing(subject="ramp-20")]}, gates.PASS),
        ({"artifact_sha256": None}, None),
    ],
)
def test_the_exclusion_reaches_payload_and_html_under_the_verdict(kw, verdict) -> None:
    """A PASS is exactly when a reader stops reading, so it still carries it."""
    payload = _payload(**kw)
    if verdict:
        assert payload["verdict"]["status"] == verdict
    exclusions = payload["scope_exclusions"]
    assert sorted(exclusions) == [gates.HEADROOM_PPS]
    assert exclusions[gates.HEADROOM_PPS].strip()
    html = run_report.render_load_html(payload)
    assert run_report._esc(exclusions[gates.HEADROOM_PPS]) in html
    if verdict:
        # Placement is the disclosure. A footnote would be a downgrade.
        assert (
            html.index('class="verdict')
            < html.index(NOT_IN_SCOPE)
            < html.index("<h2>Gates</h2>")
        )


def test_a_report_with_nothing_excluded_shows_no_block() -> None:
    html = run_report.render_load_html(_payload(exclusions={}))
    assert NOT_IN_SCOPE not in html
