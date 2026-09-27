"""The collapsed health gates.

Two verdict implementations used to disagree (``service._verdict`` vs
``report.check_json``). Each disagreement is pinned here with the reading that
won, so a future edit that quietly relaxes one shows up as a failing test rather
than as a green CI run on a broken deployment.

The tables share one rule: a gate that measured nothing is UNKNOWN, never PASS,
and claims no metric or value. UNKNOWN still exits non-zero.
"""

from __future__ import annotations

import inspect
import json

import pytest

from voicegateway.livekit_diag import gates

P, F, W, U = gates.PASS, gates.FAIL, gates.WARN, gates.UNKNOWN
_CPU, _MEM = gates.node_cpu_gates, gates.node_memory_gates
_HEAP, _GO, _FD = gates.BASELINE_HEAP, gates.BASELINE_GOROUTINES, "filefd_allocated"
_INF, _NAN = float("inf"), float("nan")
_JUNK_COUNTS = (None, "", "abc", [], {})


def _stats(avg: float, mx: float, trials: int) -> dict:
    """A summarize()-shaped stats block."""
    return {"avg": avg, "p50": avg, "p95": mx, "min": avg, "max": mx, "trials": trials}


def _agent(stats: dict, **kw) -> list[dict]:
    return [{"agent": "a", "stats": stats, **kw}]


def _step(
    clients: int, rtt: float | None, quality: str = "Excellent", samples: object = None
) -> dict:
    """A ramp step. Without ``samples`` it is the pre-count shape archived runs have."""
    step = {"clients": clients, "rtt_ms": rtt, "loss_pct": 0.0, "quality": quality}
    if samples is not None:
        step["samples"] = samples
        step["rtt_stat"] = "mean_of_n" if samples else "not_measured"
    return step


def _timed_out(clients: int, quality: str = "Unknown") -> dict:
    """The step a tier reports when not one ping came back."""
    return _step(clients, 0.0, quality, 0)


def _base(rtt: float | None, samples: object = None, quality: str = "Excellent"):
    """An SFU baseline: a ramp step without the tier."""
    return {k: v for k, v in _step(0, rtt, quality, samples).items() if k != "clients"}


def _reading(node: str, utilisation, **kw) -> gates.NodeUtilisationReading:
    kw.setdefault("samples", 12)
    return gates.NodeUtilisationReading(node=node, utilisation=utilisation, **kw)


def _fd(node: str, used, limit) -> gates.HeadroomReading:
    return gates.HeadroomReading(
        node=node, resource=gates.HEADROOM_FILE_DESCRIPTORS, used=used, limit=limit
    )


def _cmp(metric: str, baseline, post, **kw) -> gates.BaselineComparison:
    kw.setdefault("node", "sfu-1")
    return gates.BaselineComparison(
        metric=metric, baseline=baseline, post_settle=post, **kw
    )


# Well past the settle window, so a case reaches the ratio arithmetic.
_SETTLED = {"baseline_at_ms": 0, "post_settle_at_ms": gates.MIN_SETTLE_MS * 2}


def _ok(result: dict) -> dict:
    return {"ok": True, "result": result}


def _sfu_load(baseline: dict, ramp: list) -> dict:
    return _ok(
        {"baseline": baseline, "ramp": ramp, "target_rtt_ms": 50.0, "resource": None}
    )


def _failing() -> gates.GateResult:
    return gates.GateResult(
        gate=gates.NODE_CPU_GATE,
        status=F,
        detail="peak CPU on sfu-1 was 91.0%",
        subject="sfu-1",
        metric="node_cpu_utilisation",
        value=0.91,
        threshold=0.70,
    )


# ---------------------------------------------------------------------------
# Severity, verdict and exit code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code"), [(P, 0), (W, 1), (U, 1), (F, 1), (gates.WAIVED, 1)]
)
def test_exit_code_is_zero_only_for_pass(status: str, code: int) -> None:
    assert gates.exit_code(status) == code


