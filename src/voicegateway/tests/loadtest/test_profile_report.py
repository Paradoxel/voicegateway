"""The profile view: measurements and reference bands, and no verdict at all.

VoiceGateway profiles. Its default report shows what it measured and the range
each figure is usually read against, and stops. A human reads and decides.

The gate machinery is not deleted, it moves behind ``--acceptance``, because an
engagement that contracted 99.5% establishment does need a pass or a fail and a
pipeline needs an exit code. Two views over ONE set of measurements, so they can
never disagree about a number.

**Three properties this file exists to hold.**

*No judgement leaks into the default view.* Not a verdict block, not a status
tag, not a coloured bar, not an exit code, and not a line on the console.

*A quantity nobody can measure does not appear at all.* PPS headroom has no
published denominator, so it is dropped, not reported as "unknown".

*A quantity that IS measurable but was not collected is still named*, counted
in the HTML and explained per entry in the JSON.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from voicegateway.cli._app import app
from voicegateway.livekit_diag import gates, run_report
from voicegateway.loadtest import judge

runner = CliRunner()

CAPTURE = (
    Path(__file__).resolve().parent.parent / "fixtures" / "loadtest" / "capture-01"
)

# Imported from the suites that own them, so self-containment cannot drift here
# while this file looks like it enforces it.
from voicegateway.tests.cli.test_livekit_report_cli import (  # noqa: E402
    _EXTERNAL_MARKERS as CLI_MARKERS,
)
from voicegateway.tests.server.test_diagnostics_report import (  # noqa: E402
    _EXTERNAL_MARKERS as SERVER_MARKERS,
)

_STATUS_COLOURS = ("#1f7a44", "#a32020", "#a86a00", "#6a4ca8")


@pytest.fixture(scope="module")
def profiled(tmp_path_factory):
    """The real capture (a FAILED run), exported through the DEFAULT path."""
    tmp = tmp_path_factory.mktemp("profile")
    monkey = pytest.MonkeyPatch()
    monkey.delenv("VOICEGW_DB_PATH", raising=False)
    config = tmp / "voicegw.yaml"
    config.write_text(
        yaml.dump(
            {"cost_tracking": {"enabled": True, "db_path": str(tmp / "throwaway.db")}}
        )
    )
    cli = ["--config", str(config)]
    imported = runner.invoke(
        app, ["loadtest", "import", str(CAPTURE), "--captured", *cli]
    )
    assert imported.exit_code == 0
    out = tmp / "out"
    result = runner.invoke(
        app, ["loadtest", "report", "capture-01", *cli, "--out", str(out)]
    )
    html = next(p for p in out.iterdir() if p.suffix == ".html")
    payload = json.loads(
        next(p for p in out.iterdir() if p.suffix == ".json").read_text()
    )
    monkey.undo()
    return {
        "result": result,
        "html": html.read_text(),
        "name": html.name,
        "payload": payload,
    }


def _section(html: str, heading: str) -> str:
    start = html.index(heading)
    return html[start : html.index("<h2", start + 1)]


def _glance(payload: dict) -> dict:
    rows = run_report._glance_rows(payload, run_report._profile_rows(payload)[0])
    return {r["what"]: r for r in rows}


def _fact(subject: str, value: float, *, reading: str = "ok", key="resource_trend"):
    """A glance row as _profile_glance_rows builds them."""
    return {
        "key": key,
        "gate": key,
        "name": "n",
        "meaning": "m",
        "subject": subject,
        "step": None,
        "value": value,
        "threshold": 1.1,
        "detail": "",
        "reading": reading,
    }


# --------------------------------------------------------------------------
# No judgement, and nothing unmeasurable, anywhere
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "marker",
    [
        'class="verdict',
        'class="tag',
        "<h2>Gates</h2>",
        "Not in scope for this report",
        gates.PASS,
        gates.FAIL,
        gates.UNKNOWN,
        gates.WAIVED,
        "fleet/pps",
        *sorted(set(CLI_MARKERS) | set(SERVER_MARKERS)),
    ],
)
def test_nothing_judged_unmeasurable_or_external_reaches_the_document(
    profiled, marker
) -> None:
    assert marker not in profiled["html"], marker
    assert "pps headroom" not in profiled["html"].lower()


def test_the_cli_exits_zero_and_names_no_verdict_on_a_failing_run(profiled) -> None:
    """--acceptance exits non-zero on this capture; the profile makes no claim."""
    result = profiled["result"]
    assert result.exit_code == 0, result.output
    assert "Verdict" not in result.output
    assert "Profiled" in result.output


def test_the_measurements_are_present_and_the_glance_comes_first(profiled) -> None:
    html = profiled["html"]
    for section in ("Per test", "Measurements", "How to read these numbers"):
        assert html.index("At a glance") < html.index(section), section
    # The pps allowance COUNTER is measured, unlike pps headroom.
    assert "allowance" in html.lower()
    assert profiled["name"] == run_report.profile_filename("capture-01")
    assert profiled["name"] != run_report.load_report_filename("capture-01")
    # The JSON is the same payload either view: gates and verdict still there.
    assert profiled["payload"]["gates"]
    assert profiled["payload"]["verdict"]["status"]


def test_the_unmeasurable_set_is_the_judge_modules(profiled) -> None:
    assert run_report.PROFILE_PERMANENTLY_UNMEASURABLE == frozenset(
        judge.PERMANENT_HEADROOM_EXCLUSIONS
    )


# --------------------------------------------------------------------------
# What was not collected is counted in the HTML and explained in the JSON
# --------------------------------------------------------------------------


def test_uncollected_is_a_count_in_prose_and_a_list_in_the_json(profiled) -> None:
    entries = profiled["payload"]["not_collected"]
    assert entries
    section = _section(profiled["html"], "Not collected in this run")
    assert str(len(entries)) in section
    for tag in ("<table", "<tr", "<li"):
        assert tag not in section, tag
    for entry in entries:
        for field in ("measurement", "cause", "change", "recorded"):
            assert entry[field], field
    # A permanent gap is a fact about the world, not a thing to fix.
    assert "pps" not in " ".join(str(e["subject"]) for e in entries)


def test_uncollected_is_null_when_nothing_was_gated_never_empty() -> None:
    assert run_report.not_collected_entries(None) is None
    assert run_report.not_collected_entries([]) == []


# --------------------------------------------------------------------------
# At a glance: one table, every line carrying a number
# --------------------------------------------------------------------------


def test_every_glance_line_carries_a_measured_number(profiled) -> None:
    section = _section(profiled["html"], "At a glance")
    assert "<tr>" in section
    assert "at least 99.5%" in section
    for placeholder in ("not measured", "not recorded", "unknown", "&mdash;"):
        assert placeholder not in section.lower(), placeholder
    readings = set(re.findall(r"<td class='reading'>([^<]*)</td>", profiled["html"]))
    assert readings and readings <= {"ok", "over", ""}


@pytest.mark.parametrize("selector", [".reading", ".band"])
def test_neither_the_reading_nor_the_band_is_styled_like_a_gate(selector) -> None:
    css = run_report._PROFILE_CSS
    rule = css[css.index(selector) :].split("}")[0]
    for forbidden in _STATUS_COLOURS:
        assert forbidden not in rule, forbidden
    if selector == ".reading":
        for chip in ("background", "border", "border-radius", "font-weight"):
            assert chip not in rule, chip


def test_the_reading_comes_from_the_gate_not_a_second_comparison(profiled) -> None:
    """The glance can never say ok where a gate said no."""
    over_keys = {
        run_report._profile_key(g)
        for g in profiled["payload"]["gates"]
        if g["status"] == gates.FAIL and g.get("value") is not None
    }
    assert over_keys, "the fixture is wrong: nothing failed"
    rows = _glance(profiled["payload"])
    names = [run_report._GLANCE_NAMES.get(k) for k in over_keys]
    checked = [n for n in names if n in rows]
    assert checked, f"no over-reading key reached the glance table: {over_keys}"
    for name in checked:
        assert rows[name]["reading"] == "over", name
    # A waiver removes the comparison, so it gets no reading at all.
    assert run_report._GLANCE_READINGS == {gates.PASS: "ok", gates.FAIL: "over"}


def _gates(*specs, tests=()) -> dict:
    """A payload from (gate, status, subject, value, threshold) tuples."""
    keys = ("gate", "status", "subject", "value", "threshold")
    return {
        "tests": list(tests),
        "gates": [dict(zip(keys, s, strict=True)) for s in specs],
    }


CPU, TREND, PASS, FAIL = (
    gates.NODE_CPU_GATE,
    gates.RESOURCE_TREND_GATE,
    gates.PASS,
    gates.FAIL,
)

#: Measured in both directions, which the capture (all calls timed out) is not.
_TWO_SIDED = _gates(
    (CPU, PASS, "sip-1", 0.66, gates.MAX_NODE_CPU_UTILISATION),
    tests=[
        {"name": "soak", "rtp_packets_sent": 111_682, "rtp_packets_received": 110_698},
        {"name": "ramp", "rtp_packets_sent": 1_498, "rtp_packets_received": 1_400},
    ],
)


@pytest.mark.parametrize(
    ("payload", "what", "field", "expected", "reading"),
    [
        # Worst node quoted, never an average.
        (
            _gates((CPU, PASS, "quiet", 0.1, 0.7), (CPU, FAIL, "busy", 0.9, 0.7)),
            "Peak CPU, worst node",
            "why",
            "busy",
            "over",
        ),
        # Drift is a count because its units differ; no byte figure quoted.
        (
            _gates(
                (TREND, FAIL, "n/memory_used_bytes", 45_800_000.0, 0.01),
                (TREND, PASS, "n/sockstat_udp_inuse", 4.0, 0.01),
            ),
            "Resource drift",
            "value",
            "1 of 2",
            "over",
        ),
        # The worse of the two media ratios, naming its test, no invented ref.
        (_TWO_SIDED, "Two-way media", "value", "0.935", ""),
        (_TWO_SIDED, "Two-way media", "why", "ramp", ""),
        (_TWO_SIDED, "Peak CPU, worst node", "reference", "at most 70%", "ok"),
    ],
)
def test_glance_rows(payload, what, field, expected, reading) -> None:
    rows = _glance(payload)
    row = next(r for name, r in rows.items() if name.startswith(what))
    assert expected in row[field]
    assert row["reading"] == reading
    if what == "Two-way media":
        assert row["reference"] == ""


def test_a_measurement_nobody_ordered_still_reaches_the_table() -> None:
    assert gates.SFU_CAPACITY_GATE not in run_report._GLANCE_ORDER
    assert len(_glance(_gates((gates.SFU_CAPACITY_GATE, PASS, "sfu", 1.0, 1.0)))) == 1


def test_the_media_row_escapes_the_test_name_and_skips_a_zero_denominator() -> None:
    def media(name, sent, received):
        test = {
            "name": name,
            "rtp_packets_sent": sent,
            "rtp_packets_received": received,
        }
        return run_report._glance_media_row({"tests": [test]})

    why = media("<script>alert(1)</script>", 10, 9)["why"]
    assert "<script>" not in why and "&lt;script&gt;" in why
    assert media("t", 0, 0) is None


# --------------------------------------------------------------------------
# The reference band never becomes a verdict
# --------------------------------------------------------------------------


def test_the_band_renders_the_value_and_the_reference_separately() -> None:
    html = run_report._profile_band(0.838, 0.70)
    assert "83.8%" in html and "70.0%" in html


@pytest.mark.parametrize(
    ("gate", "value", "threshold", "banded"),
    [
        (gates.NODE_CPU_GATE, 0.838, 0.70, True),
        (gates.SUSTAINED_HEALTH_GATE, 2.0, 3.0, False),  # a count has no maximum
        (gates.RETURN_TO_BASELINE_GATE, 0.82, 1.10, False),  # a multiple, not 0..1
    ],
)
def test_a_band_is_only_drawn_where_the_denominator_is_real(
    gate, value, threshold, banded
) -> None:
    row = {"gate": gate, "value": value, "threshold": threshold}
    assert ("band-fill" in run_report._profile_reference(row)) is banded


# --------------------------------------------------------------------------
# Classification, so a new gate cannot vanish
# --------------------------------------------------------------------------


def test_every_load_gate_is_classified_once() -> None:
    assert run_report.PROFILE_UNCLASSIFIED_GATES == {
        gates.AGENTS_GATE,
        gates.LATENCY_GATE,
        gates.SFU_CAPACITY_GATE,
        gates.SFU_QUALITY_GATE,
    }
    keys = [k for group in run_report.PROFILE_GROUPS.values() for k in group]
    assert len(keys) == len(set(keys))


# --------------------------------------------------------------------------
# Per-call media counts reach the payload row the gate judged
# --------------------------------------------------------------------------


W, WO = "calls_answered_with_inbound", "calls_answered_without_inbound"


@pytest.mark.parametrize(
    ("test", "expected"),
    [
        ({"attempted_calls": 900, W: 899, WO: 1}, {W: 899, WO: 1}),
        ({"attempted_calls": 10}, {W: None, WO: None}),  # not counted is not zero
        ({W: 900, WO: 0}, {WO: 0}),  # a measured zero is the passing result
        ({W: "899", WO: "1"}, {W: 899, WO: 1}),  # persisted strings from a driver
        # The 24 hour soak: silent calls on a run whose establishment was 1.0.
        (
            {"attempted_calls": 28804, "succeeded_calls": 28804, W: 16606, WO: 12198},
            {"establishment_ratio": 1.0, WO: 12198},
        ),
    ],
)
def test_the_load_test_row_carries_the_counts(test, expected) -> None:
    row = run_report._load_test_row({"name": "g0", **test})
    assert {k: row[k] for k in expected} == expected


# ---------------------------------------------------------------------------
# One fact echoed per generator process is one fact
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rows", "subjects"),
    [
        ([_fact("sip-1", 0.5465968586387453)] * 12, ["sip-1"]),
        ([_fact("sip-1", v) for v in (0.31, 0.48, 0.55, 0.66)], ["sip-1"] * 4),
        (
            [_fact(f"sip-{n}", 0.55) for n in (1, 2, 3, 4)],
            ["sip-1", "sip-2", "sip-3", "sip-4"],
        ),
        (
            [_fact("a", 1.0), _fact("b", 2.0), _fact("a", 1.0), _fact("c", 3.0)],
            ["a", "b", "c"],
        ),
    ],
)
def test_distinct_facts(rows, subjects) -> None:
    assert [r["subject"] for r in run_report.distinct_facts(rows)] == subjects


def test_the_glance_counts_distinct_facts_not_echoes() -> None:
    cpu = [
        _fact(f"sip-{n}/livekit-sip", 0.42, key="node_cpu")
        for n in (1, 2, 3, 4)
        for _ in range(12)
    ]
    [row] = [
        r
        for r in run_report._glance_rows({"tests": []}, cpu)
        if r["what"] == run_report._GLANCE_NAMES["node_cpu"]
    ]
    assert "worst of 4 measured" in row["why"], row["why"]

    trend = [
        _fact(f"{s}/memory", 1.16 if over else 1.01, reading="over" if over else "ok")
        for s, over in (
            ("sip-1", True),
            ("sip-2", True),
            ("sip-3", False),
            ("sip-4", False),
            ("sfu-1", False),
        )
        for _ in range(12)
    ]
    [row] = [
        r
        for r in run_report._glance_rows({"tests": []}, trend)
        if r["what"] == run_report._GLANCE_NAMES[gates.RESOURCE_TREND_GATE]
    ]
    assert row["value"] == "2 of 5", row["value"]


@pytest.mark.parametrize(
    ("measured", "note"),
    [
        (
            [_fact(f"sip-{n}", 0.55) for n in (1, 2, 3, 4) for _ in range(12)],
            "These 48 rows state 4 distinct findings",
        ),
        ([_fact(f"sip-{n}", 0.1 * n) for n in (1, 2, 3, 4)], None),
    ],
)
def test_the_measurements_table_says_when_it_repeats_itself(measured, note) -> None:
    html = run_report._render_profile_measurements(measured)
    if note:
        assert note in html
    else:
        assert "distinct findings" not in html
