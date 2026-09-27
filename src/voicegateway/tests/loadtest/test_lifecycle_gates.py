"""The three contracted criteria that had no gate at all.

The report emitted five gate families, and mapping them onto the client's list
left three lines uncovered:

* "No crashes, OOM events, port exhaustion, file-descriptor exhaustion, or
  unplanned restarts." Only file descriptors were gated.
* "No increasing stale calls, rooms, sessions, or memory usage." Nothing looked
  at trend, so a run that leaked steadily for ten minutes passed every gate.
* "Resource usage returns close to baseline after calls terminate." Nothing
  compared before against after.

All three are computable from columns ``node_samples`` already stored, except
the kernel OOM counter, which was verified present on a live node-exporter
before being wired rather than assumed.

**Restarts and OOM are one criterion and two signals, never one.** A crash
presents as a restart, so the restart check covers crashes. It does NOT cover
OOM: the kernel can kill a child or a sibling without the scraped service
restarting, so a clean restart signal must never vouch for the OOM half.
"""

from __future__ import annotations

import pytest

from voicegateway.livekit_diag import gates
from voicegateway.loadtest import judge

MIN = gates.MIN_TREND_SAMPLES


def _life(**kw):
    return gates.LifecycleReading(node="sip-1", source="livekit-sip", **kw)


def _trend(values, *, rising_is_bad=True, metric="m", node="n", window_ms=None):
    return gates.TrendReading(
        node=node,
        metric=metric,
        values=tuple(values),
        rising_is_bad=rising_is_bad,
        window_ms=window_ms,
    )


def _steady(baseline: float, drift: float):
    """A window that ramps, holds at baseline, then settles at baseline+drift."""
    return [0.0] * MIN + [baseline] * MIN + [baseline + drift] * MIN


# --------------------------------------------------------------------------
# Restarts, crashes and OOM
# --------------------------------------------------------------------------

_CLEAN = (0.0, 0.0, 0.0)
_STEADY = (1785.0,) * 3
_MOVED = (1785.0, 1785.0, 1900.0)


@pytest.mark.parametrize(
    ("starts", "ooms", "window", "status", "detail"),
    [
        # The new start time (1900) falls inside the window (opened at 1700).
        (_MOVED, _CLEAN, 1700.0, gates.FAIL, "restarted during the window"),
        # A new start time from BEFORE the window belongs to a process already
        # running when the run began: the series moved between processes. The
        # gap is the gate's own arithmetic, 1786374592.118 - 1786373687.16.
        (
            (1786373510.14, 1786373510.14, 1786373687.16),
            _CLEAN,
            1786374592.118,
            gates.FAIL,
            "905s BEFORE",
        ),
        # No window, so the question cannot be decided: the weaker claim.
        (_MOVED, _CLEAN, None, gates.FAIL, "cannot be said"),
        # The kernel killed something and the service never restarted.
        (_STEADY, (0.0, 0.0, 1.0), None, gates.FAIL, "did not restart"),
        (_STEADY, _CLEAN, 1700.0, gates.PASS, "held one process start time"),
        # Cumulative since boot. A kill last week is not a kill during the run.
        (_STEADY, (7.0, 7.0, 7.0), None, gates.PASS, None),
        # A NULL is a missed scrape, not a process that started at zero.
        ((1785.0, None, 1785.0), (0.0, None, 0.0), None, gates.PASS, None),
        # An unmeasured half is UNKNOWN, never vouched for by the other.
        (_STEADY, (), None, gates.UNKNOWN, "does not prove no OOM"),
        ((), (0.0, 0.0), None, gates.UNKNOWN, "does not prove no restart"),
        ((None, None), (), None, gates.UNKNOWN, None),
    ],
)
def test_process_lifecycle_gate(starts, ooms, window, status, detail) -> None:
    gate = gates.process_lifecycle_gate(
        _life(start_times=starts, oom_kills=ooms, window_start_s=window)
    )
    assert gate.status == status, gate.detail
    if detail:
        assert detail in gate.detail
    if len(set(starts) - {None}) > 1:
        # Whatever caused the change, counter rates across it cannot be trusted.
        assert "unusable" in gate.detail
    if detail != "restarted during the window":
        assert "restarted during the window" not in gate.detail