# fmt: off
_WORST = [
    ([P, W, U], U), ([U, F], F), ([P, P], P),
    # A status from a vocabulary this module does not know is not rounded down.
    (["PROBABLY_FINE"], F),
    # WAIVED sits above PASS and below every measured problem.
    ([P, gates.WAIVED], gates.WAIVED), ([gates.WAIVED, W], W),
    ([gates.WAIVED, U], U), ([gates.WAIVED, F], F),
]
# fmt: on


@pytest.mark.parametrize(("statuses", "worst"), _WORST)
def test_worst_status(statuses: list[str], worst: str) -> None:
    assert gates.worst_status(statuses) == worst


def test_a_run_with_no_gates_is_unknown_not_pass() -> None:
    """Nothing was evaluated, so nothing was demonstrated."""
    assert gates.verdict([]) == U


# ---------------------------------------------------------------------------
# evaluate_checks: the whole-run shape
# ---------------------------------------------------------------------------

# fmt: off
_CHECKS = [
    # summarize's fabricated 0.0 avg over zero trials is not a fast reply.
    ({"latency": _ok({"agents": _agent(_stats(0.0, 0.0, 0))})}, [U]),
    # _verdict never read an sfu_load baseline at all.
    ({"sfu_load": _sfu_load({"rtt_ms": 90.0, "quality": "Poor"}, [])}, [F, U]),
    # A check that errored is FAIL, the stricter reading; one no gate reads is UNKNOWN.
    ({"latency": {"ok": False, "error": "check timed out"}}, [F]),
    ({"telepathy": _ok({})}, [U]),
    ({
        "agents": _ok({"agents": []}),
        "sfu": _ok({"baseline": {"rtt_ms": 90.0, "quality": "Poor"}}),
        "latency": _ok({"agents": _agent(_stats(0.5, 0.6, 2))}),
    }, [P, F, P]),
    # Every ping timed out: on the whole probe, then on the baseline alone.
    ({"sfu_load": _sfu_load(_base(0.0, quality="Unknown"), [_timed_out(2), _timed_out(10)])}, [U, U]),
    ({"sfu_load": _sfu_load(_base(0.0, 0), [_step(2, 11.0, samples=3)])}, [U, P]),
]
# fmt: on


@pytest.mark.parametrize(("checks", "statuses"), _CHECKS)
def test_evaluate_checks(checks: dict, statuses: list[str]) -> None:
    results = gates.evaluate_checks(checks, 1500.0)
    assert [g.status for g in results] == statuses
    assert gates.verdict(results) == gates.worst_status(statuses)
    for gate in results:
        json.loads(json.dumps(gate.as_dict()))


def test_a_failed_check_carries_its_error_and_the_keys_are_a_contract() -> None:
    checks = {"latency": {"ok": False, "error": "check timed out"}}
    [gate] = gates.evaluate_checks(checks, 1500.0)
    assert "check timed out" in gate.detail
    keys = "gate status detail subject metric value threshold"
    assert set(gate.as_dict()) == set(keys.split())


def test_zero_agents_in_rooms_still_passes_the_agents_gate() -> None:
    """An idle registered worker is invisible to list_agents, so no count gate."""
    assert gates.agents_gate({"agents": [], "roster": None}).status == P
    gate = gates.agents_gate({"agents": [], "roster": [{"agent_name": "idle"}]})
    assert gate.status == P
    assert "1 worker(s) on the heartbeat roster" in gate.detail


# ---------------------------------------------------------------------------
# latency_gates: fewer than 10 samples is never called p95
# ---------------------------------------------------------------------------

_TWO = _agent(_stats(1.45, 2.4, 2), samples=[0.5, 2.4])
_TEN = _agent(_stats(0.69, 2.4, 10), samples=[0.5] * 9 + [2.4])
_AVG = "agent_reply_latency_avg_ms"

