#!/usr/bin/env bash
# A/B for the content-preserving fixture spike against a real agent.
# Control: shape-only replay. Treatment: --response-fixture.
# Same wheel, same corpus, same authored task and oracle; fresh experiment dirs.
set -u
D=WORKSPACE
DRIFTER=drifter
cd "$D"

run_arm() {
  local label="$1" task="$2"; shift 2
  echo "===== ARM $label (task $task) ====="
  timeout 2700 "$DRIFTER" run --fixture .drifter/runs --server filesystem \
    --task-id "$task" --operator description_update --repeats 4 --yes "$@" 2>&1
  echo "===== END $label ====="
}

run_arm off fx-off
run_arm on fx-on --response-fixture responses.yaml
echo AB_COMPLETE
