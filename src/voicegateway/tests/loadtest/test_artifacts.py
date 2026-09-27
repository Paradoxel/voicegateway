"""The load-generator artifact parser, and the four ways it can lie quietly.

Every trap here produces a PLAUSIBLE NUMBER rather than an error, which is why
each one gets a test that pins the right answer next to the wrong one it would
otherwise have produced:

1. Peak concurrency taken from ``summary.json`` reads 0 for a busy run, because
   the summary's ``active_calls`` is the end-of-run drain state.
2. Establishment scanned per-row over the CSV reads 0, because the first
   interval has calls in flight and none completed.
3. ``health.passed`` is the generator's verdict against its own threshold.
   Echoing it makes the gate layer a passthrough, and the passthrough is
   invisible because the numbers agree.
4. A cause the artifact omitted, rendered as 0, claims there were none.

The fixture values are inline rather than read from disk, so the wrong answer
each trap yields is visible in the same file as the assertion that rejects it.
They are hand-built from the generator's published schema documentation and
prove only that this parser is defensive, never that it matches a real artifact.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from voicegateway.loadtest import artifacts as art

# The 43-column -trace_stat header, in the documented order.
STAT_HEADER = (
    "timestamp,elapsed_ms,total_calls,success_calls,failed_calls,active_calls,"
    "success_ratio,calls_per_second,retransmits,timeouts,avg_call_ms,"
    "call_stddev_ms,avg_invite_ms,invite_stddev_ms,rtp_packets_sent,"
    "rtp_packets_received,rtcp_sender_reports,rtcp_receiver_reports,"
    "rtcp_packets_received,failure_timeout,failure_unexpected_sip,"
    "failure_transport_error,failure_parse_error,failure_scenario_error,"
    "failure_cancelled,interval_ms,interval_calls_per_second,delta_total_calls,"
    "delta_success_calls,delta_failed_calls,delta_retransmits,delta_timeouts,"
    "delta_rtp_packets_sent,delta_rtp_packets_received,delta_rtcp_sender_reports,"
    "delta_rtcp_receiver_reports,delta_rtcp_packets_received,"
    "delta_failure_timeout,delta_failure_unexpected_sip,"
    "delta_failure_transport_error,delta_failure_parse_error,"
    "delta_failure_scenario_error,delta_failure_cancelled"
)

# Three intervals of a one-hour, 500-concurrent run. The middle row is where
# concurrency actually peaks; the last row has drained to zero.
STAT_ROWS = [
    # Early: 4 calls in flight, none completed yet, so success_ratio is 0.
    "2026-07-31T18:00:01Z,1000,4,0,0,4,0,4,0,0,0,0,0,0,1200,1180,0,0,0,"
    "0,0,0,0,0,0,1000,4,4,0,0,0,0,1200,1180,0,0,0,0,0,0,0,0,0",
    # Mid-run: 492 concurrent. THIS is the run's peak concurrency.
    "2026-07-31T18:30:00Z,1800000,7500,7002,6,492,0.9991,4.1667,5,1,118000,40,"
    "300,20,44200000,44190000,14985,14979,29964,1,4,1,0,0,0,1000,4.1,7496,7002,"
    "6,5,1,44198800,44188820,14985,14979,29964,1,4,1,0,0,0",
    # Drained: active_calls back to 0. Cumulative columns are final.
    "2026-07-31T19:00:00Z,3600000,15000,14985,15,0,0.999,4.1667,12,3,118042,41,"
    "310,21,88410000,88396500,29970,29958,59928,3,9,2,0,1,0,1000,4.1,7500,7983,"
    "9,7,2,44210000,44206500,14985,14979,29964,2,5,1,0,1,0",
]

SUMMARY: dict = {
    "schema_version": "gossipper_summary_v1",
    "tool_version": "gossipper 0.1.62",
    "started_at": "2026-07-31T18:00:00Z",
    "finished_at": "2026-07-31T19:00:00Z",
    "duration": "1h0m0s",
    "elapsed_ms": 3600000,
    "total_calls": 15000,
    "success_calls": 14985,
    "failed_calls": 15,
    # The drain value. Sourcing concurrency from here reports 0 for this run.
    "active_calls": 0,
    "success_ratio": 0.999,
    "calls_per_second": 4.1667,
    "retransmits": 12,
    "timeouts": 3,
    "failure_classes": {
        "timeout": 3,
        "unexpected_sip": 9,
        "transport_error": 2,
        "parse_error": 0,
        "scenario_error": 1,
        "cancelled": 0,
    },
    "rtd": {
        "answer": {
            "avg": 412.6,
            "min": 188,
            "max": 2140,
            "last": 397,
            "stddev": 143.8,
            "buckets": {"<=200": 41, "<=500": 13802, "<=1000": 1096, ">2000": 1},
        }
    },
    "media": {
        "rtp_packets_sent": 88410000,
        "rtp_packets_received": 88396500,
    },
    # Never read. See test_the_generators_own_verdict_is_not_read.
    "health": {"passed": True, "min_success_ratio": 0.995},
    "findings": [],
}


def _write(
    directory: Path, *, summary: dict | None = SUMMARY, csv: bool = True
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if summary is not None:
        (directory / "summary.json").write_text(json.dumps(summary))
    if csv:
        (directory / "run_stat.csv").write_text(
            STAT_HEADER + "\n" + "\n".join(STAT_ROWS) + "\n"
        )
    return directory


def _summary(drop: str | None = None, **changes) -> dict:
    """SUMMARY with one key dropped and/or some keys replaced."""
    return {k: v for k, v in SUMMARY.items() if k != drop} | changes


def _file(directory: Path, name: str, content: str | bytes) -> Path:
    path = directory / name
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)
    return path


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    return _write(tmp_path / "ramp-500")


# --------------------------------------------------------------------------
# The full run: every column from the right surface
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        # Trap 1. The summary's active_calls is the drain value, 0. The CSV's
        # mid-run row is the real peak.
        ("peak_concurrency", 492),
        ("attempted_calls", 15000),
        # The generator's ratio is kept only as a cross-check. Rounding is
        # agreement: 14985/15000 is 0.999 exactly.
        ("reported_success_ratio", 0.999),
        ("reported_ratio_disagrees", False),
        # calls.jsonl is documented nowhere, so it can never be required.
        ("call_records_status", "absent"),
        ("call_records_count", None),
        # 2026-07-31T18:00:00Z, in UTC epoch milliseconds.
        ("started_at_ms", 1785520800000),
        ("ended_at_ms", 1785520800000 + 3_600_000),
        ("tool", "gossipper"),
        ("tool_version", "0.1.62"),
        ("rtp_packets_sent", 88410000),
        ("rtp_packets_received", 88396500),
        ("name", "ramp-500"),
    ],
)
def test_the_full_run_parses(run_dir: Path, field: str, expected) -> None:
    assert getattr(art.parse_test_directory(run_dir), field) == expected


def test_the_name_can_be_overridden(run_dir: Path) -> None:
    assert art.parse_test_directory(run_dir, name="step-3").name == "step-3"


def test_establishment_is_computed_from_counts_not_scanned_per_row(
    run_dir: Path,
) -> None:
    """Trap 2. A per-row min over success_ratio reads 0 and fails a healthy run."""
    parsed = art.parse_test_directory(run_dir)
    assert parsed.establishment_ratio == pytest.approx(14985 / 15000)
    # The wrong answer, pinned: the first interval legitimately reads 0.
    per_row_min = min(
        s.success_ratio for s in parsed.samples if s.success_ratio is not None
    )
    assert per_row_min == 0.0


@pytest.mark.parametrize(
    "parsed",
    [
        # 0.0 would fail the 0.995 gate for a run that never ran.
        art.ParsedTest(name="t", attempted_calls=0, succeeded_calls=0),
        art.ParsedTest(name="t"),
    ],
)
def test_establishment_is_none_rather_than_zero(parsed: art.ParsedTest) -> None:
    assert parsed.establishment_ratio is None


@pytest.mark.parametrize(
    ("ratio", "disagrees"),
    [
        (0.60, True),  # a ratio contradicting its own counts is caught
        (None, False),  # a missing ratio is not a disagreement
        # Every comparison against NaN is False, so the property itself is
        # fooled. That is why parse_summary refuses a NaN at the boundary.
        (float("nan"), False),
    ],
)
def test_the_reported_ratio_is_kept_only_to_contradict_the_counts(
    ratio: float | None, disagrees: bool
) -> None:
    parsed = art.ParsedTest(
        name="t", attempted_calls=100, succeeded_calls=10, reported_success_ratio=ratio
    )
    assert parsed.reported_ratio_disagrees is disagrees


# --------------------------------------------------------------------------
# Trap 3: the generator's self-assessment
# --------------------------------------------------------------------------


def test_the_generators_own_verdict_is_not_read(tmp_path: Path) -> None:
    """Two summaries differing ONLY in health.passed must parse identically.

    Asserting the parsed output is identical proves the verdict reached nothing.
    The parser reports measurements; judging them happens in gates.py.
    """
    failing = _summary(health={"passed": False, "min_success_ratio": 0.995})
    a = art.parse_test_directory(_write(tmp_path / "a"), name="same")
    b = art.parse_test_directory(_write(tmp_path / "b", summary=failing), name="same")
    assert a == b
    assert not {"passed", "health", "verdict", "status"} & set(vars(a))


# --------------------------------------------------------------------------
# Trap 4: failures, and the two shapes they arrive in
# --------------------------------------------------------------------------


def test_both_surfaces_normalise_onto_the_same_cause_names(run_dir: Path) -> None:
    """The summary NESTS failures; the CSV carries them FLAT.

    delta_failure_* beside the CSV's cumulative columns is per-interval: in the
    last row unexpected_sip reads 9 cumulative against a 5 delta.
    """
    from_json = art.parse_summary(run_dir / "summary.json").failures_by_cause
    from_csv = art.parse_stat_csv(run_dir / "run_stat.csv")[-1].failures_by_cause
    assert from_json == SUMMARY["failure_classes"]
    assert from_csv == from_json


# --------------------------------------------------------------------------
# Either surface alone, altered, or partial
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("summary", "csv", "expected"),
    [
        # No CSV: concurrency is None, not the summary's drain value of 0.
        # Everything the summary CAN answer still comes through.
        (SUMMARY, False, {"peak_concurrency": None, "attempted_calls": 15000}),
        # CSV alone: cumulative columns fall back to the LAST row, nothing only
        # the summary carries is invented, and failures come from the CSV.
        (
            None,
            True,
            {
                "peak_concurrency": 492,
                "attempted_calls": 15000,
                "succeeded_calls": 14985,
                "duration_ms": 3600000,
                "answer_latency": None,
                "artifact_schema_version": None,
                "failures_by_cause": SUMMARY["failure_classes"],
            },
        ),
        # An omitted cause is absent, not 0, because 0 claims there were none.
        (
            _summary(failure_classes={"timeout": 3}),
            True,
            {"failures_by_cause": {"timeout": 3}},
        ),
        # No answer timing: the scenario did not configure start_rtd.
        (_summary(drop="rtd"), True, {"answer_latency": None}),
        # Nothing is invented for a differently-formatted build string.
        (
            _summary(tool_version="gossipper-0.1.62"),
            True,
            {"tool": "gossipper-0.1.62", "tool_version": None},
        ),
    ],
    ids=["summary-only", "csv-only", "partial-causes", "no-rtd", "unspaced-tool"],
)
def test_a_partial_artifact_parses_what_it_can(
    tmp_path: Path, summary: dict | None, csv: bool, expected: dict
) -> None:
    parsed = art.parse_test_directory(_write(tmp_path / "d", summary=summary, csv=csv))
    assert {k: getattr(parsed, k) for k in expected} == expected


def test_neither_surface_present_raises_missing(tmp_path: Path) -> None:
    with pytest.raises(art.MissingArtifact):
        art.parse_test_directory(tmp_path)


def test_a_summary_that_omits_failure_classes_does_not_take_the_csv_totals(
    tmp_path: Path,
) -> None:
    """An absent section is not the same fact as an empty one.

    A clean run may omit failure_classes. Letting the CSV's cumulative counts
    fill it would contradict the summary's own failed_calls.
    """
    clean = _summary(drop="failure_classes", failed_calls=0, success_calls=15000)
    _file(tmp_path, "summary.json", json.dumps(clean))
    _file(
        tmp_path,
        "s.csv",
        "elapsed_ms,total_calls,success_calls,failed_calls,active_calls,"
        "failure_timeout\n3600000,15000,15000,0,0,5\n",
    )
    parsed = art.parse_test_directory(tmp_path)
    assert parsed.failed_calls == 0
    assert sum(parsed.failures_by_cause.values()) == 0


def test_answer_latency_is_carried_through_without_inventing_percentiles(
    run_dir: Path,
) -> None:
    """Buckets keep the generator's labels. A p95 from edges is fabrication."""
    latency = art.parse_test_directory(run_dir).answer_latency
    assert latency is not None
    assert (latency.avg_ms, latency.max_ms) == (412.6, 2140)
    assert latency.buckets["<=500"] == 13802
    assert not hasattr(latency, "p95_ms")