# fmt: off
_LATENCY = [
    (_agent(_stats(0.0, 0.0, 0), error="no worker joined"), False, U, None),
    ([], False, U, None),
    (_TWO, True, W, "agent_reply_latency_max_of_2_ms"),
    (_TEN, True, W, "agent_reply_latency_p95_ms"),
    # The same input is a WARN under --strict: that is the point of the flag.
    (_TWO, False, P, _AVG),
    (_agent(_stats(2.0, 2.5, 2)), False, W, _AVG),
    # No ``trials``: a positive timing is a reading, 0.0 is the no-samples sentinel.
    (_agent({"avg": 0.8}), False, P, _AVG),
    (_agent({"avg": 0.0}), False, U, None),
]
# fmt: on


@pytest.mark.parametrize(("entries", "strict", "status", "metric"), _LATENCY)
def test_latency_gates(entries: list, strict: bool, status: str, metric) -> None:
    [gate] = gates.latency_gates(entries, 1500.0, strict=strict)
    assert gate.status == status
    assert gate.metric == metric


def test_latency_values_come_from_the_statistic_that_decided() -> None:
    [tail] = gates.latency_gates(_TWO, 1500.0, strict=True)
    assert tail.value == 2400.0
    # compute_percentiles interpolates; the legacy summarize p95 would not.
    [p95] = gates.latency_gates(_TEN, 1500.0, strict=True)
    assert p95.value is not None and 500.0 < p95.value < 2400.0
    [nothing] = gates.latency_gates(_LATENCY[0][0], 1500.0)
    assert "no worker joined" in nothing.detail


# ---------------------------------------------------------------------------
# sfu_quality_gate: Poor/Lost FAIL, and quality and rtt are independent readings
#
# A connection that came up while every ping timed out reports "Excellent"
# beside 0.0ms over 0 samples. Loss is never read: sfu.py hardcodes 0.0.
# ---------------------------------------------------------------------------

# fmt: off
_QUALITY = [
    (_base(90.0, quality="Poor"), F), (_base(90.0, quality="Lost"), F),
    (_base(11.0), P), ({"rtt_ms": 11.0, "loss_pct": 99.0, "quality": "Excellent"}, P),
    # "Unknown" is SfuProbe's absence of a reading, not a good one.
    (None, U), ({"quality": "Unknown"}, U),
    (_base(0.0, 0), U), (_base(11.0, 2), P), (_base(11.0, 1), P), (_base(11.0, "3"), P),
    (_base(0.0, 0, "Poor"), F), (_base(0.0, 0, "Lost"), F), (_base(90.0, 2, "Poor"), F),
    # Legacy baselines without a count: the rtt decides, and 0.0 is not a time.
    (_base(0.0), U), ({"quality": "Excellent"}, U), (_base(None), U),
    *[(_base(11.0) | {"samples": j}, P) for j in _JUNK_COUNTS],
    *[(_base(0.0) | {"samples": j}, U) for j in _JUNK_COUNTS],
]
# fmt: on


@pytest.mark.parametrize(("baseline", "status"), _QUALITY)
def test_sfu_quality_gate(baseline: dict | None, status: str) -> None:
    gate = gates.sfu_quality_gate(baseline)
    assert gate.status == status
    assert gate.value is None
    assert (gate.metric is None) == (status == U)


def test_a_degraded_baseline_never_prints_its_empty_mean_as_a_time() -> None:
    gate = gates.sfu_quality_gate(_base(0.0, 0, "Poor"))
    assert "rtt 0.0ms" not in gate.detail


# ---------------------------------------------------------------------------
# sfu_capacity_gate: find_knee's two opposite Nones, and ramps that measured nothing
# ---------------------------------------------------------------------------


def test_find_knee_returns_the_same_none_for_opposite_ramps() -> None:
    """The ambiguity sfu_capacity_gate closes. Sample counts keep the clean None."""
    from voicegateway.livekit_diag.sfu import RampStep, find_knee

    broken = [RampStep(2, 90.0, 0.0, "Poor"), RampStep(10, 120.0, 0.0, "Poor")]
    clean = [
        RampStep(2, 11.0, 0.0, "Excellent", 2),
        RampStep(10, 14.0, 0.0, "Excellent", 10),
    ]
    assert find_knee(broken, 50.0, 1.0) is None
    assert find_knee(clean, 50.0, 1.0) is None


