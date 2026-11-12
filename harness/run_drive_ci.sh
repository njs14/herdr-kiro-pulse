#!/usr/bin/env bash
# Run the verify-herdr-kiro stand-in recipes. Needs herdr 0.9.5 on PATH.
# No Kiro API key. Not a live App check.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DRIVE=(python3 .cursor/skills/verify-herdr-kiro/drive.py)
export VERIFY_RUN="${VERIFY_RUN:-$RUNNER_TEMP/herdr-kiro-verify}"
export VERIFY_EVIDENCE="${VERIFY_EVIDENCE:-$RUNNER_TEMP/herdr-kiro-verify-evidence}"
if [[ -z "${RUNNER_TEMP:-}" ]]; then
  export VERIFY_RUN="${VERIFY_RUN:-/tmp/herdr-kiro-verify-ci}"
  export VERIFY_EVIDENCE="${VERIFY_EVIDENCE:-/tmp/herdr-kiro-verify-ci-evidence}"
fi

cleanup() {
  "${DRIVE[@]}" cleanup >/dev/null 2>&1 || true
}
trap cleanup EXIT

"${DRIVE[@]}" launch
"${DRIVE[@]}" doctor

# idle
"${DRIVE[@]}" wait idle live_prompt_idle 12

# working
"${DRIVE[@]}" paint working
"${DRIVE[@]}" wait working workflow_banner_working 8

# tool approval
"${DRIVE[@]}" paint approval
"${DRIVE[@]}" wait blocked workflow_requires_approval 8

# trust dialog
"${DRIVE[@]}" paint trust
"${DRIVE[@]}" wait blocked workspace_trust 8

# restore idle before finished-turn / release handoff
"${DRIVE[@]}" paint idle
"${DRIVE[@]}" wait idle live_prompt_idle 8

# finished turn (background) + release handoff
"${DRIVE[@]}" background
"${DRIVE[@]}" paint working
"${DRIVE[@]}" hook UserPromptSubmit
"${DRIVE[@]}" paint idle
"${DRIVE[@]}" hook Stop
sleep 1
"${DRIVE[@]}" paint working
"${DRIVE[@]}" hook UserPromptSubmit
"${DRIVE[@]}" status >/dev/null
"${DRIVE[@]}" paint idle
"${DRIVE[@]}" hook Stop
"${DRIVE[@]}" wait done idle live_prompt_idle 12

sleep 12
"${DRIVE[@]}" status --save release.json
python3 - <<'PY'
import json, os, sys

evidence = None
meta_path = os.path.join(os.environ["VERIFY_RUN"], "run.json")
with open(meta_path, encoding="utf-8") as handle:
    evidence = json.load(handle)["evidence"]
path = os.path.join(evidence, "release.json")
with open(path, encoding="utf-8") as handle:
    data = json.load(handle)
if data.get("agent_status") != "done":
    sys.exit("release.json agent_status is not done: " + repr(data.get("agent_status")))
if data.get("explain_state") != "idle":
    sys.exit("release.json explain_state is not idle")
if data.get("explain_rule") != "live_prompt_idle":
    sys.exit("release.json explain_rule is not live_prompt_idle")
log_path = os.path.join(evidence, "hook.log")
with open(log_path, encoding="utf-8") as handle:
    text = handle.read()
if "release-agent" in text:
    sys.exit("hook.log contains release-agent while kiro-cli stand-in is still running")
print(json.dumps({"release": "ok", "completion_seq": data.get("completion_seq")}))
PY

"${DRIVE[@]}" seen
"${DRIVE[@]}" wait idle live_prompt_idle 8
"${DRIVE[@]}" status --save seen.json

python3 - <<'PY'
import json, os, sys

meta_path = os.path.join(os.environ["VERIFY_RUN"], "run.json")
with open(meta_path, encoding="utf-8") as handle:
    evidence = json.load(handle)["evidence"]
with open(os.path.join(evidence, "seen.json"), encoding="utf-8") as handle:
    data = json.load(handle)
if data.get("agent_status") != "idle":
    sys.exit("seen.json agent_status is not idle")
print(json.dumps({"drive_ci": "ok", "evidence": evidence}))
PY