def test_every_parsed_field_maps_onto_a_load_run_test_column(run_dir: Path) -> None:
    """DATA3 writes this straight into load_run_tests, so the names must line up."""
    from voicegateway.repository.load_runs_repository import LoadRunTestInput

    names = {
        "started_at_ms",
        "ended_at_ms",
        "peak_concurrency",
        "attempted_calls",
        "succeeded_calls",
        "failed_calls",
        "rtp_packets_sent",
        "rtp_packets_received",
    }
    assert names <= set(LoadRunTestInput.__dataclass_fields__)
    assert names <= set(vars(art.parse_test_directory(run_dir)))


# --------------------------------------------------------------------------
# The defensive contract: a named error on a shape this parser does not know
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("summary", "error", "match"),
    [
        # The message names what it found, so a human can act on it.
        (
            _summary(schema_version="gossipper_summary_v2"),
            art.UnknownArtifactSchema,
            "gossipper_summary_v2",
        ),
        # Not read best-effort. An unversioned file is an unknown one.
        (_summary(drop="schema_version"), art.UnknownArtifactSchema, None),
        # An unknown VERSION and a corrupt FILE warrant different responses.
        (_summary(total_calls="not-a-number"), art.MalformedArtifact, None),
        # json.dumps writes a bare NaN token, which json.loads accepts.
        (_summary(success_ratio=float("nan")), art.MalformedArtifact, None),
    ],
    ids=["schema-v2", "no-schema", "not-a-number", "nan-ratio"],
)
def test_an_unusable_summary_raises_a_named_error(
    tmp_path: Path, summary: dict, error: type, match: str | None
) -> None:
    with pytest.raises(error, match=match):
        art.parse_test_directory(_write(tmp_path / "d", summary=summary))


