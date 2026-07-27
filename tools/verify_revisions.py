#!/usr/bin/env python3
"""Verify the revision registry and stamp the results back into it.

    python3 tools/verify_revisions.py                 # verify, stamp, write
    python3 tools/verify_revisions.py --dry-run       # verify, print, write nothing
    python3 tools/verify_revisions.py --check         # no probes; judge committed data

The registry claims that capability lines have shipped at given revisions. This
runs each line's acceptance spec and records what actually happened, so the
public site renders from measurements rather than from prose that drifts.

WHY --check EXISTS AND WHAT IT DOES NOT DO

The site builds on Netlify, which cannot reach the control plane. So the live
probes and the build gate are necessarily different checks:

    default mode   runs on the box with the LAN. Executes probes, stamps
                   verdict + last_verified + renderable.
    --check        runs anywhere, executes nothing. Judges only the committed
                   data: is every verdict present, is every timestamp fresh,
                   is every rendered line entitled to render.

--check therefore verifies a RECORD, not a system. It answers "was this
checked recently and did it pass", never "is this true right now". That
distinction is the honest content of the freshness window, and stating it is
the point: a verified fact has a shelf life.

FAIL CLOSED, EVERYWHERE

Every ambiguous outcome resolves to not-renderable:
  - no acceptance spec            -> no verdict, cannot render
  - invalid spec                  -> failed (attested treats this as a failure,
                                     not a crash, and so do we)
  - regressed line                -> cannot render regardless of history
  - unsigned or stale assertion   -> cannot render
  - attested not importable       -> exit 2, write NOTHING

The last one matters most. A verifier that cannot verify must not leave stale
`passed` verdicts sitting in a file the site trusts.

COVERAGE

The audit states its denominator. Every line gets a verdict entry, the count is
asserted against the number of lines found, and a mismatch is a hard failure.
An audit that silently skipped a line is the failure mode this rule exists for.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

try:
    from attested import verify
except ImportError:
    sys.stderr.write(
        "FATAL: attested is not installed, so nothing can be verified.\n"
        "       pip install attested\n"
        "       Refusing to write: stale verdicts must not survive a broken verifier.\n"
    )
    raise SystemExit(2)

DEFAULT_REGISTRY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "revisions.json"
)

PROBE_TIMEOUT = 120.0  # pytest runs live here, so 60s is not enough


# ----------------------------------------------------------------------- helpers


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str | None) -> datetime | None:
    """Parse a stamped timestamp. Unparseable is treated as absent, not as an
    error -- a timestamp we cannot read is a timestamp we cannot trust."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _fresh(stamp: str | None, window_days: int, now: datetime) -> bool:
    dt = _parse_iso(stamp)
    if dt is None:
        return False
    return (now - dt) <= timedelta(days=window_days)


# ------------------------------------------------------------------- per-line ops


def judge_asserted(line: dict, window_days: int, now: datetime) -> tuple[str, str, bool]:
    """An asserted line is a human's signature on a claim no probe can check.

    It still expires. A signature with no date, or an old one, does not entitle
    the claim to appear on the site -- otherwise 'asserted' is just an exemption
    with better manners.
    """
    if not line.get("asserted_by"):
        return "unsigned", "no asserted_by: an assertion needs a name behind it", False
    if not _fresh(line.get("asserted_on"), window_days, now):
        stamp = line.get("asserted_on") or "never"
        return (
            "stale_assertion",
            "asserted_on is %s, outside the %d-day assertion window" % (stamp, window_days),
            False,
        )
    return "asserted", "signed by %s on %s" % (line["asserted_by"], line["asserted_on"]), True


def run_probe(line: dict) -> tuple[str, str, bool, str]:
    """Execute one line's acceptance spec. Returns (verdict, detail, renderable, cmd)."""
    spec = line.get("acceptance")
    if not spec:
        return (
            None,
            "no acceptance spec: %s" % line.get("acceptance_blocked_on", "reason not recorded"),
            False,
            "",
        )

    # A placeholder is not a spec. These exist in the registry on purpose, to
    # mark work as blocked rather than to be silently executed.
    flat = json.dumps(spec)
    if "CONFIRM-" in flat:
        return None, "spec contains an unresolved placeholder; not executed", False, ""

    from attested import local  # imported here so --check never needs an executor

    result = verify(spec, executor=local(timeout=PROBE_TIMEOUT))
    verdict = "passed" if result.passed else "failed"
    return verdict, result.detail, bool(result.passed), result.command


# ------------------------------------------------------------------------ registry