_POOR = [_step(2, 90.0, "Poor")]

# fmt: off
_CAPACITY = [
    (_POOR + [_step(10, 120.0, "Poor")], 50.0, None, F, 90.0),
    ([_step(2, 11.0), _step(10, 14.0)], 50.0, None, P, 11.0),
    # Finding a knee partway up is the reason to run a ramp.
    ([_step(2, 11.0), _step(10, 14.0), _step(25, 88.0, "Poor")], 50.0, None, P, 11.0),
    # A saturated prober describes this host, so it cannot indict the SFU.
    (_POOR, 50.0, {"saturated": True, "cpu_peak": 99.0}, U, None),
    (_POOR, 50.0, {"saturated": None, "cpu_peak": None}, F, 90.0),
    ([], 50.0, None, U, None), ([_step(2, 11.0)], None, None, U, None),
    ([_timed_out(2), _timed_out(10)], 50.0, None, U, None),
    ([_step(10, 11.0, samples=3)], 50.0, None, P, 11.0),
    ([_step(10, 90.0, samples=3)], 50.0, None, F, 90.0),
    # Poor is an observation; its companion 0.0ms is not.
    ([_timed_out(2, "Poor")], 50.0, None, F, None),
    # Legacy steps without a count: 0.0 or no rtt is not a fast reply.
    ([_step(2, 0.0, "Unknown")], 50.0, None, U, None),
    ([_step(2, 0.0)], 50.0, None, U, None),
    ([{"clients": 2, "quality": "Excellent"}], 50.0, None, U, None),
    ([_step(2, 11.0, samples="3")], 50.0, None, P, 11.0),
    *[([_step(2, 11.0) | {"samples": j}], 50.0, None, P, 11.0) for j in _JUNK_COUNTS],
    *[([_step(2, 0.0, "Unknown") | {"samples": j}], 50.0, None, U, None) for j in _JUNK_COUNTS],
]
# fmt: on


@pytest.mark.parametrize(("ramp", "target", "resource", "status", "value"), _CAPACITY)
def test_sfu_capacity_gate(ramp: list, target, resource, status: str, value) -> None:
    gate = gates.sfu_capacity_gate(ramp, target, resource)
    assert gate.status == status
    assert gate.value == value
    if status == U:
        assert gate.metric is None


def test_a_ramp_that_measured_nothing_says_so_and_keeps_its_budget() -> None:
    gate = gates.sfu_capacity_gate([_timed_out(2), _timed_out(10)], 50.0, None)
    assert "samples 0" in gate.detail
    assert gate.threshold == 50.0


# ---------------------------------------------------------------------------
# establishment_gate: at least 99.5% of attempts, an INCLUSIVE floor
#
# Zero attempts is also zero failures, and every "failures within budget"
# phrasing calls that run perfect. It is UNKNOWN.
# ---------------------------------------------------------------------------

# fmt: off
_ESTABLISHMENT = [
    # Exactly on the bar at every scale: float division keeps the boundary.
    (15000, 14985, None, P), (200, 199, None, P), (2000, 1990, None, P),
    (20000, 19900, None, P), (1000, 994, None, F), (1000, 996, None, P),
    (1000, 996, 0.999, F),
    (0, 0, None, U), (0, 5, None, U), (None, None, None, U),
    (15000, None, None, U), (None, 14985, None, U),
    (100, 101, None, U), (100, -1, None, U), (-5, 0, None, U),
    # bool is an int subclass, so True would otherwise be a count.
    (True, True, None, U), (True, False, None, U),
    ("many", "most", None, U), ([], {}, None, U),
    # int(inf) raises OverflowError, not ValueError, and json.loads accepts Infinity.
    (_INF, 1, None, U), (-_INF, 1, None, U), (_NAN, 1, None, U),
    (100, _INF, None, U), (100, -_INF, None, U), (100, _NAN, None, U),
]
# fmt: on


