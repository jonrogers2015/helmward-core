#!/usr/bin/env python3
"""
Content Pipeline 0.2 -- gate a generated blog post before it can be published.

The rule: a generated post carries claims in a central registry (data,
not per-post frontmatter edits -- same lesson as the wiki claims
registry). Every claim must have a real, non-vacuous verification spec
and must actually pass. A post with zero registered claims cannot be
gated automatically and is refused, not silently allowed through.

If every claim passes: instead of auto-publishing, this creates a task
in `needs_approval` status and a linked Approval row, so a human
(Jon) sees it on the Approvals page and has to explicitly approve
before anything goes live. Approving requeues the task; a follow-up
agent run then does the actual publish (flip draft:false, git commit,
push). Unlike v1, the publish task itself now carries a
verification_spec -- once the agent claims and reports done, the
control plane's OWN dispatch loop independently re-checks the real
end state (draft:false landed, the right commit exists, HEAD matches
origin) before the task is trusted as complete. This closes the gap
where a fabricated "yes I pushed it" self-report would otherwise have
been the only evidence -- exactly the failure mode this whole system
exists to catch.

If any claim fails: exits non-zero, prints exactly which claim and
why, touches nothing else. The post stays draft:true.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "/root/attested")
sys.path.insert(0, "/root/helmward-core/control-plane")

from attested import verify
from attested.templates import safe_command_output_contains, safe_command_exit_code

from app import db  # control-plane's own db module -- same process, no HTTP race

SITE_DIR = Path("/mnt/agent-os/helmward-site")
SITE_BLOG_DIR = SITE_DIR / "src/content/blog"
CLAIMS_FILE = SITE_BLOG_DIR / "_claims.json"


def load_claims_for_slug(slug: str) -> list[dict]:
    if not CLAIMS_FILE.exists():
        return []
    data = json.loads(CLAIMS_FILE.read_text())
    return [c for c in data.get("claims", []) if c["slug"] == slug]


def build_spec(claim: dict) -> dict:
    spec_type = claim["spec_type"]
    if spec_type == "command_output_contains":
        return safe_command_output_contains(claim["command"], expected=claim["expected"])
    elif spec_type == "command_exit_code":
        return safe_command_exit_code(claim["command"], expected=claim.get("expected", 0))
    else:
        raise ValueError(f"unsupported spec_type in blog claims registry: {spec_type!r}")


def build_publish_verification_spec(slug: str) -> dict:
    """
    Ground-truth check for the publish step, run independently by the
    control plane's dispatch loop -- NOT trusted from the agent's own
    report. All four conditions must hold in one shell pipeline so a
    partial success (e.g. committed but not pushed, or pushed but
    draft never flipped) cannot slip through:
      1. draft: false actually landed in the post's frontmatter
      2. a commit exists whose message names this exact publish
      3. that commit is the one HEAD currently points at (fetched fresh)
      4. local HEAD matches origin/main -- i.e. it was actually pushed
    """
    check_cmd = (
        f"cd {SITE_DIR} && "
        f"grep -q '^draft: false' src/content/blog/{slug}.md && "
        f"git log -1 --format=%s -- src/content/blog/{slug}.md | grep -q 'publish: {slug}' && "
        f"git fetch origin main -q && "
        f'[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] && '
        f"echo PUBLISH_VERIFIED_OK"
    )
    return safe_command_output_contains(check_cmd, expected="PUBLISH_VERIFIED_OK")


def gate_post(slug: str) -> int:
    post_path = SITE_BLOG_DIR / f"{slug}.md"
    if not post_path.exists():
        print(f"REFUSED: no post found at {post_path}")
        return 1

    claims = load_claims_for_slug(slug)
    if not claims:
        print(f"REFUSED: {slug} has zero registered claims -- cannot be gated automatically. "
              f"Register at least one real claim in {CLAIMS_FILE} before this post can be published.")
        return 1

    print(f"Checking {len(claims)} claim(s) for '{slug}':")
    all_passed = True
    details = []
    for claim in claims:
        spec = build_spec(claim)
        result = verify(spec)
        status = "PASS" if result else "FAIL"
        print(f"  [{status}] {claim['label']}")
        if not result:
            print(f"         reason: {result.detail}")
            all_passed = False
        details.append(f"{claim['label']}: {'OK' if result else 'FAILED -- ' + result.detail}")

    if not all_passed:
        print(f"\nGATE FAILED for '{slug}' -- not requesting approval. Post stays draft:true.")
        return 1

    print(f"\nAll claims passed for '{slug}'. Creating publish task + approval request...")

    publish_spec = build_publish_verification_spec(slug)

    task = db.create_task(
        capability="apex-real",
        payload={
            "prompt": (
                f"Publish blog post '{slug}': in {SITE_DIR}, "
                f"edit src/content/blog/{slug}.md to set draft: false in the "
                f"frontmatter (leave everything else unchanged), then "
                f"git add, commit with message 'publish: {slug}', and git push."
            )
        },
        idempotency_key=f"publish-{slug}",
        verification_spec=publish_spec,
        initial_status="needs_approval",
    )
    task_id = task["id"] if isinstance(task, dict) else task

    approval = db.create_approval(
        task_id=task_id,
        action=f"Publish blog post: {slug}",
        details="; ".join(details),
    )

    print(f"Task {task_id} set to needs_approval, verification_spec attached "
          f"(will independently check draft:false + commit + push once approved).")
    print(f"Approval {approval.get('id') if isinstance(approval, dict) else approval} created.")
    print("This will now appear on the Approvals page.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: gate_blog_post.py <slug>")
        sys.exit(2)
    sys.exit(gate_post(sys.argv[1]))