_COUNTS = "elapsed_ms,total_calls,success_calls,failed_calls,active_calls\n"


@pytest.mark.parametrize(
    ("name", "content", "error", "match"),
    [
        ("summary.json", "{not json", art.MalformedArtifact, None),
        (
            "summary.json",
            '{"schema_version": "gossipper_summary_v1", "total_calls": Infinity}',
            art.MalformedArtifact,
            None,
        ),
        (
            "s.csv",
            "timestamp,total_calls\n2026-07-31T18:00:00Z,4\n",
            art.UnknownArtifactSchema,
            "active_calls",
        ),
        # DictReader keeps the last occurrence, so the value read is unknowable.
        (
            "s.csv",
            "elapsed_ms,total_calls,total_calls,success_calls,failed_calls,"
            "active_calls\n1000,4,999,0,0,4\n",
            art.UnknownArtifactSchema,
            "total_calls",
        ),
        # float() accepts these, then int() raises outside this module's
        # hierarchy as a bare ValueError or OverflowError.
        *[
            ("s.csv", f"{_COUNTS}1000,{token},0,0,4\n", art.ArtifactError, None)
            for token in ("nan", "NaN", "inf", "-inf", "Infinity")
        ],
    ],
)
def test_a_malformed_file_raises_a_named_error(
    tmp_path: Path, name: str, content: str, error: type, match: str | None
) -> None:
    parse = art.parse_summary if name.endswith(".json") else art.parse_stat_csv
    with pytest.raises(error, match=match):
        parse(_file(tmp_path, name, content))


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        # A generator that adds or reorders a column must still import.
        (
            "brand_new_column,active_calls,failed_calls,success_calls,total_calls,"
            "elapsed_ms\nxyz,492,6,7002,7500,1800000\n",
            {"active_calls": 492, "total_calls": 7500},
        ),
        # An empty value is not measured, never zero.
        (
            f"{_COUNTS}1000,4,,,\n",
            {"total_calls": 4, "success_calls": None, "active_calls": None},
        ),
        # A BOM attaches to the first header field. timestamp is not required,
        # so nothing would raise and every at_ms would silently read None.
        (
            b"\xef\xbb\xbftimestamp,elapsed_ms,total_calls,success_calls,"
            b"failed_calls,active_calls\n2026-07-31T18:00:01Z,1000,4,0,0,4\n",
            {"at_ms": 1785520801000, "active_calls": 4},
        ),
    ],
    ids=["reordered", "empty-values", "byte-order-mark"],
)
def test_a_csv_row_is_read_by_column_name(
    tmp_path: Path, content: str | bytes, expected: dict
) -> None:
    [row] = art.parse_stat_csv(_file(tmp_path, "s.csv", content))
    assert {k: getattr(row, k) for k in expected} == expected


