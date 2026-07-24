"""The Helmward verification gate.

Pure logic, no I/O: a spec plus probe output goes in, a verdict comes out. No
database, no HTTP, no model, no agent, no filesystem. That isolation is the
whole point -- this module decides whether a claimed result is actually true,
so it must not depend on anything that could itself be lying.

Two entry points:

    build_raw_command(spec) -> str
        Turn a spec into a literal shell command for a worker to execute
        directly. No model interprets it, so there is nothing to paraphrase.

    evaluate_probe_result(spec, probe_output, agent_result="") -> (bool, str)
        Judge the probe's real output against the spec. Returns the verdict
        and a human-readable reason.

DESIGN RULE, and the reason this module exists separately: THE GATE FAILS
CLOSED. Every ambiguous situation resolves to "not verified". Specifically:

  * Empty or whitespace-only probe output NEVER passes any check. Empty output
    means the probe did not run, or ran and produced nothing -- neither is
    evidence that the work happened. The pre-2026-07-24 implementation treated
    it as a pass for file_exists, command_output_contains and
    agent_result_matches_probe, because "" is a substring of every string and
    "" == "" is true. A checker that green-lights an absent result is worse
    than no checker: it manufactures confidence.

  * Malformed specs are errors, never silent reinterpretation. Writing
    "expected" instead of "expected_exit_code" used to make the gate quietly
    check for exit code 0 -- it happened to be correct that time, which is
    exactly why it survived. A typo must never decide what gets verified.

  * Exit codes are parsed and compared as integers, not substring-matched.
    Matching "PROBE_EXIT_CODE:1" as a substring passes on an actual code of
    10, 100, or 199.

Adding a check type means adding one entry to SPEC_SCHEMA and one branch to
each of the two functions. The schema is the single source of truth for what
is valid -- api.py imports SUPPORTED_TYPES from here rather than keeping its
own list, so the two cannot drift apart.
"""
from __future__ import annotations

import re

__all__ = [
    "SpecError",
    "SUPPORTED_TYPES",
    "SPEC_SCHEMA",
    "validate_spec",
    "build_raw_command",
    "evaluate_probe_result",
]


class SpecError(ValueError):
    """A verification_spec is missing required keys, or carries unknown ones."""


# Single source of truth for spec shape. required = must be present and
# non-empty; optional = permitted; anything else is rejected outright.
SPEC_SCHEMA: dict[str, dict[str, frozenset]] = {
    "file_exists": {
        "required": frozenset({"path"}),
        "optional": frozenset({"min_bytes"}),
    },
    "file_checksum": {
        "required": frozenset({"path", "sha256"}),
        "optional": frozenset(),
    },
    "command_output_contains": {
        "required": frozenset({"command", "expected"}),
        "optional": frozenset(),
    },
    "command_exit_code": {
        "required": frozenset({"command"}),
        "optional": frozenset({"expected_exit_code"}),
    },
    "agent_result_matches_probe": {
        "required": frozenset({"command"}),
        "optional": frozenset({"match", "match_key"}),
    },
}

SUPPORTED_TYPES = frozenset(SPEC_SCHEMA)

_MATCH_STRATEGIES = frozenset({"exact_string", "exact_line_containing"})

# Standard `ls -l` line: permission bits, link count, owner, group, SIZE, ...
# Captures size as group 1 wherever it appears, so it survives agents that wrap
# raw output in JSON or markdown despite being told not to.
_LS_LINE_RE = re.compile(
    r"^[-dlcbps][-rwxsStT]{9}\+?\s+\d+\s+\S+\s+\S+\s+(\d+)\s+\w+\s+\d+",
    re.MULTILINE,
)

_EXIT_CODE_RE = re.compile(r"PROBE_EXIT_CODE:(\d+)")

_MISSING_FILE_MARKERS = ("No such file or directory", "cannot access")


