"""The path where a run PASSES, which nothing had ever exercised.

Every real artifact in this repository is of a failed run, so the report
generator, the gates and the capacity derivation had only ever been run against
failures. Before test day is a bad time to find out that a passing run renders
wrong.

**Two runs, because one run cannot be both.** ``derive_calls_per_node`` refuses
a ramp that never saturated, and ``node_cpu_gate`` fails any step at or above
the 70% ceiling. So the step that makes a capacity figure derivable is the same
step that fails the CPU gate. An operator needs both runs and the report is
right to describe them differently.

Both fixtures are SYNTHETIC and say so in their own PROVENANCE.md. They are
shaped like the one real capture: a stat file named ``gossipper_<pid>_stats.log``
and no ``summary.json``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from voicegateway.cli._app import app
from voicegateway.livekit_diag import gates
from voicegateway.loadtest import judge
from voicegateway.repository import node_samples_repository as node_samples
from voicegateway.services.storage_service import StorageService

runner = CliRunner()

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "loadtest"
ACCEPTANCE = FIXTURES / "acceptance-500"
SATURATION = FIXTURES / "saturation-ramp"

NODE = "sfu-1"
SOURCE = "node-exporter"
CORES = 4.0
TOTAL_BYTES = 16 * 2**30

# Throughput, as the cumulative byte counters a node_exporter publishes. Read as
# a RATE, so the value has to be monotonic in absolute time: a counter that goes
# backwards is a reset, which read_counter_rate reports as "cannot say".
COUNTER_ORIGIN_S = 1_785_661_200
RX_BYTES_PER_SECOND = 50_000_000.0
TX_BYTES_PER_SECOND = 60_000_000.0

# The instance type's PUBLISHED baseline, in bits per second, both directions.
# Declared rather than scraped: the ENA driver reports no link speed. 400 Mbps in
# and 480 Mbps out against 3.125 Gbps leaves 87% and 85% headroom.
DECLARED_BASELINE_BPS = 3_125_000_000.0

# The whole acceptance run, in one window comfortably inside both ceilings.
ONE_WINDOW = [(1_785_661_201_000, 1_785_661_320_000, 0.66, 0.61)]
MEASURABLE = ("call_establishment", "node_cpu", "node_memory")


async def _seed(db_path: Path, windows: list[tuple[int, int, float, float]]) -> None:
    """Node samples describing a node at ``cpu`` busy and ``memory`` used.

    CPU is a pair of counters diffed at read time, so the fraction is expressed
    as the RATE the idle counter accrues at. Writing a utilisation directly
    would test nothing, because nothing in production stores one.
    """
    rows = []
    for start_ms, end_ms, cpu, memory in windows:
        for elapsed in range(0, (end_ms - start_ms) // 1000 + 1, 10):
            at_ms = start_ms + elapsed * 1000
            since_origin = at_ms // 1000 - COUNTER_ORIGIN_S
            values = {
                "cpu_seconds_total": 1000.0 + CORES * elapsed,
                "cpu_idle_seconds_total": 800.0 + CORES * (1.0 - cpu) * elapsed,
                "memory_total_bytes": float(TOTAL_BYTES),
                "memory_available_bytes": float(int(TOTAL_BYTES * (1.0 - memory))),
                # Descriptors, media ports and network are MEASURED here, so what
                # is left UNKNOWN is only what nothing can scrape.
                "process_open_fds": 1200.0,
                "process_max_fds": 524287.0,
                "media_ports_in_use": 1200.0,
                "media_ports_total": 10001.0,
                "network_receive_bytes_total": RX_BYTES_PER_SECOND * since_origin,
                "network_transmit_bytes_total": TX_BYTES_PER_SECOND * since_origin,
            }
            # The hypervisor's shaping counters, flat at zero rather than
            # absent: a zero DELTA is a measured clean window.
            for kind in ("bw_in", "bw_out", "pps", "conntrack", "linklocal"):
                values[f"ethtool_{kind}_allowance_exceeded"] = 0.0
            rows.append(
                node_samples.NodeSampleInput(
                    node=NODE, source=SOURCE, at_ms=at_ms, outcome="ok", values=values
                )
            )
    storage = StorageService(db_path=str(db_path))
    await storage._ensure_initialized()
    async with storage._conn.session() as db:
        await node_samples.insert_samples(db, rows)
    await storage.aclose()


def _run(
    tmp_path: Path, fixture: Path, windows, *, plan=None, waive=None, baseline=False
):
    """Seed node samples, import, report. Returns the payload and the exits.

    ``baseline`` declares the per-node published link baseline, the only way a
    bandwidth headroom figure can exist.
    """
    db = tmp_path / "throwaway.db"
    config = tmp_path / "voicegw.yaml"
    config.write_text(
        yaml.dump({"cost_tracking": {"enabled": True, "db_path": str(db)}})
    )
    # Seeded BEFORE the import, because correlation happens at import time.
    asyncio.run(_seed(db, windows))

    argv = ["loadtest", "import", str(fixture), "--captured", "--config", str(config)]
    if plan is not None:
        argv += ["--plan", str(plan)]
    if baseline:
        path = tmp_path / "network-baseline.json"
        bps = DECLARED_BASELINE_BPS
        path.write_text(json.dumps({NODE: {"in_bps": bps, "out_bps": bps}}))
        argv += ["--network-baseline", str(path)]
    imported = runner.invoke(app, argv)

    out = tmp_path / "out"
    report_argv = ["loadtest", "report", "--acceptance", fixture.name]
    report_argv += ["--config", str(config), "--out", str(out)]
    if waive is not None:
        report_argv += ["--waive", str(waive)]
    exported = runner.invoke(app, report_argv)
    reports = sorted(out.glob("*.json")) if out.is_dir() else []
    payload = json.loads(reports[0].read_text()) if reports else None
    return {"imported": imported, "exported": exported, "payload": payload}


def _gates(payload, name: str):
    return [g for g in payload["gates"] if g["gate"] == name]


def test_the_fixture_is_shaped_like_a_real_capture() -> None:
    """Guards the premise. A summary.json here would test a different path."""
    names = {p.name for p in ACCEPTANCE.iterdir()}
    assert "summary.json" not in names
    assert any(n.startswith("gossipper_") and n.endswith("_stats.log") for n in names)


# --------------------------------------------------------------------------
# The acceptance run: everything measurable passes, and it still is not PASS
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def acceptance(tmp_path_factory):
    return _run(
        tmp_path_factory.mktemp("acceptance"), ACCEPTANCE, ONE_WINDOW, baseline=True
    )


@pytest.mark.parametrize(
    ("gate", "expected"),
    [("call_establishment", 0.999), ("node_cpu", 0.66), ("node_memory", 0.61)],
)
def test_every_measurable_gate_passes_with_its_number(
    acceptance, gate, expected
) -> None:
    assert acceptance["imported"].exit_code == 0, acceptance["imported"].output
    [row] = _gates(acceptance["payload"], gate)
    assert row["status"] == gates.PASS, row["detail"]
    assert row["value"] == pytest.approx(expected, abs=0.01)


def test_the_verdict_is_still_not_pass_and_here_is_why(acceptance) -> None:
    """THE FINDING. A perfect 500-concurrent run reports UNKNOWN and exits non-zero.

    RTP ports and network are measured now and green. What is left is ``pps``,
    which is permanent (no denominator is published by anyone), two
    dependencies nobody configured, and four lifecycle criteria whose columns
    this fixture's samples do not carry.
    """
    payload = acceptance["payload"]
    assert payload["verdict"]["status"] == gates.UNKNOWN
    assert acceptance["exported"].exit_code != 0
    unknown = [g for g in payload["gates"] if g["status"] == gates.UNKNOWN]
    assert {g["subject"].split("/")[-1] for g in unknown} == {
        gates.HEADROOM_PPS,
        "redis",
        "health_endpoint",
        "node-exporter",
        "memory_used_bytes",
        "filefd_allocated",
        "sockstat_udp_inuse",
    }
    # The resources that left that set PASS, each carrying its number. Ingress
    # and egress are separate credit buckets, so separate rows with different
    # values: 400 Mbps in and 480 Mbps out against the same 3.125 Gbps.
    measured = {
        g["subject"]: g["value"]
        for g in _gates(payload, gates.HEADROOM_GATE)
        if g["status"] == gates.PASS
    }
    assert measured == {
        f"{NODE}/{SOURCE}/file_descriptors": pytest.approx(0.9977, abs=1e-4),
        f"{NODE}/{SOURCE}/{gates.HEADROOM_RTP_PORTS}": pytest.approx(0.88, abs=1e-2),
        f"{NODE}/{gates.HEADROOM_NETWORK_IN}": pytest.approx(0.872),
        f"{NODE}/{gates.HEADROOM_NETWORK_OUT}": pytest.approx(0.8464),
    }
    assert min(measured.values()) >= gates.MIN_HEADROOM_FRACTION
    # The hypervisor never throttled this node, measured as a zero DELTA.
    [allowance] = _gates(payload, gates.NETWORK_ALLOWANCE_GATE)
    assert (allowance["status"], allowance["value"]) == (gates.PASS, 0.0)
    # UNKNOWN outranks PASS, which is what makes the remainder decisive.
    assert gates.worst_status([gates.PASS, gates.UNKNOWN]) == gates.UNKNOWN

    # And the run that passes has no capacity figure: it never exceeded the
    # ceiling, so its capacity is a floor and the derivation refuses.
    assert payload["capacity"]["calls_per_node"] is None
    assert "never saturated" in payload["capacity"]["reason"]


def test_pps_is_the_one_resource_nothing_will_ever_measure() -> None:
    """Structural, so it holds beyond this fixture.

    The reason says the denominator is UNPUBLISHED rather than uncollected: a
    reason phrased as "nothing scrapes it" would read as work somebody could do.
    """
    excluded = judge.excluded_headroom_resources()
    assert sorted(excluded) == [gates.HEADROOM_PPS]
    assert "no per-instance-type PPS allowance is published" in excluded["pps"]
    # The others are measurable, and a caller that scraped none of them still
    # gets them filed as unmeasured rather than silently dropped.
    readings = gates.unscraped_headroom_readings(NODE)
    assert [r.resource for r in readings] == [
        gates.HEADROOM_RTP_PORTS,
        gates.HEADROOM_NETWORK_IN,
        gates.HEADROOM_NETWORK_OUT,
    ]
    assert all(r.used is None and r.limit is None for r in readings)


# --------------------------------------------------------------------------
# The sizing run: a capacity table, at the cost of the CPU gate
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def saturation(tmp_path_factory):
    # One 59s window per step, 390s apart, CPU climbing past the ceiling at the
    # last. The fixture's steps are six minutes, because a step shorter than
    # capacity.MIN_STEADY_STATE_S records arrivals, not occupancy.
    base = 1_785_661_201_000
    windows = [
        (base + i * 390_000, base + i * 390_000 + 59_000, cpu, 0.40 + i * 0.05)
        for i, cpu in enumerate((0.31, 0.48, 0.66, 0.79))
    ]
    return _run(
        tmp_path_factory.mktemp("saturation"),
        SATURATION,
        windows,
        plan=SATURATION / "plan.json",
    )


def test_the_capacity_figure_is_derived(saturation) -> None:
    """200: the highest concurrency sustained at or under 70% CPU.

    500 at C=200: ceil(500 / (0.85 x 200)) + 1 = ceil(2.94) + 1 = 4.
    """
    payload = saturation["payload"]
    assert saturation["imported"].exit_code == 0, saturation["imported"].output
    # A plateaued ramp would prove the refusal rather than the derivation, and
    # the targets are the operator's, since no artifact records them.
    tests = payload["tests"]
    assert [t["peak_concurrency"] for t in tests] == [100, 150, 200, 250]
    assert [t["target_concurrency"] for t in tests] == [100, 150, 200, 250]
    assert payload["capacity"]["calls_per_node"] == 200
    tiers = {t["target_concurrency"]: t for t in payload["capacity"]["tiers"]}
    assert sorted(tiers) == [100, 150, 300, 500]
    for tier in tiers.values():
        assert tier["spare_nodes"] == 1
        assert tier["nodes"] == tier["nodes_for_load"] + 1
    assert tiers[500]["usable_per_node"] == pytest.approx(170.0)
    assert tiers[500]["nodes"] == 4


def test_the_sizing_run_fails_its_cpu_gate(saturation) -> None:
    """The cost of the figure, visible rather than smoothed away.

    Only the 79% step fails; the steps under the ceiling still pass.
    """
    statuses = [g["status"] for g in _gates(saturation["payload"], "node_cpu")]
    assert sorted(statuses) == sorted([gates.PASS] * 3 + [gates.FAIL])
    assert saturation["payload"]["verdict"]["status"] == gates.FAIL
    assert saturation["exported"].exit_code != 0


# --------------------------------------------------------------------------
# The best achievable outcome: WAIVED, in writing
# --------------------------------------------------------------------------

# Every criterion the acceptance run leaves unmeasured. A key matching no gate is
# REFUSED, so this map is also the assertion that no key here is stale: RTP
# ports and network are measured and PASS, and need no waiver.
WAIVERS = {
    "resource_headroom/fleet/pps": "no published denominator on any instance type",
    "sustained_health/fleet/redis": "single-node deployment with no Redis",
    "sustained_health/fleet/health_endpoint": "no health endpoint configured",
    "process_lifecycle/sfu-1/node-exporter": "no process start time or OOM counter",
    "return_to_baseline/fleet/memory_used_bytes": "no idle samples outside the window",
    "return_to_baseline/fleet/filefd_allocated": "no idle samples outside the window",
    "return_to_baseline/fleet/sockstat_udp_inuse": "no idle samples outside window",
}


@pytest.mark.parametrize(
    ("waivers", "exit_code", "verdict"),
    [
        (WAIVERS, 1, gates.WAIVED),
        # A typo would leave the gate unwaived and the waiver unrecorded.
        ({"resource_headroom/fleet/rtp_port": "typo"}, 2, None),
    ],
)
def test_a_waiver_is_recorded_never_a_pass(
    tmp_path, waivers, exit_code, verdict
) -> None:
    """WAIVED outranks PASS, and a pipeline going green on it has dropped the
    requirement, so a waived run still exits non-zero."""
    waiver = tmp_path / "waivers.json"
    waiver.write_text(json.dumps(waivers))
    result = _run(tmp_path, ACCEPTANCE, ONE_WINDOW, waive=waiver, baseline=True)
    assert result["exported"].exit_code == exit_code, result["exported"].output
    payload = result["payload"]
    if verdict is None:
        assert payload is None, "a refused waiver must not write a report"
        return
    assert payload["verdict"]["status"] == verdict
    waived = [g for g in payload["gates"] if g["status"] == gates.WAIVED]
    assert len(waived) == len(waivers)
    for gate in waived:
        # A waiver a reviewer cannot read is the silent pass this exists to
        # prevent, so its reason is in the rendered detail.
        assert gate["waiver_reason"]
        assert gate["waiver_reason"] in gate["detail"]
    # Non-vacuous: waiving must not touch the measured gates.
    measurable = [g for g in payload["gates"] if g["gate"] in MEASURABLE]
    assert {g["status"] for g in measurable} == {gates.PASS}