# --------------------------------------------------------------------------
# calls.jsonl: optional enrichment, never a requirement
# --------------------------------------------------------------------------


def _call_record(number: int, *, sent: int, received: int, success: bool = True) -> str:
    """One record in the shape a captured gossipper run actually writes."""
    return json.dumps(
        {
            "schema_version": "gossipper_call_record_v1",
            "call_id": f"gossip-{number}-{number}-3e3d7839",
            "call_number": number,
            "success": success,
            "duration_ms": 300277,
            "media": {
                "RTPPacketsSent": sent,
                "RTPOctetsSent": sent * 160,
                "RTPPacketsReceived": received,
                "RTCPSenderReports": 599,
            },
        }
    )


def _calls(*records: tuple[int, int]) -> str:
    return "\n".join(
        _call_record(n, sent=s, received=r) for n, (s, r) in enumerate(records)
    )


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        # (status, count, answered_with_inbound, answered_without_inbound)
        (None, ("absent", None, None, None)),
        # Counted, but a foreign schema leaves the tally None, never zero: zero
        # would read as "every call got audio".
        (
            '{"whatever": 1}\n{"unknown_shape": true}\n\n{"third": null}\n',
            ("present", 3, None, None),
        ),
        ('{"schema_version": "something_else_v9"}', ("present", 1, None, None)),
        # Unreadable never fails the import. UnicodeDecodeError is a
        # ValueError, not an OSError.
        ("{not json at all\n", ("unreadable", None, None, None)),
        (b"\xff\xfe not valid utf-8\n", ("unreadable", None, None, None)),
        (_calls((15000, 14997), (15000, 0), (15000, 0)), ("present", 3, 1, 2)),
        # THE POINT OF THE WHOLE CHANGE. These two runs have identical
        # received-per-sent totals; only the per-call counts tell them apart.
        (_calls(*[(1000, 500)] * 4), ("present", 4, 4, 0)),
        (_calls(*[(1000, 1000)] * 2, *[(1000, 0)] * 2), ("present", 4, 2, 2)),
        # A failed call is owned by the establishment gate; counting it here
        # would punish one failure twice.
        (
            _call_record(1, sent=0, received=0, success=False),
            ("present", 1, 0, 0),
        ),
        # A call that sent nothing never exercised the return path.
        (_calls((0, 0)), ("present", 1, 0, 0)),
    ],
)
def test_call_records_are_tallied_without_failing_the_import(
    run_dir: Path, content: str | bytes | None, expected: tuple
) -> None:
    if content is not None:
        _file(run_dir, "calls.jsonl", content)
    parsed = art.parse_test_directory(run_dir)
    assert (
        parsed.call_records_status,
        parsed.call_records_count,
        parsed.calls_answered_with_inbound,
        parsed.calls_answered_without_inbound,
    ) == expected
    # The primary surfaces still imported, untouched by the records.
    assert (parsed.attempted_calls, parsed.peak_concurrency) == (15000, 492)
