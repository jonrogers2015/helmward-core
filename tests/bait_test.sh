#!/bin/sh
# Deterministic bait test for capability-based claiming.
#
# Proves two things about the live control plane, not from memory of how
# dispatch used to work, but from the actual running system:
#   1. A task whose capability no agent has declared is NEVER claimed. There
#      is no fallback assignment, no wildcard match -- capability filtering
#      is real, not cosmetic.
#   2. A task whose capability a real, online agent has DOES get claimed
#      within one poll cycle. Claiming is pull-based and actually works, not
#      just "nothing crashes when you post a task".
#
# This is the successor to the manual bait test described in the
# dispatch-race notes (assign_task / _pick_agent / PULL_CAPABILITIES), which
# tested a bug that NATS removal made structurally impossible. This version
# tests the CURRENT pull-only system, not the old one.
#
# Poll interval is a fixed `sleep 20` in agentos-poller.sh (confirmed by
# reading the source, not assumed) -- WAIT_SECONDS below is set well above
# that so a real claim has time to land without the test being flaky.

set -eu

BASE="${HELMWARD_BASE:-http://127.0.0.1:8080}"
REAL_CAPABILITY="${1:-apex-real}"
WAIT_SECONDS=30
NONCE="$(date +%s)-$$"
BOGUS_CAPABILITY="bait-nonexistent-${NONCE}"

echo "=== bait test: capability-based claiming ==="
echo "base: $BASE"
echo "bogus capability: $BOGUS_CAPABILITY (nobody should ever claim this)"
echo "real capability:  $REAL_CAPABILITY (an online agent should claim this)"

create_task() {
    # $1 = capability, $2 = prompt text
    curl -s -X POST "$BASE/api/tasks" \
        -H "Content-Type: application/json" \
        -d "{\"capability\":\"$1\",\"payload\":{\"prompt\":\"$2\"}}" \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])"
}

get_status() {
    curl -s "$BASE/api/tasks/$1" | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])"
}

BOGUS_ID=$(create_task "$BOGUS_CAPABILITY" "bait task -- must never be claimed")
REAL_ID=$(create_task "$REAL_CAPABILITY" "echo bait-real-ok")

echo "bogus task: $BOGUS_ID"
echo "real task:  $REAL_ID"
echo "waiting ${WAIT_SECONDS}s (poll interval is 20s, confirmed from agentos-poller.sh)..."
sleep "$WAIT_SECONDS"

BOGUS_STATUS=$(get_status "$BOGUS_ID")
REAL_STATUS=$(get_status "$REAL_ID")

echo "bogus status: $BOGUS_STATUS"
echo "real status:  $REAL_STATUS"

FAIL=0

if [ "$BOGUS_STATUS" != "queued" ]; then
    echo "FAIL: bait task (capability=$BOGUS_CAPABILITY) left 'queued' -- status is '$BOGUS_STATUS'. Something claimed work it had no declared capability for."
    FAIL=1
fi

if [ "$REAL_STATUS" = "queued" ]; then
    echo "FAIL: real task (capability=$REAL_CAPABILITY) is still 'queued' after ${WAIT_SECONDS}s. Claiming did not happen -- either no agent with this capability is online, or the poller is broken."
    FAIL=1
fi

if [ "$FAIL" -eq 0 ]; then
    echo "BAITTEST-OK"
    exit 0
else
    echo "BAITTEST-FAIL"
    exit 1
fi
