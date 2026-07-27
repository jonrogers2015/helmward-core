#!/usr/bin/env python3
"""
Verified Memory 0.2 -- checks every wiki page automatically.

Not "does this page exist" (0.1 already does that via list_wiki_pages).
This is: for the pages that carry a claim, is the claim STILL TRUE right
now, or has reality moved and the wiki hasn't caught up?

Coverage discipline (the same lesson the blog audit learned the hard
way): enumerate every page from disk, require a verdict for every page,
and fail loudly if verdict count != page count. A page silently skipped
is indistinguishable from a page silently passing.

Verdicts: verified | stale | no_claims
Exit code: 0 if no page is `stale`, 1 if any page is `stale`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "/root/attested")
from attested import verify
from attested.templates import safe_command_exit_code, safe_file_exists

WIKI_BASE = Path("/opt/helmward/wiki")
CLAIMS_FILE = WIKI_BASE / "_claims.json"


def enumerate_pages() -> list[str]:
    """Every .md file under the wiki base, as paths relative to it."""
    pages = []
    for p in WIKI_BASE.rglob("*.md"):
        pages.append(str(p.relative_to(WIKI_BASE)))
    return sorted(pages)


def load_claims() -> dict[str, list[dict]]:
    """Group claims by page. A page with no entry gets an empty list."""
    if not CLAIMS_FILE.exists():
        return {}
    data = json.loads(CLAIMS_FILE.read_text())
    by_page: dict[str, list[dict]] = {}
    for claim in data.get("claims", []):
        by_page.setdefault(claim["page"], []).append(claim)
    return by_page


def build_spec(claim: dict) -> dict:
    spec_type = claim["spec_type"]
    if spec_type == "command_exit_code":
        return safe_command_exit_code(claim["command"], expected=claim.get("expected", 0))
    elif spec_type == "file_exists":
        return safe_file_exists(claim["path"], min_bytes=claim.get("min_bytes", 1))
    else:
        raise ValueError(f"unsupported spec_type in claims registry: {spec_type!r}")


def main() -> int:
    pages = enumerate_pages()
    claims_by_page = load_claims()

    verdicts = {}

    for page in pages:
        page_claims = claims_by_page.get(page, [])
        if not page_claims:
            verdicts[page] = ("no_claims", "page carries no registered claim")
            continue

        page_passed = True
        details = []
        for claim in page_claims:
            spec = build_spec(claim)
            result = verify(spec)
            details.append(f"{claim['label']}: {'OK' if result else 'STALE -- ' + result.detail}")
            if not result:
                page_passed = False

        verdicts[page] = ("verified" if page_passed else "stale", "; ".join(details))

    # Coverage assertion: every enumerated page has a verdict, no more, no less.
    assert set(verdicts.keys()) == set(pages), "verdict/page set mismatch -- coverage bug"
    assert len(verdicts) == len(pages), "verdict count != page count -- coverage bug"

    print(f"{'PAGE':<45} {'VERDICT':<12} DETAIL")
    print("-" * 100)
    stale_count = 0
    for page in pages:
        verdict, detail = verdicts[page]
        if verdict == "stale":
            stale_count += 1
        print(f"{page:<45} {verdict:<12} {detail}")

    print()
    print(f"{len(pages)} pages, {len(verdicts)} verdicts, "
          f"{sum(1 for v, _ in verdicts.values() if v == 'verified')} verified, "
          f"{stale_count} stale, "
          f"{sum(1 for v, _ in verdicts.values() if v == 'no_claims')} no_claims")

    return 1 if stale_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
