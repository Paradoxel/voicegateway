"""Fleet sizing, and the false ceiling that makes it buy the wrong number.

The centrepiece is the plateau. A ramp that holds its arrival rate fixed while
raising the target concurrency stops climbing at ``rate x duration``, and every
step above that measures the load generator rather than the node. The run
completes, every number in it is internally consistent, and the plateau reads as
the node's ceiling. Nothing downstream can catch it, so it is caught here.

The two margins are also pinned apart. 0.85 is a sizing decision; 0.20 is an
acceptance threshold on a run that already happened. They look like they should
sum to 1.0, and a reader who makes them do so breaks the sizing.
"""

from __future__ import annotations

import math

import pytest

from voicegateway.livekit_diag import gates
from voicegateway.loadtest import capacity
from voicegateway.loadtest.capacity import RampStep

# A ramp that holds the rate fixed while raising the target: 2 calls/s with a
# 60 s hold sustains about 124 concurrent, so the last two steps are asking for
# concurrency the generator cannot produce.
FIXED_RATE_RAMP = [
    RampStep(target_concurrency=n, rate_per_second=2.0, hold_seconds=60.0)
    for n in (25, 50, 100, 150, 200)
]


def _ramp(*pairs, target=True, peak=None, **fields) -> list[RampStep]:
    """Steps that reached ``peak`` or what they asked for: ``(target, cpu)``.

    ``target=False`` leaves ``target_concurrency`` NULL, which is what imported
    rows routinely carry: the target lives in the generator's scenario file.
    """
    return [
        RampStep(
            target_concurrency=n if target else None,
            peak_concurrency=peak or n,
            peak_cpu_utilisation=cpu,
            **fields,
        )
        for n, cpu in pairs
    ]


def _parallel_generators(count: int = 12, samples: int | None = 1374) -> list[RampStep]:
    """Rows shaped like a real churn run: N generators, one shared window.

    The processes all watched the same fleet for the same 20 minutes, so they
    report the same peak CPU from the same sample count.
    """
    return _ramp(
        *[(51, 0.5465968586387453)] * count,
        duration_seconds=1200.0,
        samples_in_window=samples,
    )


#: An observed run: 25 concurrent at 83.8% CPU over a 110-second ramp step and
#: 100 at 66.5% on the same node over a soak. Sized from the ramp, 100 calls
#: needs nine nodes; the soak on the same page shows one node doing it.
_OBSERVED = (
    _ramp((25, 0.838), duration_seconds=110.0)
    + _ramp((30, 0.712), duration_seconds=84.0)
    + _ramp((100, 0.665), duration_seconds=600.0)
)
_SHORT = _ramp((15, 0.59), (25, 0.84), duration_seconds=110.0)
_SATURATED = ((100, 0.31), (150, 0.48), (200, 0.66), (250, 0.79))


# --------------------------------------------------------------------------
# Little's law
# --------------------------------------------------------------------------


def test_sustainable_concurrency_counts_setup_not_just_hold() -> None:
    """A call occupies a slot from when it is placed, not from when answered."""
    assert capacity.sustainable_concurrency(2.0, 60.0) == pytest.approx(124.0)
    # Ignoring the setup overhead would say 120 and overstate what fits.
    assert capacity.sustainable_concurrency(2.0, 60.0, setup_overhead_s=0.0) == 120.0


@pytest.mark.parametrize(
    ("target", "hold", "rate"),
    [(500, 118.0, 4.1667), (300, 118.0, 2.5), (150, 118.0, 1.25), (100, 178.0, 0.5556)],
)
def test_required_rate_is_the_inverse(target, hold, rate) -> None:
    """The rates a well-formed plan declares are exactly target / duration."""
    got = capacity.required_rate(target, hold)
    assert got == pytest.approx(rate, abs=1e-4)
    assert capacity.sustainable_concurrency(got, hold) == pytest.approx(target)


@pytest.mark.parametrize(
    "call",
    [
        lambda: capacity.sustainable_concurrency(0.0, 60.0),
        # A node that carries nothing is refused rather than divided by.
        lambda: capacity.nodes_for(500, 0),
        # Nothing here can compute a machine type, so an uncited one is invented.
        lambda: capacity.InstanceType(name="c7i.2xlarge", role="SIP", citation="  "),
    ],
)
def test_invalid_inputs_are_refused(call) -> None:
    with pytest.raises(ValueError):
        call()


def test_a_cited_instance_type_carries_its_source_through() -> None:
    quoted = capacity.InstanceType(
        name="c7i.2xlarge", role="SIP", citation="sizing-runbook.md:115"
    )
    assert quoted.citation == "sizing-runbook.md:115"