@pytest.mark.parametrize(("attempted", "succeeded", "bar", "status"), _ESTABLISHMENT)
def test_establishment_gate(attempted, succeeded, bar, status: str) -> None:
    kw = {} if bar is None else {"threshold": bar}
    gate = gates.establishment_gate(attempted=attempted, succeeded=succeeded, **kw)
    assert gate.status == status
    assert gate.threshold == (bar or gates.MIN_ESTABLISHMENT_RATIO)
    if status == U:
        assert gate.metric is None and gate.value is None
    else:
        assert gate.metric == "establishment_ratio"
        assert gate.value == succeeded / attempted


def test_establishment_gate_identity() -> None:
    assert gates.MIN_ESTABLISHMENT_RATIO == 0.995
    gate = gates.establishment_gate(attempted=15000, succeeded=14985, subject="r-500")
    assert gate.gate == gates.ESTABLISHMENT_GATE
    assert gate.subject == "r-500"
    assert "14985 of 15000" in gate.detail


# ---------------------------------------------------------------------------
# node_cpu_gates / node_memory_gates: STRICT per-node ceilings
#
# The opposite direction to establishment's floor: sitting exactly on a ceiling
# has not stayed below it.
# ---------------------------------------------------------------------------

# fmt: off
_NODE = [
    (_CPU, 0.42, P), (_MEM, 0.60, P),
    (_CPU, 0.70, F), (_MEM, 0.75, F), (_CPU, 0.6999, P), (_MEM, 0.7499, P),
    # One number, two verdicts: the ceilings are not interchangeable.
    (_CPU, 0.72, F), (_MEM, 0.72, P), (_CPU, 0.91, F),
    # An idle node passes on evidence; an unscraped one has none.
    (_CPU, 0.0, P), (_CPU, None, U),
]
# fmt: on


@pytest.mark.parametrize(("fn", "utilisation", "status"), _NODE)
def test_node_gates(fn, utilisation: float | None, status: str) -> None:
    samples = 0 if utilisation is None else 12
    [gate] = fn([_reading("sfu-1", utilisation, samples=samples)])
    assert gate.status == status
    assert gate.subject == "sfu-1"
    assert gate.value == utilisation
    assert (gate.metric is None) == (status == U)
    cpu, mem = gates.MAX_NODE_CPU_UTILISATION, gates.MAX_NODE_MEMORY_UTILISATION
    assert gate.threshold == (cpu if fn is _CPU else mem)


def test_node_ceilings_and_one_gate_per_node() -> None:
    """A mean across the fleet hides the one node that saturated."""
    assert gates.MAX_NODE_CPU_UTILISATION == 0.70
    assert gates.MAX_NODE_MEMORY_UTILISATION == 0.75
    readings = [_reading("a", 0.10), _reading("b", 0.12), _reading("c", 0.95)]
    assert [g.status for g in _CPU(readings)] == [P, P, F]
    [gate] = _CPU([_reading("node-a", 0.5, source="node-exporter")])
    assert gate.subject == "node-a/node-exporter"


def test_an_unmeasured_node_carries_its_reason() -> None:
    reason = "every scrape timed out"
    [gate] = _CPU([_reading("n", None, samples=0, unmeasured_reason=reason)])
    assert gate.status == U
    assert reason in gate.detail


@pytest.mark.parametrize("fn", [_CPU, _MEM])
def test_a_window_nobody_sampled_is_unknown_not_an_idle_fleet(fn) -> None:
    [gate] = fn([])
    assert gate.status == U
    assert gate.value is None