def load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save(path: str, data: dict) -> None:
    """Atomic write. A half-written registry is a registry the site would read."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".revisions-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def verify_registry(data: dict, now: datetime) -> list[dict]:
    """Run every line. Returns one report row per line, in registry order."""
    assertion_window = int(data.get("assertion_staleness_days", 90))
    rows = []

    for line in data["lines"]:
        cls = line.get("verification_class")

        if line.get("state") == "regressed":
            # Honest by construction: there is nothing to verify about a
            # capability that has stopped working, and history does not
            # entitle it to render.
            verdict, detail, renderable, command = (
                "regressed",
                "line is regressed; not eligible to render",
                False,
                "",
            )
        elif cls == "asserted":
            verdict, detail, renderable = judge_asserted(line, assertion_window, now)
            command = ""
        elif cls == "probe":
            verdict, detail, renderable, command = run_probe(line)
        else:
            verdict, detail, renderable, command = (
                None,
                "unknown verification_class %r" % cls,
                False,
                "",
            )

        line["verdict"] = verdict
        line["renderable"] = renderable
        line["verdict_detail"] = detail
        if cls == "probe" and verdict in ("passed", "failed"):
            line["last_verified"] = _iso(now)
            line["last_probe_command"] = command

        rows.append(
            {
                "id": line["id"],
                "class": cls,
                "verdict": verdict,
                "renderable": renderable,
                "detail": detail,
            }
        )

    return rows


def check_committed(data: dict, now: datetime) -> list[dict]:
    """Build-time gate. Executes nothing; judges the committed record only."""
    probe_window = int(data.get("staleness_days", 14))
    assertion_window = int(data.get("assertion_staleness_days", 90))
    rows = []

    for line in data["lines"]:
        cls = line.get("verification_class")
        entitled = False
        why = ""

        # Entitlement is RECOMPUTED from the evidence, never read from the
        # stored `renderable` flag. That flag lives in a committed, hand-editable
        # file: trusting it would let anyone grant a line the right to render by
        # typing `true`, with no passing verdict behind it. The stored flag is a
        # claim like any other, so it is checked below rather than believed.
        if line.get("state") == "regressed":
            why = "line is regressed"
        elif cls == "probe":
            if line.get("verdict") != "passed":
                why = "verdict is %r, not 'passed'" % line.get("verdict")
            elif not _fresh(line.get("last_verified"), probe_window, now):
                why = "last_verified %s is outside the %d-day window" % (
                    line.get("last_verified") or "never",
                    probe_window,
                )
            else:
                entitled = True
        elif cls == "asserted":
            _verdict, why_a, ok = judge_asserted(line, assertion_window, now)
            entitled, why = ok, "" if ok else why_a
        else:
            why = "unknown verification_class %r" % cls

        # Tamper check: a stored flag that disagrees with the recomputed answer
        # means the file was edited by hand or by something that skipped the
        # verifier. Either way the discrepancy itself is the finding.
        stored = bool(line.get("renderable"))
        if stored != entitled:
            why = "MISMATCH: stored renderable=%s, evidence says %s (%s)" % (
                stored,
                entitled,
                why or "entitled",
            )
            entitled = False

        rows.append(
            {
                "id": line["id"],
                "class": cls,
                "verdict": line.get("verdict"),
                "renderable": entitled,
                "detail": why or "entitled to render",
            }
        )

    return rows


# ---------------------------------------------------------------------- reporting


def report(rows: list[dict], total_lines: int) -> int:
    """Print the table, assert coverage, return the exit code."""
    width = max(len(r["id"]) for r in rows) if rows else 0
    for r in rows:
        flag = "RENDER" if r["renderable"] else "hold  "
        print(
            "%s  %-*s  %-8s  %-16s  %s"
            % (flag, width, r["id"], r["class"] or "?", r["verdict"] or "none", r["detail"])
        )

    # Coverage: the audit states its denominator.
    if len(rows) != total_lines:
        print(
            "\nCOVERAGE FAILURE: %d lines in registry, %d verdicts produced"
            % (total_lines, len(rows)),
            file=sys.stderr,
        )
        return 1

    failed = [r["id"] for r in rows if r["verdict"] == "failed"]
    renderable = sum(1 for r in rows if r["renderable"])
    print(
        "\n%d lines, %d verdicts, %d renderable, %d failed"
        % (total_lines, len(rows), renderable, len(failed))
    )
    if failed:
        print("FAILED: %s" % ", ".join(failed), file=sys.stderr)
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--registry", default=DEFAULT_REGISTRY)
    ap.add_argument("--dry-run", action="store_true", help="verify but do not write")
    ap.add_argument("--check", action="store_true", help="judge committed data; run no probes")
    args = ap.parse_args()

    now = _now()
    data = load(args.registry)
    total = len(data["lines"])

    if args.check:
        rows = check_committed(data, now)
        stamped = _parse_iso(data.get("generated_at"))
        if stamped is None:
            print("WARNING: registry has no generated_at stamp", file=sys.stderr)
        return report(rows, total)

    rows = verify_registry(data, now)
    data["generated_at"] = _iso(now)

    if not args.dry_run:
        save(args.registry, data)

    return report(rows, total)


if __name__ == "__main__":
    raise SystemExit(main())