# --------------------------------------------------------------------------
# The false ceiling
# --------------------------------------------------------------------------


def test_a_fixed_rate_ramp_is_caught_before_it_runs() -> None:
    """Predicted from the plan alone, and the fix is quantified, not just flagged."""
    unreachable = capacity.unreachable_steps(FIXED_RATE_RAMP)
    by_target = {step.target_concurrency: rate for step, _, rate in unreachable}
    assert by_target == pytest.approx({150: 2.419, 200: 3.226}, abs=1e-3)
    # Non-vacuous: the low steps are genuinely reachable at the same rate.
    assert capacity.unreachable_steps(FIXED_RATE_RAMP[:3]) == []

    finding = capacity.detect_plateau(FIXED_RATE_RAMP)
    assert (finding.plateaued, finding.plateau_at) == (True, 124)
    assert finding.unreachable_targets == [150, 200]
    assert "generator" in finding.detail


@pytest.mark.parametrize(
    ("steps", "plateau_at"),
    [
        # The plan recorded no rates, but what it reached betrays it.
        ([(100, 100), (150, 124), (200, 125)], 125),
        # A small shortfall on every step is still a climb.
        ([(100, 100), (150, 149), (200, 198)], None),
        # Imported rows carry no target. Comparing two unknown targets raised a
        # TypeError and took the whole report down.
        (_ramp((100, 0.31), (150, 0.48), (200, 0.66), target=False), None),
    ],
)
def test_a_plateau_is_caught_from_results(steps, plateau_at) -> None:
    if isinstance(steps[0], tuple):
        steps = [RampStep(target_concurrency=t, peak_concurrency=p) for t, p in steps]
    finding = capacity.detect_plateau(steps)
    assert finding.plateaued is (plateau_at is not None)
    if plateau_at is not None:
        assert finding.plateau_at == plateau_at
        assert "not attributable" in finding.detail


# --------------------------------------------------------------------------
# Deriving the calls-per-node figure
# --------------------------------------------------------------------------

_PLATEAUED = _ramp(
    (25, 0.12), (50, 0.24), (100, 0.48), rate_per_second=2.0, hold_seconds=60.0
) + _ramp((150, 0.58), (200, 0.58), peak=124, rate_per_second=2.0, hold_seconds=60.0)


_FINGERPRINTED = [
    RampStep(
        target_concurrency=n,
        peak_concurrency=n,
        peak_cpu_utilisation=cpu,
        duration_seconds=600.0,
        samples_in_window=samples,
    )
    for (n, cpu), samples in zip(_SATURATED, (300, 305, 298, 301), strict=True)
]


@pytest.mark.parametrize(
    ("steps", "expected", "reason"),
    [
        # A generator's limit sized as a node's buys wrong. 124 looks like an
        # answer, which is exactly why returning it would be the bug.
        (_PLATEAUED, None, ["generator"]),
        # Unsaturated: 200 is a floor, and sizing from a floor OVER-provisions.
        # The message read "under-provision" until 2026-08, which was backwards.
        (_ramp((100, 0.2), (150, 0.3), (200, 0.41)), None, ["AT LEAST 200", "!under"]),
        # 200 sat at 66%, under the ceiling; 250 breached it at 79%. No duration
        # was recorded, and not knowing is not evidence the step was short.
        (_ramp(*_SATURATED), 200, ["recorded no duration"]),
        (
            _ramp(*_SATURATED, duration_seconds=capacity.MIN_STEADY_STATE_S),
            200,
            ["70%", "!no duration"],
        ),
        (_ramp((100, None), (200, None)), None, ["nothing here shows"]),
        (_ramp((100, 0.81), (150, 0.93)), None, ["exceeded"]),
        # "No plateau detected" must not mean "the check could not run", but one
        # step cannot plateau against anything and refuses for another reason.
        (_ramp(*_SATURATED[:3], target=False), None, ["could not be ruled out"]),
        (_ramp((100, None), target=False), None, ["!could not be ruled out"]),
        # The saturation was a call-setup rate borrowed from short steps.
        (_OBSERVED, None, ["2 step(s) shorter than 300s"]),
        (_SHORT, None, ["longest 110s"]),
        # The duration filter runs first: a plateau across short steps is a fact
        # about setup rates, not the generator.
        (
            _ramp((150, 0.58), (200, 0.58), peak=124, duration_seconds=60.0),
            None,
            ["ran for under 300s", "!stopped scaling"],
        ),
        # Twelve generators each peaked at 51 while the fleet carried 612. The
        # report used to answer "AT LEAST 51", a per-generator number in a
        # sentence about a node.
        (_parallel_generators(), None, ["612", "!AT LEAST"]),
        (_parallel_generators(count=1), None, ["!parallel"]),
        # Sequential steps see different windows, so they still derive.
        (_FINGERPRINTED, 200, []),
    ],
)
def test_derive_calls_per_node(steps, expected, reason) -> None:
    value, text = capacity.derive_calls_per_node(steps, cpu_ceiling=0.70)
    assert value == expected
    for fragment in reason:
        if fragment.startswith("!"):
            assert fragment[1:] not in text, text
        else:
            assert fragment in text, text