# ---------------------------------------------------------------------------
# headroom_gates: an INCLUSIVE floor of 20% left free
#
# 80% of a limit in use passes here while 80% CPU fails its ceiling. The 0.85 in
# the node-count formula is a different number (a 15% CPU margin).
# ---------------------------------------------------------------------------

# fmt: off
_HEADROOM = [
    (400_000, 1_048_576, P), (800, 1000, P), (801, 1000, F), (80, 100, P),
    (0, 100, P), (100, 100, F),
    (None, 1000, U), (500, None, U), (None, None, U),
    # A zero ceiling has no headroom to have, and used > limit is incoherent.
    (0, 0, U), (10, 0, U), (-1, 100, U), (101, 100, U),
]
# fmt: on


@pytest.mark.parametrize(("used", "limit", "status"), _HEADROOM)
def test_headroom_gates(used, limit, status: str) -> None:
    [gate] = gates.headroom_gates([_fd("sfu-1", used, limit)])
    assert gate.status == status
    assert gate.subject == "sfu-1/file_descriptors"
    assert gate.threshold == gates.MIN_HEADROOM_FRACTION
    if status == U:
        assert gate.value is None
    else:
        assert gate.metric == "file_descriptors_headroom"
        assert gate.value == pytest.approx((limit - used) / limit)


def test_headroom_identity() -> None:
    assert gates.MIN_HEADROOM_FRACTION == 0.20
    [gate] = gates.headroom_gates([_fd("sfu-1", 400_000, 1_048_576)])
    assert gate.gate == gates.HEADROOM_GATE
    # The raw counts travel, so a reader can check the percentage.
    assert "400000 of 1048576 used" in gate.detail
    [nothing] = gates.headroom_gates([])
    assert nothing.status == U


def test_a_caller_that_scraped_nothing_files_three_not_measured_rows() -> None:
    """Unmeasured resources file a row saying so instead of vanishing.

    Network is split in and out because a cloud instance meters them against
    separate credit buckets. pps is a permanent scope exclusion (nobody publishes
    a per-instance allowance), so it is never filed as fixable here.
    """
    readings = gates.unscraped_headroom_readings("sfu-1")
    resources = [r.resource for r in readings]
    assert resources == [
        gates.HEADROOM_RTP_PORTS,
        gates.HEADROOM_NETWORK_IN,
        gates.HEADROOM_NETWORK_OUT,
    ]
    assert gates.HEADROOM_PPS not in resources
    results = gates.headroom_gates(
        [_fd("sfu-1", 10, 100), _fd("sfu-2", 95, 100)] + readings
    )
    assert [g.status for g in results] == [P, F, U, U, U]
    assert [g.subject for g in results][2:] == [f"sfu-1/{r}" for r in resources]
    assert len({g.subject for g in results}) == 5


# ---------------------------------------------------------------------------
# return_to_baseline_gates: did the fleet give its resources back after teardown?
#
# heap_inuse and goroutines, never RSS: Go returns freed heap lazily. Quantized
# counters (filefd_allocated ticks in 32s) get an absolute-change floor so a few
# ticks cannot fail a node; memory is deliberately not floored.
# ---------------------------------------------------------------------------