def validate_spec(spec: dict) -> str:
    """Check a spec's shape. Returns its type, or raises SpecError.

    Unknown keys are rejected rather than ignored: a spec carrying a key this
    module does not understand is far more likely to be a typo for one that
    matters than a harmless extra.
    """
    if not isinstance(spec, dict):
        raise SpecError("verification_spec must be an object, got %s" % type(spec).__name__)

    t = spec.get("type")
    if not t:
        raise SpecError("verification_spec has no 'type'")
    if t not in SPEC_SCHEMA:
        raise SpecError(
            "unsupported verification type %r; must be one of %s"
            % (t, ", ".join(sorted(SUPPORTED_TYPES)))
        )

    schema = SPEC_SCHEMA[t]
    given = set(spec) - {"type"}
    allowed = schema["required"] | schema["optional"]

    unknown = given - allowed
    if unknown:
        raise SpecError(
            "unknown key(s) %s for type %r; allowed: %s. A misspelled key is "
            "not ignored here -- it would silently change what gets checked."
            % (", ".join(repr(k) for k in sorted(unknown)), t, ", ".join(sorted(allowed)))
        )

    missing = schema["required"] - given
    if missing:
        raise SpecError(
            "type %r requires %s" % (t, ", ".join(repr(k) for k in sorted(missing)))
        )

    for key in sorted(schema["required"]):
        value = spec[key]
        if value is None or (isinstance(value, str) and not value.strip()):
            raise SpecError("%r must not be empty for type %r" % (key, t))

    if t == "agent_result_matches_probe":
        strategy = spec.get("match", "exact_string")
        if strategy not in _MATCH_STRATEGIES:
            raise SpecError(
                "unknown match strategy %r; must be one of %s"
                % (strategy, ", ".join(sorted(_MATCH_STRATEGIES)))
            )
        if strategy == "exact_line_containing" and not (spec.get("match_key") or "").strip():
            raise SpecError("match strategy 'exact_line_containing' requires 'match_key'")

    if t == "command_exit_code" and "expected_exit_code" in spec:
        try:
            int(spec["expected_exit_code"])
        except (TypeError, ValueError):
            raise SpecError(
                "'expected_exit_code' must be an integer, got %r" % (spec["expected_exit_code"],)
            )

    if t == "file_exists" and "min_bytes" in spec and spec["min_bytes"] is not None:
        try:
            int(spec["min_bytes"])
        except (TypeError, ValueError):
            raise SpecError("'min_bytes' must be an integer, got %r" % (spec["min_bytes"],))

    return t


def build_raw_command(spec: dict) -> str:
    """Turn a spec into a literal shell command.

    Executed directly by the worker with NO model in the loop. This is
    deliberate: an earlier version generated a natural-language probe prompt
    and fed it back through the same LLM that produced the original claim,
    inheriting the exact reliability gap it was meant to catch -- in testing
    the model hallucinated a plausible `ls` failure for a file that genuinely
    existed, failing real work. A shell command has nothing to fabricate: it
    either runs and returns real output, or it does not run.
    """
    t = validate_spec(spec)

    if t == "file_exists":
        return "ls -la %s" % spec["path"]

    if t == "file_checksum":
        # Byte-for-byte confirmation, not just presence/size. Nothing generates
        # the expected hash except the caller's own pre-computed value, so a
        # fabricated write cannot accidentally satisfy it.
        return "sha256sum %s 2>&1" % spec["path"]

    if t in ("command_output_contains", "agent_result_matches_probe"):
        # Same probe; they differ only in how the output is judged.
        return spec["command"]

    if t == "command_exit_code":
        return "%s; echo PROBE_EXIT_CODE:$?" % spec["command"]

    raise SpecError("no command builder for type %r" % t)  # unreachable