def test_rows_are_per_node_per_source() -> None:
    readings = [
        gates.LifecycleReading(
            node=node,
            source="livekit-sip",
            start_times=(1785.0, 1900.0) if node == "sip-2" else (1785.0, 1785.0),
            oom_kills=(0.0, 0.0),
        )
        for node in ("sip-1", "sip-2")
    ]
    results = gates.process_lifecycle_gates(readings)
    assert len(results) == 2
    [failed] = [r for r in results if r.status == gates.FAIL]
    assert failed.subject == "sip-2/livekit-sip"


# --------------------------------------------------------------------------
# Stale resource trend
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "rising_is_bad", "status"),
    [
        # First-versus-last would read every ramp as a leak; middle third
        # against final third excludes the ramp by construction.
        pytest.param([1, 5, 10] + [20] * 2 * MIN, True, gates.PASS, id="ramp"),
        # memory_available_bytes leaks DOWNWARD.
        pytest.param([900] * 2 * MIN + [500] * MIN, False, gates.FAIL, id="free-falls"),
        pytest.param([500] * 2 * MIN + [900] * MIN, False, gates.PASS, id="free-rises"),
        # Non-vacuous: the noise floor must not have turned the gate off.
        pytest.param(_steady(7.14e9, -7.14e9 * 0.05), False, gates.FAIL, id="5pct"),
        # Exactly at the 1% floor is a leak, so the bar is a bar and not a gap.
        pytest.param(_steady(1000.0, 10.0), True, gates.FAIL, id="at-floor"),
        pytest.param(_steady(1000.0, 9.0), True, gates.PASS, id="below-floor"),
        # No fraction exists to take. A count going 0 to 5 is real.
        pytest.param(_steady(0.0, 5.0), True, gates.FAIL, id="zero-baseline"),
        # A gap must not drag a mean down as if it were a zero. Each third still
        # clears MIN_TREND_SAMPLES after its gap is removed.
        pytest.param(
            [1, 2, 3, 4] + [10, None, 10, 10] * 2, True, gates.PASS, id="gaps"
        ),
    ],
)
def test_resource_trend_gate(values, rising_is_bad, status) -> None:
    gate = gates.resource_trend_gate(_trend(values, rising_is_bad=rising_is_bad))
    assert gate.status == status, gate.detail


@pytest.mark.parametrize(
    ("values", "status", "detail"),
    [
        ([1, 5, 10] + [20] * MIN + [30] * MIN, gates.FAIL, None),
        # The measurement SUCCEEDED and found no meaningful drift: PASS, since
        # UNKNOWN would report a clean node as unevaluated.
        (_steady(7.15e9, 406_727), gates.PASS, "measurement noise rather than a leak"),
        # "Too short to tell" and "flat" look identical in one number.
        ([1] * 6, gates.UNKNOWN, "Too short to tell is not flat"),
    ],
)
def test_the_trend_detail_carries_the_finding(values, status, detail) -> None:
    gate = gates.resource_trend_gate(_trend(values))
    assert gate.status == status
    if detail:
        assert detail in gate.detail
    else:
        assert gate.value == pytest.approx(10.0)


# The fourteen resource_trend rows a real 100-concurrent fleet run produced:
# (node, metric, steady-state baseline, drift, rising_is_bad). Every one of them
# must PASS. Two of them did not before the magnitude floor existed, and one of
# those was the box running the collector, carrying no test load at all.
FLEET_ROWS = (
    ("agent-0", "memory_available_bytes", 3.2e10, 17_452_686, False),
    ("loadgen-0", "memory_available_bytes", 1.57e10, 2_448_071, False),
    ("loadgen-1", "memory_available_bytes", 9.5e9, 9_500, False),
    ("monitor-0", "memory_available_bytes", 7.15e9, -406_727, False),
    ("sfu-1", "memory_available_bytes", 7.14e9, -7_607_979, False),
    ("sfu-2", "memory_available_bytes", 7.25e9, 1_500_302, False),
    ("sip-1", "memory_available_bytes", 1.54e10, 45_882_510, False),
    ("sip-2", "memory_available_bytes", 1.57e10, 6_103_410, False),
    ("sfu-1", "rooms", 57.9, -10, True),
    ("sfu-1", "participants", 215.7, -57, True),
    ("sfu-2", "rooms", 58.9, -19, True),
    ("sfu-2", "participants", 110.4, -38, True),
    ("sip-1", "sip_calls_active", 98.2, -28, True),
    ("sip-2", "sip_calls_active", 100.0, 0, True),
)