# fmt: off
_RETURN = [
    (_cmp(_HEAP, 100_000_000, 108_000_000), 1.5, P),
    # A per-call goroutine that never exits.
    (_cmp(_GO, 120, 4_800), 1.5, F),
    (_cmp(_HEAP, 100, 150), 1.1, F), (_cmp(_HEAP, 100, 150), 2.0, P),
    # Teardown that has not finished draining looks exactly like a clean one.
    (_cmp(_HEAP, 100, 100, baseline_at_ms=0, post_settle_at_ms=60_000), 1.5, U),
    (_cmp(_HEAP, 100, 100, baseline_at_ms=0, post_settle_at_ms=600_000), 1.5, P),
    # No timestamps means the settle check cannot run, not that it failed.
    (_cmp(_HEAP, 100, 100), 1.5, P),
    (_cmp(_HEAP, None, 100), 1.5, U), (_cmp(_HEAP, 100, None), 1.5, U),
    (_cmp(_HEAP, None, None), 1.5, U),
    (_cmp(_GO, 0, 10), 1.5, U), (_cmp(_GO, -1, 10), 1.5, U),
    # 7 UDP sockets against an idle 3: the denominator carries no meaning.
    (_cmp("sockstat_udp_inuse", 3, 7, **_SETTLED), 1.10, U),
    (_cmp(_FD, 4096, 40960, **_SETTLED), 1.10, F),
    (_cmp(_FD, 4096, 4100, **_SETTLED), 1.10, P),
    # The real case: 96 descriptors is three ticks, under the 128 floor. The
    # same ratio at ten times the magnitude is a real leak.
    (_cmp(_FD, 864.0, 960.0), 1.10, P), (_cmp(_FD, 8640.0, 9600.0), 1.10, F),
    (_cmp(_FD, 864.0, 864.0 + 127.0), 1.10, P),
    (_cmp(_FD, 864.0, 864.0 + 128.0), 1.10, F),
    (_cmp(_FD, 864.0, 880.0), 1.10, P),
    # A large drop is not "a small move" into the floor branch.
    (_cmp(_FD, 2000.0, 800.0), 1.10, P),
    (_cmp("memory_used_bytes", 740_500_000.0, 814_700_000.0), 1.09, F),
]
# fmt: on


@pytest.mark.parametrize(("comparison", "tolerance", "status"), _RETURN)
def test_return_to_baseline_gates(comparison, tolerance: float, status: str) -> None:
    [gate] = gates.return_to_baseline_gates([comparison], tolerance=tolerance)
    assert gate.status == status
    assert gate.threshold == tolerance
    assert gate.subject == f"sfu-1/{comparison.metric}"
    if status != U:
        # The floor suppresses a verdict, never the ratio a reader wants to see.
        ratio = comparison.post_settle / comparison.baseline
        assert gate.value == pytest.approx(ratio)


def test_return_to_baseline_identity() -> None:
    [gate] = gates.return_to_baseline_gates([_cmp(_FD, 864.0, 960.0)], tolerance=1.1)
    assert gate.gate == gates.RETURN_TO_BASELINE_GATE
    assert "under the 128" in gate.detail
    [nothing] = gates.return_to_baseline_gates([], tolerance=1.5)
    assert nothing.status == U
    assert gates.MIN_SETTLE_MS == 300_000
    # Pinned so nobody adds RSS later: it reports a leak on healthy runs.
    assert (_HEAP, _GO) == ("heap_inuse_bytes", "go_goroutines")


def test_the_tolerance_is_required_and_has_no_default() -> None:
    """Near baseline is never quantified by the criterion, so none is invented."""
    params = inspect.signature(gates.return_to_baseline_gates).parameters
    assert params["tolerance"].default is inspect.Parameter.empty
    assert params["tolerance"].kind is inspect.Parameter.KEYWORD_ONLY
    with pytest.raises(TypeError):
        gates.return_to_baseline_gates([_cmp(_HEAP, 10, 10)])


def test_every_baseline_metric_has_a_floor_decision_recorded() -> None:
    """A new metric must not inherit the pure-ratio behaviour silently."""
    from voicegateway.loadtest.aggregation import BASELINE_METRICS

    assert "memory_used_bytes" not in gates.MIN_ABSOLUTE_CHANGE
    decided = set(gates.MIN_ABSOLUTE_CHANGE) | {"memory_used_bytes"}
    assert set(BASELINE_METRICS) <= decided, (
        f"no floor decision recorded for {set(BASELINE_METRICS) - decided}: add "
        "it to MIN_ABSOLUTE_CHANGE, or to this test's exemption with a reason"
    )


# ---------------------------------------------------------------------------
# two_way_media_gate: a call that answered and carried no audio is a failure
# ---------------------------------------------------------------------------