def evaluate_probe_result(
    spec: dict, probe_output: str, agent_result: str = ""
) -> tuple[bool, str]:
    """Judge probe output against the spec. Returns (passed, detail).

    agent_result is the original task's claimed result. Only
    agent_result_matches_probe uses it -- that is the one type checking the
    agent's claim against ground truth rather than checking ground truth alone.

    Never raises: this is called per-task from the sweeper, and one malformed
    spec must not take down the loop. Invalid specs resolve to (False, why).
    """
    try:
        t = validate_spec(spec)
    except SpecError as exc:
        return False, "invalid verification_spec: %s" % exc

    probe_output = probe_output or ""

    # THE FAIL-CLOSED GUARD. Applies to every type, before any type-specific
    # logic can find a way to say yes. No output means no evidence.
    if not probe_output.strip():
        return False, (
            "probe produced no output, so nothing was verified. The probe "
            "likely never ran (worker died, command not found, or the result "
            "was never submitted). Treating this as a failure by design -- an "
            "absent result is not a passing result."
        )

    if t == "file_exists":
        path = spec["path"]
        for marker in _MISSING_FILE_MARKERS:
            if marker in probe_output:
                return False, "file not found at %s: %s" % (path, probe_output.strip())
        min_bytes = spec.get("min_bytes")
        if min_bytes is not None:
            match = _LS_LINE_RE.search(probe_output)
            if not match:
                return False, (
                    "could not parse a file size from probe output, so min_bytes "
                    "could not be checked: %s" % probe_output.strip()
                )
            size = int(match.group(1))
            if size < int(min_bytes):
                return False, (
                    "file exists but is smaller than expected (%d < %d bytes)"
                    % (size, int(min_bytes))
                )
        if not _LS_LINE_RE.search(probe_output):
            # Output that is neither a recognizable listing nor a known error
            # is not evidence of anything.
            return False, (
                "probe output is not a recognizable directory listing, so the "
                "file's existence is unconfirmed: %s" % probe_output.strip()
            )
        return True, "confirmed via probe: %s" % probe_output.strip()

    if t == "file_checksum":
        path = spec["path"]
        for marker in _MISSING_FILE_MARKERS:
            if marker in probe_output:
                return False, "file not found for checksum at %s: %s" % (
                    path,
                    probe_output.strip(),
                )
        expected = spec["sha256"].strip().lower()
        parts = probe_output.strip().split()  # "<hash>  <filename>"
        actual = parts[0].lower() if parts else ""
        if actual == expected:
            return True, "checksum confirmed: %s" % actual
        return False, (
            "checksum mismatch at %s: expected %s, got %r (raw probe output: %r)"
            % (path, expected, actual, probe_output.strip())
        )

    if t == "command_output_contains":
        expected = spec["expected"]
        if expected in probe_output:
            return True, "probe output contained expected string: %s" % probe_output.strip()
        return False, "expected %r not found in probe output: %s" % (
            expected,
            probe_output.strip(),
        )

    if t == "agent_result_matches_probe":
        # Ground truth (probe_output) is trustworthy -- a direct shell probe
        # with no model in the loop. What is under test is whether the AGENT's
        # claim matches it, catching the case a substring check would
        # false-pass: a fabricated `df` table still contains a "/".
        strategy = spec.get("match", "exact_string")
        agent_result = (agent_result or "").strip()
        probe_clean = probe_output.strip()

        if not agent_result:
            return False, (
                "agent claimed no result, so there is nothing to compare against "
                "the probe's output: %r" % probe_clean
            )

        if strategy == "exact_string":
            if agent_result == probe_clean:
                return True, "match confirmed: %s" % probe_clean
            return False, "probe: %r | agent claimed: %r" % (probe_clean, agent_result)

        # exact_line_containing
        match_key = spec["match_key"]
        probe_line = next(
            (l.strip() for l in probe_output.splitlines() if match_key in l), None
        )
        if probe_line is None:
            return False, "probe output had no line containing %r: %r" % (
                match_key,
                probe_clean,
            )
        agent_line = next(
            (l.strip() for l in agent_result.splitlines() if match_key in l), None
        )
        if agent_line is None:
            return False, (
                "agent result had no line containing %r | probe: %r | agent full "
                "result: %r" % (match_key, probe_line, agent_result)
            )
        if agent_line == probe_line:
            return True, "match confirmed: %s" % probe_line
        return False, "probe: %r | agent claimed: %r" % (probe_line, agent_line)

    if t == "command_exit_code":
        expected_code = int(spec.get("expected_exit_code", 0))
        match = _EXIT_CODE_RE.search(probe_output)
        if match is None:
            return False, (
                "probe output carried no PROBE_EXIT_CODE marker, so the exit "
                "code is unknown: %s" % probe_output.strip()
            )
        # Parsed as an integer, not substring-matched: "PROBE_EXIT_CODE:1" is a
        # substring of "PROBE_EXIT_CODE:10", which used to pass.
        actual_code = int(match.group(1))
        if actual_code == expected_code:
            return True, "probe confirmed exit code %d" % expected_code
        return False, "expected exit code %d, probe reported %d" % (
            expected_code,
            actual_code,
        )

    return False, "no evaluator for type %r" % t  # unreachable