@pytest.mark.parametrize(
    ("node", "metric", "baseline", "drift", "rising_is_bad"), FLEET_ROWS
)
def test_no_row_from_the_real_fleet_run_fails(
    node, metric, baseline, drift, rising_is_bad
) -> None:
    """monitor-0 failed on 407 KB, 0.0057% of 7.15 GB, on the collector's box.

    The gate was reading the SIGN of ordinary memory noise.
    """
    reading = _trend(
        _steady(baseline, drift),
        rising_is_bad=rising_is_bad,
        metric=metric,
        node=node,
        window_ms=600_000,
    )
    gate = gates.resource_trend_gate(reading)
    assert gate.status == gates.PASS, gate.detail


def test_the_floor_sits_well_above_observed_noise() -> None:
    """1% against the largest wrong-way movement on the fleet run above."""
    assert gates.MIN_TREND_DRIFT_FRACTION == 0.01
    unfavourable = [
        abs(d) / b
        for _, _, b, d, rising_bad in FLEET_ROWS
        if b and (d > 0 if rising_bad else d < 0)
    ]
    assert unfavourable, "fixture would be vacuous with nothing moving wrongly"
    assert max(unfavourable) < gates.MIN_TREND_DRIFT_FRACTION


def test_the_rate_is_reported_but_never_gated_on() -> None:
    """Noise scales with the size of the thing, not with how long you watch."""
    values = _steady(1000.0, 5.0)
    with_window = gates.resource_trend_gate(_trend(values, window_ms=600_000))
    without = gates.resource_trend_gate(_trend(values))
    assert "per hour" in with_window.detail
    assert "per hour" not in without.detail
    assert with_window.status == without.status


def test_the_thirds_split_excludes_the_first_third() -> None:
    middle, final = gates.steady_state_thirds([0, 0, 0, 1, 1, 1, 2, 2, 2])
    assert (middle, final) == ([1, 1, 1], [2, 2, 2])


# --------------------------------------------------------------------------
# Return to baseline
# --------------------------------------------------------------------------


def test_no_pre_run_baseline_is_unknown_not_pass() -> None:
    """Nobody established what baseline was, so nothing returned to it."""
    [gate] = gates.return_to_baseline_gates(
        [
            gates.BaselineComparison(
                node="sip-1",
                metric="memory_available_bytes",
                baseline=None,
                post_settle=1000.0,
                unmeasured_reason="no idle sample was recorded before the test",
            )
        ],
        tolerance=judge.BASELINE_TOLERANCE,
    )
    assert gate.status == gates.UNKNOWN


def test_the_tolerance_is_stated_by_the_caller_not_defaulted() -> None:
    """A ratio CEILING above 1.0, stated by the judge rather than invented."""
    with pytest.raises(TypeError):
        gates.return_to_baseline_gates([])  # type: ignore[call-arg]
    assert judge.BASELINE_TOLERANCE > 1.0


# --------------------------------------------------------------------------
# All three reach a run, and none of them is a ratio
# --------------------------------------------------------------------------


def test_a_run_with_no_window_still_reports_all_three() -> None:
    """Once per RUN, not per step: a contracted criterion never has no row."""
    families = {
        g.gate
        for g in judge.judge_run(
            [{"name": "r", "attempted_calls": 1, "succeeded_calls": 1}]
        )
    }
    assert {
        gates.PROCESS_LIFECYCLE_GATE,
        gates.RESOURCE_TREND_GATE,
        gates.RETURN_TO_BASELINE_GATE,
    } <= families


@pytest.mark.parametrize(
    "gate", [gates.PROCESS_LIFECYCLE_GATE, gates.RESOURCE_TREND_GATE]
)
def test_count_gates_are_registered_and_are_not_ratios(gate) -> None:
    assert gate in gates.ALL_GATES
    assert gate not in gates.RATIO_GATES