# fmt: off
_MEDIA = [
    # Zero silent calls is the bar, not a percentage. 16606/12198 is the 24h soak.
    (500, 0, P), (999, 1, F), (16606, 12198, F),
    (None, None, U), (5, None, U), (None, 5, U), (0, 0, U),
    (10, -1, U), (-5, 0, U), (True, False, U),
]
# fmt: on


@pytest.mark.parametrize(("with_inbound", "without_inbound", "status"), _MEDIA)
def test_two_way_media_gate(with_inbound, without_inbound, status: str) -> None:
    gate = gates.two_way_media_gate(
        answered_with_inbound=with_inbound, answered_without_inbound=without_inbound
    )
    assert gate.status == status
    if status != U:
        assert gate.value == without_inbound


def test_two_way_media_reports_the_silent_share_and_is_registered() -> None:
    gate = gates.two_way_media_gate(
        answered_with_inbound=16606, answered_without_inbound=12198
    )
    assert "42.3%" in gate.detail
    assert {"TWO_WAY_MEDIA_GATE", "two_way_media_gate"} <= set(gates.__all__)
    assert gates.TWO_WAY_MEDIA_GATE in gates.ALL_GATES
    # A count of calls, never rendered as a ratio.
    assert gates.TWO_WAY_MEDIA_GATE not in gates.RATIO_GATES


# ---------------------------------------------------------------------------
# WAIVED: a threshold the run was not held to, recorded, never a silent pass
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_a_waiver_requires_a_reason(blank: str) -> None:
    with pytest.raises(ValueError, match="requires a reason"):
        gates.waive(_failing(), reason=blank)


def test_a_waiver_is_recorded_and_never_collapses_to_pass() -> None:
    waived = gates.waive(_failing(), reason="  CPU exporter not funded  ")
    assert waived.status == gates.WAIVED
    clean = gates.GateResult(gate="x", status=P, detail="fine")
    assert gates.verdict([clean, waived]) == gates.WAIVED
    # The reason is stripped and carried structurally and through JSON.
    assert waived.waiver_reason == "CPU exporter not funded"
    payload = json.loads(json.dumps(waived.as_dict()))
    assert payload["waiver_reason"] == "CPU exporter not funded"
    # The measurement, and what it would have been, survive the decision.
    kept = (waived.gate, waived.subject, waived.metric, waived.value, waived.threshold)
    assert kept == (gates.NODE_CPU_GATE, "sfu-1", "node_cpu_utilisation", 0.91, 0.70)
    assert "Would otherwise have been FAIL" in waived.detail
    [line] = gates.summary_lines([waived])
    assert "[WAIVED]" in line


def test_the_waived_status_is_rendered_downstream() -> None:
    """Prometheus drops unknown statuses; the report must not say "could not evaluate"."""
    from voicegateway.livekit_diag import run_report
    from voicegateway.server.api import metrics as metrics_api

    assert gates.WAIVED in metrics_api._GATE_STATUSES
    assert gates.WAIVED in run_report._VERDICT_MEANING


# ---------------------------------------------------------------------------
# Every gate is synchronous and JSON-safe
# ---------------------------------------------------------------------------


def test_gates_are_synchronous_and_json_safe() -> None:
    sync = (_CPU, _MEM, gates.headroom_gates, gates.return_to_baseline_gates)
    for fn in (gates.establishment_gate, *sync):
        assert not inspect.iscoroutinefunction(fn)
    results = [
        gates.establishment_gate(attempted=100, succeeded=100),
        gates.establishment_gate(attempted=0, succeeded=0),
        *_CPU([_reading("n", 0.5), _reading("m", None, samples=0)]),
        *gates.headroom_gates(
            [_fd("n", 10, 100), *gates.unscraped_headroom_readings("n")]
        ),
        *gates.return_to_baseline_gates(
            [_cmp(_HEAP, 100, 110), _cmp(_GO, None, 5)], tolerance=1.5
        ),
    ]
    for gate in results:
        json.loads(json.dumps(gate.as_dict()))