def test_the_parallel_refusal_quotes_no_per_generator_figure() -> None:
    _, reason = capacity.derive_calls_per_node(_parallel_generators(), cpu_ceiling=0.70)
    assert "51" not in reason.replace("612", ""), reason
    assert "node count" in reason or "number of nodes" in reason


@pytest.mark.parametrize(("steps", "tempting"), [(_OBSERVED, 100), (_SHORT, 15)])
def test_without_the_duration_floor_a_wrong_figure_ships(steps, tempting) -> None:
    """Non-vacuous: the unfiltered derivation really does answer."""
    value, _ = capacity.derive_calls_per_node(
        steps, cpu_ceiling=0.70, min_steady_state_s=0.0
    )
    assert value == tempting


def test_the_cpu_ceiling_used_is_the_gate_threshold() -> None:
    """The sizing ceiling and the acceptance ceiling are the same 70%."""
    value, _ = capacity.derive_calls_per_node(
        _ramp((200, 0.69), (250, 0.71)),
        cpu_ceiling=gates.MAX_NODE_CPU_UTILISATION,
    )
    assert value == 200


def test_the_duration_floor_is_inclusive() -> None:
    """A step landing exactly on it has run long enough."""
    [on] = _ramp((100, 0.5), duration_seconds=capacity.MIN_STEADY_STATE_S)
    [under] = _ramp((100, 0.5), duration_seconds=capacity.MIN_STEADY_STATE_S - 0.1)
    eligible, too_short, _ = capacity._steady_state_steps(
        [on, under], capacity.MIN_STEADY_STATE_S
    )
    assert (eligible, too_short) == ([on], [under])


@pytest.mark.parametrize(
    ("steps", "expected"),
    [
        (_parallel_generators(), 12),
        (_parallel_generators(count=1), 0),
        # No window fingerprint on older artifacts: not guessed at.
        (_parallel_generators(samples=None), 0),
    ],
)
def test_concurrent_generators(steps, expected) -> None:
    assert capacity.concurrent_generators(steps) == expected


# --------------------------------------------------------------------------
# The table
# --------------------------------------------------------------------------


def test_the_spare_node_applies_at_every_tier_not_just_the_largest() -> None:
    """500 at C=150: ceil(500 / (0.85 x 150)) + 1 = ceil(3.92) + 1 = 5."""
    table = capacity.capacity_table(150)
    assert [t.target_concurrency for t in table] == [100, 150, 300, 500]
    assert all(t.spare_nodes == 1 for t in table)
    assert table[-1].usable_per_node == pytest.approx(127.5)
    assert capacity.capacity_table(150, tiers=(500,)) == [table[-1]]


@pytest.mark.parametrize(
    ("calls_per_node", "target", "expected"),
    [
        (150, 500, 5),
        # A ramp plateauing at 124 buys six where a real 200/node needs four.
        (200, 500, 4),
        (124, 500, 6),
        (150, 100, 2),
        (150, 300, 4),
        # 3.1 nodes of load is four machines. Rounding down under-provisions.
        (150, 400, 5),
        # What a setup-dominated ramp shipped: 15/node sizes 100 at nine.
        (15, 100, 9),
    ],
)
def test_node_counts_across_the_table(calls_per_node, target, expected) -> None:
    tier = capacity.nodes_for(target, calls_per_node)
    assert tier.nodes == expected
    load = math.ceil(target / (capacity.SIZING_MARGIN * calls_per_node))
    assert tier.nodes == tier.nodes_for_load + 1 == load + 1


def test_the_sizing_margin_and_the_headroom_floor_are_not_complements() -> None:
    """0.85 decides how many nodes to buy; 0.20 judges a run already done.

    Different quantities, different questions, and this test exists so nobody
    "corrects" one to match the other.
    """
    assert capacity.SIZING_MARGIN == 0.85
    assert gates.MIN_HEADROOM_FRACTION == 0.20
    assert capacity.SIZING_MARGIN + gates.MIN_HEADROOM_FRACTION != 1.0
