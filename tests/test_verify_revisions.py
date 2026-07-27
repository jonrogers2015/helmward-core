"""Tests for tools/verify_revisions.py.

This file exists because the tool it tests writes the record the public site
renders from. A verifier with no test of its own has the same shape as the
dream cycle: something writes 'this is true' into a file nobody checks.

Each test builds its own throwaway registry (tmp_path) with synthetic lines
whose specs are real, cheap, deterministic shell commands (`true`, `false`,
`echo`) rather than anything touching helmward-core or CT201 -- this suite
verifies the TOOL, not the product.

Run: python3 -m pytest tests/test_verify_revisions.py -q
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import verify_revisions as vr  # noqa: E402


# --------------------------------------------------------------------- fixtures


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def make_line(id_, **overrides):
    base = {
        "id": id_,
        "public_name": id_,
        "revision": "0.1",
        "state": "shipped",
        "statement": "test line",
        "verification_class": "probe",
        "acceptance": None,
        "last_verified": None,
        "verdict": None,
        "renderable": False,
        "next": None,
    }
    base.update(overrides)
    return base


def make_registry(lines, **top_overrides):
    reg = {
        "schema_version": 1,
        "staleness_days": 14,
        "assertion_staleness_days": 90,
        "lines": lines,
    }
    reg.update(top_overrides)
    return reg


@pytest.fixture
def registry_path(tmp_path):
    return str(tmp_path / "revisions.json")


# ------------------------------------------------------------- individual lines


def test_passing_probe_verdict_and_stamp():
    line = make_line(
        "passes",
        acceptance={"type": "command_exit_code", "command": "true", "expected_exit_code": 0},
    )
    verdict, detail, renderable, command = vr.run_probe(line)
    assert verdict == "passed"
    assert renderable is True
    # attested wraps command_exit_code specs (e.g. "; echo PROBE_EXIT_CODE:$?")
    # to carry the exit code through the shell, so check containment, not
    # equality, against the original command.
    assert "true" in command


def test_failing_probe_verdict():
    line = make_line(
        "fails",
        acceptance={"type": "command_exit_code", "command": "false", "expected_exit_code": 0},
    )
    verdict, detail, renderable, _ = vr.run_probe(line)
    assert verdict == "failed"
    assert renderable is False


def test_missing_spec_is_not_renderable_and_has_no_verdict():
    line = make_line("unspecced", acceptance=None, acceptance_blocked_on="waiting on X")
    verdict, detail, renderable, _ = vr.run_probe(line)
    assert verdict is None
    assert renderable is False
    assert "waiting on X" in detail


def test_placeholder_spec_is_refused_not_executed():
    """A CONFIRM-* placeholder must never reach a shell. Executing a
    half-written spec by accident is worse than refusing it."""
    line = make_line(
        "placeholder",
        acceptance={"type": "command_output_contains", "command": "CONFIRM-ENDPOINT", "expected": "CONFIRM-EXPECTED"},
    )
    verdict, detail, renderable, _ = vr.run_probe(line)
    assert verdict is None
    assert renderable is False
    assert "placeholder" in detail


def test_invalid_spec_fails_closed_not_a_crash():
    """attested.verify() never raises for a bad spec -- it returns a failed
    Result. This confirms our wrapper doesn't accidentally let an exception
    through for a malformed acceptance spec."""
    line = make_line("badspec", acceptance={"type": "not_a_real_type", "command": "true"})
    verdict, detail, renderable, _ = vr.run_probe(line)
    assert verdict == "failed"
    assert renderable is False


# ------------------------------------------------------------------- regressed


def test_regressed_line_never_renders_even_with_a_spec():
    """A line can carry a leftover acceptance spec from when it shipped.
    Regressed status must override it regardless."""
    data = make_registry(
        [
            make_line(
                "broke",
                state="regressed",
                acceptance={"type": "command_exit_code", "command": "true", "expected_exit_code": 0},
            )
        ]
    )
    rows = vr.verify_registry(data, _now())
    assert rows[0]["verdict"] == "regressed"
    assert rows[0]["renderable"] is False


# -------------------------------------------------------------------- asserted


def test_asserted_line_unsigned_does_not_render():
    line = make_line("claim", verification_class="asserted", asserted_by=None, asserted_on=None)
    verdict, detail, renderable = vr.judge_asserted(line, 90, _now())
    assert renderable is False
    assert "unsigned" in verdict


def test_asserted_line_signed_and_fresh_renders():
    now = _now()
    line = make_line(
        "claim",
        verification_class="asserted",
        asserted_by="Jon",
        asserted_on=_iso(now - timedelta(days=1)),
    )
    verdict, detail, renderable = vr.judge_asserted(line, 90, now)
    assert renderable is True
    assert verdict == "asserted"


def test_asserted_line_stale_signature_does_not_render():
    now = _now()
    line = make_line(
        "claim",
        verification_class="asserted",
        asserted_by="Jon",
        asserted_on=_iso(now - timedelta(days=91)),
    )
    verdict, detail, renderable = vr.judge_asserted(line, 90, now)
    assert renderable is False
    assert "stale" in verdict


def test_asserted_unparseable_timestamp_treated_as_absent():
    """A corrupt timestamp must fail closed, not raise and not pass."""
    line = make_line(
        "claim", verification_class="asserted", asserted_by="Jon", asserted_on="not-a-date"
    )
    verdict, detail, renderable = vr.judge_asserted(line, 90, _now())
    assert renderable is False


# ----------------------------------------------------------------------- --check


def test_check_recomputes_entitlement_ignores_stored_flag_when_consistent():
    now = _now()
    data = make_registry(
        [
            make_line(
                "ok",
                verdict="passed",
                renderable=True,
                last_verified=_iso(now - timedelta(days=1)),
            )
        ]
    )
    rows = vr.check_committed(data, now)
    assert rows[0]["renderable"] is True
    assert "MISMATCH" not in rows[0]["detail"]


def test_check_catches_tampered_renderable_flag():
    """The core guarantee: hand-editing renderable=true with no real verdict
    behind it must not grant entitlement. This is the whole reason --check
    recomputes instead of trusting the stored field."""
    now = _now()
    data = make_registry(
        [make_line("tampered", verdict=None, renderable=True, last_verified=None)]
    )
    rows = vr.check_committed(data, now)
    assert rows[0]["renderable"] is False
    assert "MISMATCH" in rows[0]["detail"]


def test_check_rejects_stale_probe_verdict():
    now = _now()
    data = make_registry(
        [
            make_line(
                "old",
                verdict="passed",
                renderable=True,
                last_verified=_iso(now - timedelta(days=15)),
            )
        ],
        staleness_days=14,
    )
    rows = vr.check_committed(data, now)
    assert rows[0]["renderable"] is False


def test_check_rejects_regressed_even_if_flagged_renderable():
    now = _now()
    data = make_registry(
        [
            make_line(
                "broke",
                state="regressed",
                verdict="passed",
                renderable=True,
                last_verified=_iso(now),
            )
        ]
    )
    rows = vr.check_committed(data, now)
    assert rows[0]["renderable"] is False


# --------------------------------------------------------------------- coverage


def test_coverage_mismatch_is_a_hard_failure():
    """report() must fail if the number of verdicts doesn't match the number
    of lines actually in the registry -- an audit that silently drops a line
    is exactly the failure mode this project hit with the 2-of-18 blog miss."""
    rows = [{"id": "a", "class": "probe", "verdict": "passed", "renderable": True, "detail": "ok"}]
    exit_code = vr.report(rows, total_lines=2)
    assert exit_code == 1


def test_coverage_matches_and_all_pass_exits_zero():
    rows = [{"id": "a", "class": "probe", "verdict": "passed", "renderable": True, "detail": "ok"}]
    exit_code = vr.report(rows, total_lines=1)
    assert exit_code == 0


def test_any_failed_verdict_is_a_nonzero_exit_even_if_coverage_matches():
    rows = [
        {"id": "a", "class": "probe", "verdict": "passed", "renderable": True, "detail": "ok"},
        {"id": "b", "class": "probe", "verdict": "failed", "renderable": False, "detail": "no"},
    ]
    exit_code = vr.report(rows, total_lines=2)
    assert exit_code == 1


# -------------------------------------------------------------- end-to-end (fs)


def test_end_to_end_writes_atomically_and_stamps(registry_path):
    data = make_registry(
        [
            make_line(
                "pass_line",
                acceptance={"type": "command_exit_code", "command": "true", "expected_exit_code": 0},
            ),
            make_line(
                "fail_line",
                acceptance={"type": "command_exit_code", "command": "false", "expected_exit_code": 0},
            ),
        ]
    )
    vr.save(registry_path, data)

    loaded = vr.load(registry_path)
    now = _now()
    rows = vr.verify_registry(loaded, now)
    loaded["generated_at"] = _iso(now)
    vr.save(registry_path, loaded)

    on_disk = vr.load(registry_path)
    by_id = {l["id"]: l for l in on_disk["lines"]}
    assert by_id["pass_line"]["verdict"] == "passed"
    assert by_id["pass_line"]["last_verified"] is not None
    assert by_id["fail_line"]["verdict"] == "failed"
    assert on_disk["generated_at"] == loaded["generated_at"]

    exit_code = vr.report(rows, total_lines=2)
    assert exit_code == 1  # fail_line failed


def test_save_leaves_no_tmp_file_behind_on_success(registry_path):
    data = make_registry([make_line("x")])
    vr.save(registry_path, data)
    directory = os.path.dirname(registry_path)
    leftovers = [f for f in os.listdir(directory) if f.startswith(".revisions-")]
    assert leftovers == []


def test_save_is_atomic_original_survives_a_mid_write_crash(monkeypatch, registry_path):
    """If json.dump raises partway through, the original file on disk must be
    untouched -- a half-written registry is a registry the site would read."""
    original = make_registry([make_line("safe", verdict="passed", renderable=True)])
    vr.save(registry_path, original)

    def boom(*a, **kw):
        raise RuntimeError("simulated crash mid-write")

    monkeypatch.setattr(vr.json, "dump", boom)
    with pytest.raises(RuntimeError):
        vr.save(registry_path, {"lines": []})

    survived = vr.load(registry_path)
    assert survived["lines"][0]["id"] == "safe"

    directory = os.path.dirname(registry_path)
    leftovers = [f for f in os.listdir(directory) if f.startswith(".revisions-")]
    assert leftovers == []


def test_unknown_verification_class_does_not_render():
    data = make_registry([make_line("weird", verification_class="mystery")])
    rows = vr.verify_registry(data, _now())
    assert rows[0]["renderable"] is False
    assert rows[0]["verdict"] is None
