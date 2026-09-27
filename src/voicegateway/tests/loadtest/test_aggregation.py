"""Correlating node samples to a test window, and the ways that reads wrong.

The sharp one is memory. Peak utilisation is where ``memory_available_bytes`` is
at its MINIMUM, and reaching for the maximum reports a machine at its emptiest as
its busiest. The fixtures below make the two answers differ by a wide margin so
the test cannot pass by coincidence.

Everything here is overlap, never attribution. Nothing asserts a node served a
call, because nothing server-side can.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlmodel import SQLModel

from voicegateway.livekit_diag import gates
from voicegateway.loadtest import aggregation
from voicegateway.models.node_sample_model import NodeSample  # noqa: F401
from voicegateway.repository import node_correlation_repository as correlation
from voicegateway.repository.node_samples_repository import (
    COUNTER_COLUMNS,
    GAUGE_COLUMNS,
    NodeSampleInput,
    insert_samples,
)

T0 = 1_785_520_800_000  # 2026-07-31T18:00:00Z
SEC = 1000


@pytest.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'nodes.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(
            SQLModel.metadata.create_all,
            tables=[SQLModel.metadata.tables["node_samples"]],
        )
    async with AsyncSession(engine) as session:
        yield session
    await engine.dispose()


def _sample(offset_s: int, node: str = "sfu-1", **values) -> NodeSampleInput:
    # values is a validated mapping, not kwargs: insert_samples RAISES on an
    # unknown key rather than dropping it, which is what keeps a typo'd column
    # from silently becoming a column nobody wrote.
    return NodeSampleInput(
        node=node,
        source="node_exporter",
        at_ms=T0 + offset_s * SEC,
        outcome="ok",
        values=values,
    )


def _cpu(s: int, total: float | None, idle: float | None, **more) -> NodeSampleInput:
    return _sample(s, cpu_seconds_total=total, cpu_idle_seconds_total=idle, **more)


def _mem(s: int, total: int | None, available: int | None) -> NodeSampleInput:
    return _sample(s, memory_total_bytes=total, memory_available_bytes=available)


# A node whose CPU climbs to 75% busy. cpu_seconds_total accrues at the core
# count (4 here) and idle at whatever is left over, so an idle rate of 1.0/s
# against a 4.0/s capacity is 75% utilisation.
_BUSY_CPU = [
    _cpu(0, 1000.0, 800.0),
    _cpu(10, 1040.0, 830.0),  # idle 3.0/s of 4.0/s -> 25% busy
    _cpu(20, 1080.0, 850.0),  # idle 2.0/s of 4.0/s -> 50% busy
    _cpu(30, 1120.0, 860.0),  # idle 1.0/s of 4.0/s -> 75% busy  <- peak
    _cpu(40, 1160.0, 890.0),  # idle 3.0/s of 4.0/s -> 25% busy
]


async def _aggregate(db: AsyncSession, rows, end_s: int, **window):
    await insert_samples(db, rows)
    agg = await aggregation.aggregate_test_window(
        db, started_at_ms=T0, ended_at_ms=T0 + end_s * SEC, **window
    )
    assert agg is not None
    return agg


# --------------------------------------------------------------------------
# Peaks, and the memory min-vs-max trap
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rows", "end_s", "peak", "expected", "samples"),
    [
        (_BUSY_CPU, 40, "cpu", 0.75, 4),
        # A reboot zeroes the counter. That instant is unknown, never 0% busy:
        # the one sourceable point is the 25% one.
        (
            [_cpu(0, 1000.0, 800.0), _cpu(10, 1040.0, 830.0), _cpu(20, 5.0, 4.0)],
            20,
            "cpu",
            0.25,
            1,
        ),
        # THE TRAP. Peak utilisation is the MINIMUM of memory_available_bytes:
        # it bottoms out at 100 of 1000, 90% used. max(available) reads 10%.
        (
            [_mem(0, 1000, 900), _mem(10, 1000, 400), _mem(20, 1000, 100)]
            + [_mem(30, 1000, 600)],
            30,
            "memory",
            0.90,
            4,
        ),
        # Paired within one row. Both rows are 50% used; min(available) and
        # max(total) across rows would compute 1 - 100/2000 = 95%.
        ([_mem(0, 2000, 1000), _mem(10, 200, 100)], 10, "memory", 0.50, 2),
        # A row missing either half is skipped, not zeroed or counted.
        (
            [_mem(0, 1000, None), _mem(10, None, 500), _mem(20, 1000, 200)],
            20,
            "memory",
            0.80,
            1,
        ),
    ],
    ids=["cpu-rates", "cpu-counter-reset", "memory-min", "memory-paired", "half-row"],
)
async def test_the_peak_is_read_from_the_right_samples(
    db: AsyncSession, rows, end_s: int, peak: str, expected: float, samples: int
) -> None:
    agg = await _aggregate(db, rows, end_s)
    assert getattr(agg, f"peak_{peak}_utilisation") == pytest.approx(expected)
    [reading] = getattr(agg, f"{peak}_readings")
    assert reading.samples == samples


@pytest.mark.parametrize(
    ("peak", "gate"),
    [("cpu", gates.node_cpu_gates), ("memory", gates.node_memory_gates)],
)
async def test_an_unmeasured_window_reads_none_and_gates_unknown(
    db: AsyncSession, peak: str, gate
) -> None:
    """Not 0.0. No ceiling was demonstrated, which is not staying under one."""
    rows = [_cpu(0, None, None, rooms=3), _cpu(10, None, None, rooms=4)]
    agg = await _aggregate(db, rows, 10)
    assert getattr(agg, f"peak_{peak}_utilisation") is None
    readings = getattr(agg, f"{peak}_readings")
    if peak == "cpu":
        [reading] = readings
        assert reading.utilisation is None
        assert reading.unmeasured_reason
    [result] = gate(readings)
    assert result.status == gates.UNKNOWN


async def test_the_worst_node_governs_rather_than_the_average(
    db: AsyncSession,
) -> None:
    """One node breaching while three idle is a breach, not a healthy mean."""
    rows = [
        _sample(s, node, memory_total_bytes=1000, memory_available_bytes=avail)
        for node, avail in [
            ("sfu-1", 900),
            ("sfu-2", 900),
            ("sfu-3", 900),
            ("sfu-9", 50),
        ]
        for s in (0, 10)
    ]
    agg = await _aggregate(db, rows, 10)
    assert agg.nodes_seen == 4
    # The average across nodes is 0.2375, under the 0.75 ceiling.
    assert agg.peak_memory_utilisation == pytest.approx(0.95)
    assert agg.peak_memory_utilisation > gates.MAX_NODE_MEMORY_UTILISATION


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end"), [(None, T0 + 40 * SEC), (T0, None)], ids=["no-start", "no-end"]
)
async def test_a_test_with_no_window_correlates_to_nothing(
    db: AsyncSession, start, end
) -> None:
    """Not an empty aggregate. Inventing a window from whichever end was
    recorded would attribute an arbitrary span of fleet activity to this test."""
    await insert_samples(db, _BUSY_CPU)
    agg = await aggregation.aggregate_test_window(
        db, started_at_ms=start, ended_at_ms=end
    )
    assert agg is None


async def test_samples_outside_the_padded_window_are_not_counted(
    db: AsyncSession,
) -> None:
    await insert_samples(db, _BUSY_CPU)
    agg = await aggregation.aggregate_test_window(
        db, started_at_ms=T0 + 3600 * SEC, ended_at_ms=T0 + 3700 * SEC
    )
    assert agg is not None
    assert agg.node_samples_in_window == 0
    assert agg.peak_cpu_utilisation is None


async def test_the_window_records_its_bounds_and_sample_count(
    db: AsyncSession,
) -> None:
    """So a padded correlation is never presented as exact, nor a peak oversold."""
    agg = await _aggregate(db, _BUSY_CPU, 40, pad_ms=5000)
    assert agg.window.requested_start_ms == T0
    assert agg.window.start_ms == T0 - 5000
    assert agg.window.pad_ms == 5000
    assert agg.node_samples_in_window == 5


# --------------------------------------------------------------------------
# How a peak may be described, and the contract this rests on
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("samples", "label"),
    [
        (0, "not_measured"),
        (4, "max of 4"),  # four points do not describe a distribution
        (
            gates.MIN_PERCENTILE_SAMPLES - 1,
            f"max of {gates.MIN_PERCENTILE_SAMPLES - 1}",
        ),
        (gates.MIN_PERCENTILE_SAMPLES, "p95"),
    ],
)
def test_a_peak_below_the_percentile_floor_is_labelled_as_a_max(
    samples: int, label: str
) -> None:
    assert aggregation.peak_label(samples) == label


def test_the_contract_this_module_rests_on() -> None:
    """The percentile floor is one number, not two, and every column named here
    is of the kind it is treated as. Memory is never diffed."""
    assert gates.MIN_PERCENTILE_SAMPLES == correlation.MIN_PERCENTILE_SAMPLES
    assert aggregation.CPU_CAPACITY_COLUMN in COUNTER_COLUMNS
    assert aggregation.CPU_IDLE_COLUMN in COUNTER_COLUMNS
    assert aggregation.MEMORY_AVAILABLE_COLUMN in GAUGE_COLUMNS
    assert aggregation.MEMORY_TOTAL_COLUMN in GAUGE_COLUMNS
    assert aggregation.MEMORY_AVAILABLE_COLUMN not in COUNTER_COLUMNS


# --------------------------------------------------------------------------
# Return to baseline: one sample must not decide a verdict
# --------------------------------------------------------------------------
#
# The settle side used to be the single newest row in the table, so a report
# generated straight after a run read the scrape that caught the collector host
# running the report generator, and failed that node for the tool's own
# footprint. The trace below is a real one: flat at 1312 to 1344 descriptors
# and about 1.02 GB, with one sample carrying 1408 and 1.118 GB.
#
# The baseline selection used to be "everything before the run", ascending,
# capped at 5,000 rows: the OLDEST retained history. A real hour-long run with a
# true 1.10x ratio reported 0.51x from a baseline 21 hours earlier.

_TOLERANCE = 1.10
_BASELINE_FD = 1248
_BASELINE_MEM = 958_685_000
_MEM_TOTAL = 4_000_000_000
_RUN_S = 600  # ten minutes, so the settle check has real time to read
_GB = 1_000_000_000
_TRUE_BASELINE = 0.7405 * _GB
_TRUE_SETTLE = 0.8147 * _GB
_STALE_BASELINE = 1.9362 * _GB  # 21 hours before the run, and irrelevant to it

# The window is padded 15s each side. The gate reads the 5 minutes before the
# run and 5 to 10 minutes after it, so sample timing is under test here: a
# settle sample a minute after teardown is not a post-settle reading.
_BASELINE_AT = -300  # seconds, first of eight at 15s -> -300 to -195
_SETTLE_AT = _RUN_S + 315  # 5m15s past the padded end

# (fd, memory_used_bytes) per 15s scrape after the run.
_SETTLE_TRACE = [
    (1344, 999_000_000),
    (1312, 1_040_000_000),
    (1408, 1_118_040_000),  # the report generator's own process, and nothing else
    (1312, 1_040_000_000),
    (1312, 1_027_000_000),
    (1344, 1_051_000_000),
    (1344, 1_008_000_000),
]
_LEAKED = [(1500, 1_400_000_000)] * 7
# Four of seven elevated: a median is not a majority vote a leak must win.
_CLIMBING = [(1312, _GB), (1312, _GB), (1344, 1_020_000_000)] + [
    (1600, 1_500_000_000)
] * 4


def _scrapes(start_s: int, trace) -> list[NodeSampleInput]:
    """One 15s scrape per (fd, memory_used) pair."""
    return [
        _sample(
            start_s + i * 15,
            memory_total_bytes=float(_MEM_TOTAL),
            memory_available_bytes=float(_MEM_TOTAL - used),
            filefd_allocated=None if fd is None else float(fd),
        )
        for i, (fd, used) in enumerate(trace)
    ]


# Samples DURING the run. A node is correlated to a test by having been sampled
# inside its window, so without these there is no node and no gate. Their
# values do not feed this gate.
_UNDER_LOAD = [
    s
    for i in range(_RUN_S // 60 + 1)
    for s in _scrapes(i * 60, [(1400, 1_800_000_000)])
]
_FLAT_BASELINE = _scrapes(_BASELINE_AT, [(_BASELINE_FD, _BASELINE_MEM)] * 8)


async def _baseline_gates(db: AsyncSession, *rows: list, load: bool = True) -> dict:
    await insert_samples(db, sum(rows, _UNDER_LOAD if load else []))
    agg = await aggregation.aggregate_test_window(
        db, started_at_ms=T0, ended_at_ms=T0 + _RUN_S * SEC
    )
    assert agg is not None
    return {
        g.subject: g
        for g in gates.return_to_baseline_gates(
            agg.baseline_comparisons, tolerance=_TOLERANCE
        )
    }


async def test_one_spike_in_a_flat_settle_window_is_not_a_failure(
    db: AsyncSession,
) -> None:
    """The reporting tool measuring itself. Under the old single-sample reading
    both of these were FAIL, at 1.13x and 1.17x.

    The detail says what the number is a median of, so nobody goes hunting for
    a scrape carrying it, and names the instant its baseline came from.
    """
    results = await _baseline_gates(
        db, _FLAT_BASELINE, _scrapes(_SETTLE_AT, _SETTLE_TRACE)
    )
    fd = results["sfu-1/filefd_allocated"]
    assert fd.status == gates.PASS
    assert fd.value == pytest.approx(1344 / _BASELINE_FD, rel=1e-3)
    mem = results["sfu-1/memory_used_bytes"]
    assert mem.status == gates.PASS
    assert mem.value == pytest.approx(1_040_000_000 / _BASELINE_MEM, rel=1e-3)

    last_baseline = datetime.fromtimestamp(
        (T0 + (_BASELINE_AT + 7 * 15) * SEC) / 1000, tz=UTC
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    for fragment in ("median of 7 samples", "median of 8 samples", last_baseline):
        assert fragment in fd.detail


@pytest.mark.parametrize("settle", [_LEAKED, _CLIMBING], ids=["sustained", "midway"])
async def test_a_leak_still_fails(db: AsyncSession, settle) -> None:
    """The median must not be a way to pass a leak that went up and stayed up."""
    results = await _baseline_gates(db, _FLAT_BASELINE, _scrapes(_SETTLE_AT, settle))
    assert results["sfu-1/filefd_allocated"].status == gates.FAIL
    assert results["sfu-1/memory_used_bytes"].status == gates.FAIL


async def test_a_spike_in_the_baseline_cannot_hide_a_leak(db: AsyncSession) -> None:
    """A spike on the BEFORE side inflates the denominator and turns a leak into
    a clean bill of health. It sits on the LAST sample before the run, the one
    the single-sample reading took, so the test proves something."""
    spiked = _FLAT_BASELINE[:7] + _scrapes(
        _BASELINE_AT + 7 * 15, [(1600, _BASELINE_MEM)]
    )
    settled = _scrapes(_SETTLE_AT, [(1500, _BASELINE_MEM)] * 7)
    fd = (await _baseline_gates(db, spiked, settled))["sfu-1/filefd_allocated"]
    assert fd.status == gates.FAIL
    # 1500/1248, not 1500/1600: the spike did not become the baseline.
    assert fd.value == pytest.approx(1500 / _BASELINE_FD, rel=1e-3)


def test_a_single_sample_says_so_rather_than_calling_itself_a_median() -> None:
    [result] = gates.return_to_baseline_gates(
        [
            gates.BaselineComparison(
                node="sfu-1",
                metric="filefd_allocated",
                baseline=1000.0,
                post_settle=1010.0,
                baseline_at_ms=T0,
                post_settle_at_ms=T0 + 900_000,
                baseline_samples=1,
                post_settle_samples=1,
            )
        ],
        tolerance=_TOLERANCE,
    )
    assert result.status == gates.PASS
    assert "a single sample" in result.detail


async def test_an_old_sample_cannot_become_the_baseline(db: AsyncSession) -> None:
    """A true 1.10x must not report as 0.51x, and retention must not move it.

    Which rows survive is ``workers.node_sample_max_age_days``, so reading the
    oldest of them made the retention setting a silent verdict-changer. The
    stale block is large on purpose: under the old ascending-with-a-limit
    selection the oldest rows are exactly the ones that win.
    """
    near = _scrapes(_BASELINE_AT, [(None, _TRUE_BASELINE)] * 20)
    settled = _scrapes(_SETTLE_AT, [(None, _TRUE_SETTLE)] * 20)
    pruned = (await _baseline_gates(db, near, settled))["sfu-1/memory_used_bytes"]
    assert pruned.value == pytest.approx(_TRUE_SETTLE / _TRUE_BASELINE, rel=1e-3)

    # Now the same run on a table that kept an hour of history 21 hours back.
    stale = _scrapes(-21 * 3600, [(None, _STALE_BASELINE)] * 240)
    retained = (await _baseline_gates(db, stale, load=False))["sfu-1/memory_used_bytes"]
    assert retained.value == pytest.approx(pruned.value, rel=1e-9)
    assert retained.status == pruned.status


async def test_a_report_generated_before_the_settle_window_says_unknown(
    db: AsyncSession,
) -> None:
    """Not a PASS. Nothing has settled a minute after teardown, so there is no
    return to report either way, which is what ``MIN_SETTLE_MS`` intended."""
    near = _scrapes(_BASELINE_AT, [(None, _TRUE_BASELINE)] * 20)
    draining = _scrapes(_RUN_S + 60, [(None, _TRUE_SETTLE)] * 4)
    result = (await _baseline_gates(db, near, draining))["sfu-1/memory_used_bytes"]
    assert result.status == gates.UNKNOWN
