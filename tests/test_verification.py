#!/usr/bin/env python3
"""Tests for the verification gate's pure logic.

These cover _build_raw_command and _evaluate_probe_result -- the two functions
that decide, with no model and no network in the loop, whether a task's claimed
result is actually true. They are pure: spec in, command or verdict out. No DB,
no HTTP, no agent.

Run:
    cd /root/helmward-core && python3 -m unittest discover -s tests -v

WHY THIS FILE EXISTS AT ALL: these functions are the product. Everything else in
Helmward is a queue with a dashboard. If _evaluate_probe_result can return
(True, ...) for work that did not happen, the entire premise fails, and it fails
silently -- which is worse than failing loudly, because the dashboard goes green.

Several tests below are written against the behavior the gate MUST have, not the
behavior it currently has. Those are marked VACUOUS PASS and they fail on the
pre-fix code. That is deliberate: they document real defects found while
preparing the gate for extraction into a standalone library.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

# Point DB_PATH somewhere harmless before importing app.*, so importing the
# module under test can never touch a real database.
os.environ.setdefault(
    "DB_PATH", os.path.join(tempfile.gettempdir(), "helmward-tests-unused.db")
)
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "control-plane")
)

from app.verification import (  # noqa: E402
    build_raw_command as _build_raw_command,
    evaluate_probe_result as _evaluate_probe_result,
)


class TestBuildRawCommand(unittest.TestCase):
    """A spec must translate to a literal shell command with nothing to interpret."""

    def test_file_exists(self):
        cmd = _build_raw_command({"type": "file_exists", "path": "/tmp/x"})
        self.assertIn("/tmp/x", cmd)
        self.assertTrue(cmd.startswith("ls "))

    def test_file_checksum(self):
        cmd = _build_raw_command({"type": "file_checksum", "path": "/tmp/x", "sha256": "a" * 64})
        self.assertIn("sha256sum", cmd)
        self.assertIn("/tmp/x", cmd)

    def test_command_output_contains_passes_command_through(self):
        cmd = _build_raw_command(
            {"type": "command_output_contains", "command": "echo hi", "expected": "hi"}
        )
        self.assertEqual(cmd, "echo hi")

    def test_command_exit_code_appends_marker(self):
        cmd = _build_raw_command({"type": "command_exit_code", "command": "true"})
        self.assertIn("PROBE_EXIT_CODE:$?", cmd)
        self.assertTrue(cmd.startswith("true"))

    def test_unknown_type_raises(self):
        with self.assertRaises(ValueError):
            _build_raw_command({"type": "no_such_check"})

    def test_missing_required_key_raises(self):
        # A spec without the key its command needs must fail loudly at build
        # time, not produce a malformed command that fails confusingly later.
        with self.assertRaises((KeyError, ValueError)):
            _build_raw_command({"type": "file_exists"})


class TestEvaluateHappyPath(unittest.TestCase):
    """Real probe output, correct verdicts."""

    def test_file_exists_confirmed(self):
        out = "-rw-r--r-- 1 root root 1024 Jul 24 00:00 /tmp/x"
        passed, _ = _evaluate_probe_result({"type": "file_exists", "path": "/tmp/x"}, out)
        self.assertTrue(passed)

    def test_file_exists_missing(self):
        out = "ls: cannot access '/tmp/x': No such file or directory"
        passed, detail = _evaluate_probe_result(
            {"type": "file_exists", "path": "/tmp/x"}, out
        )
        self.assertFalse(passed)
        self.assertIn("/tmp/x", detail)

    def test_file_exists_min_bytes_enforced(self):
        out = "-rw-r--r-- 1 root root 10 Jul 24 00:00 /tmp/x"
        passed, _ = _evaluate_probe_result(
            {"type": "file_exists", "path": "/tmp/x", "min_bytes": 500}, out
        )
        self.assertFalse(passed)

    def test_checksum_match(self):
        h = "a" * 64
        passed, _ = _evaluate_probe_result(
            {"type": "file_checksum", "path": "/tmp/x", "sha256": h}, "%s  /tmp/x" % h
        )
        self.assertTrue(passed)

    def test_checksum_mismatch(self):
        passed, detail = _evaluate_probe_result(
            {"type": "file_checksum", "path": "/tmp/x", "sha256": "a" * 64},
            "%s  /tmp/x" % ("b" * 64),
        )
        self.assertFalse(passed)
        self.assertIn("mismatch", detail.lower())

    def test_output_contains_hit_and_miss(self):
        spec = {"type": "command_output_contains", "command": "x", "expected": "READY"}
        self.assertTrue(_evaluate_probe_result(spec, "system READY now")[0])
        self.assertFalse(_evaluate_probe_result(spec, "system starting")[0])

    def test_exit_code_match_and_mismatch(self):
        spec = {"type": "command_exit_code", "command": "true", "expected_exit_code": 0}
        self.assertTrue(_evaluate_probe_result(spec, "PROBE_EXIT_CODE:0")[0])
        self.assertFalse(_evaluate_probe_result(spec, "PROBE_EXIT_CODE:1")[0])

    def test_exit_code_nonzero_expectation_is_honored(self):
        # Regression: expecting a nonzero code must actually check that code,
        # not fall back to 0.
        spec = {"type": "command_exit_code", "command": "false", "expected_exit_code": 1}
        self.assertTrue(_evaluate_probe_result(spec, "PROBE_EXIT_CODE:1")[0])
        self.assertFalse(_evaluate_probe_result(spec, "PROBE_EXIT_CODE:0")[0])

    def test_agent_result_matches_probe_exact(self):
        spec = {"type": "agent_result_matches_probe", "command": "df", "match": "exact_string"}
        self.assertTrue(_evaluate_probe_result(spec, "42G free", "42G free")[0])
        self.assertFalse(_evaluate_probe_result(spec, "42G free", "500G free")[0])

    def test_unknown_type_fails_closed(self):
        passed, _ = _evaluate_probe_result({"type": "no_such_check"}, "anything")
        self.assertFalse(passed)


class TestVacuousPasses(unittest.TestCase):
    """Empty probe output means the probe did not run. It must never pass.

    Every case here is a way the gate can currently return True for work that
    was never verified. These are the defects that block extracting the gate as
    a trust library -- a checker that green-lights an absent result is worse
    than no checker, because it manufactures false confidence.
    """

    def test_file_exists_empty_output_must_fail(self):
        # VACUOUS PASS: "" contains neither error string, min_bytes is absent,
        # so the function falls through to return True.
        passed, _ = _evaluate_probe_result({"type": "file_exists", "path": "/tmp/x"}, "")
        self.assertFalse(passed, "empty probe output must never confirm a file exists")

    def test_file_exists_whitespace_output_must_fail(self):
        passed, _ = _evaluate_probe_result(
            {"type": "file_exists", "path": "/tmp/x"}, "   \n  "
        )
        self.assertFalse(passed, "whitespace-only probe output must not confirm anything")

    def test_output_contains_missing_expected_key_must_fail(self):
        # VACUOUS PASS: spec.get("expected", "") yields "", and "" is a
        # substring of every string, so this passes unconditionally.
        spec = {"type": "command_output_contains", "command": "whoami"}
        passed, _ = _evaluate_probe_result(spec, "literally anything")
        self.assertFalse(passed, "a spec with no 'expected' must be rejected, not pass")

    def test_output_contains_empty_expected_must_fail(self):
        spec = {"type": "command_output_contains", "command": "whoami", "expected": ""}
        passed, _ = _evaluate_probe_result(spec, "literally anything")
        self.assertFalse(passed, "an empty 'expected' is meaningless and must not pass")

    def test_output_contains_empty_probe_output_must_fail(self):
        spec = {"type": "command_output_contains", "command": "whoami", "expected": "root"}
        passed, _ = _evaluate_probe_result(spec, "")
        self.assertFalse(passed)

    def test_agent_match_both_empty_must_fail(self):
        # VACUOUS PASS: "" == "" is True, so a silent agent and a dead probe
        # "agree" and the task is marked verified.
        spec = {"type": "agent_result_matches_probe", "command": "df", "match": "exact_string"}
        passed, _ = _evaluate_probe_result(spec, "", "")
        self.assertFalse(passed, "two empty strings are not evidence of agreement")

    def test_checksum_empty_output_fails(self):
        # file_checksum already handles this correctly -- it is the model the
        # other checks should follow.
        passed, _ = _evaluate_probe_result(
            {"type": "file_checksum", "path": "/tmp/x", "sha256": "a" * 64}, ""
        )
        self.assertFalse(passed)

    def test_checksum_missing_sha256_fails(self):
        passed, detail = _evaluate_probe_result(
            {"type": "file_checksum", "path": "/tmp/x"}, "%s  /tmp/x" % ("a" * 64)
        )
        self.assertFalse(passed)
        self.assertIn("sha256", detail.lower())


class TestSpecValidation(unittest.TestCase):
    """Malformed specs must be rejected, never silently reinterpreted."""

    def test_exit_code_wrong_key_name_must_not_silently_default(self):
        # The atomic_test_v2.sh bug: writing "expected" instead of
        # "expected_exit_code" made the gate silently check for 0. It happened
        # to be right that time. A typo must never decide what gets verified.
        spec = {"type": "command_exit_code", "command": "false", "expected": 1}
        passed, detail = _evaluate_probe_result(spec, "PROBE_EXIT_CODE:0")
        self.assertFalse(
            passed,
            "an unrecognized spec key must be an error, not a silent fallback to 0",
        )

    def test_command_types_require_a_command(self):
        for t in ("command_output_contains", "command_exit_code", "agent_result_matches_probe"):
            with self.subTest(type=t):
                with self.assertRaises((KeyError, ValueError)):
                    _build_raw_command({"type": t, "expected": "x"})

    def test_agent_match_unknown_strategy_fails_closed(self):
        spec = {
            "type": "agent_result_matches_probe",
            "command": "df",
            "match": "fuzzy_vibes",
        }
        passed, _ = _evaluate_probe_result(spec, "same", "same")
        self.assertFalse(passed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
