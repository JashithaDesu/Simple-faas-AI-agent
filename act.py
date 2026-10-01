"""Day 2: turn a diagnosis into a proposed write action, check it against
policy, get human approval if required, execute it, and verify the symptom
actually improved. Every stage is written to the audit log before the next
stage runs.

    python agent.py --scenario 1 > /tmp/diag.json   # Day 1: diagnose
    python act.py --function hello-function --diagnosis-file /tmp/diag.json

Kept separate from agent.py on purpose. Day 1's diagnose-only loop is
untouched and still usable by itself.
"""
import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import policy
import write_tools
from tools import _owned_replicasets, _revision

LOG_DIR = Path(__file__).parent / "logs"


def audit(run_id: str, event: str, **fields) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    record = {"ts": datetime.now(timezone.utc).isoformat(), "run_id": run_id,
              "event": event, **fields}
    with open(LOG_DIR / "audit.jsonl", "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def find_previous_image(function_name: str) -> str | None:
    """Code-determined, not model-determined: the image from the revision
    immediately before the current one. Returns None if there's no prior
    revision to revert to."""
    rss = sorted(_owned_replicasets(function_name), key=_revision, reverse=True)
    if len(rss) < 2:
        return None
    previous = rss[1]
    return previous.spec.template.spec.containers[0].image


# Default memory bump for an OOM fix — code-determined, not model-chosen,
# same philosophy as the image-revert logic below. A real system might
# compute this from observed usage; this is a fixed, documented default.
OOM_MEMORY_LIMIT = "256Mi"

# Container name inside each known Deployment, needed because the k8s API
# patches a specific container by name, not just "the deployment".
DEPLOYMENT_CONTAINERS = {"mini-faas-gateway": "gateway", "mini-faas-manager": "manager"}


def propose_action(target_kind: str, target_name: str, diagnosis: dict):
    """Returns (action_name, args) or None if this diagnosis category has
    no automated action defined for this target kind."""
    category = diagnosis.get("category")

    # Same underlying fix as image_pull_error — revert to the previous
    # working image. rollout_failure stays in LOW_TRUST_CATEGORIES in
    # policy.py, so this always routes to "ask", never "auto", even
    # though the mechanics of the fix are identical.
    if category in ("image_pull_error", "rollout_failure"):
        prev = find_previous_image(target_name)
        if prev is None:
            return None
        if target_kind == "function":
            return "set_function_image", {"name": target_name, "image": prev}
        container = DEPLOYMENT_CONTAINERS.get(target_name)
        if container is None:
            return None
        return "set_deployment_image", {"name": target_name, "container": container, "image": prev}

    if category == "oom_killed" and target_kind == "deployment":
        container = DEPLOYMENT_CONTAINERS.get(target_name)
        if container is None:
            return None
        return "set_deployment_memory_limit", {
            "name": target_name, "container": container, "limit": OOM_MEMORY_LIMIT,
        }

    return None


def run_action(target_name: str, diagnosis: dict, target_kind: str = "function",
                run_id: str | None = None, auto_yes: bool = False) -> dict:
    run_id = run_id or uuid.uuid4().hex[:8]
    proposal = propose_action(target_kind, target_name, diagnosis)

    if proposal is None:
        audit(run_id, "action_none", category=diagnosis.get("category"),
              reason="no automated action defined for this category, or no prior revision to revert to")
        print(f"No automated action available for category '{diagnosis.get('category')}'.")
        return {"run_id": run_id, "executed": False, "reason": "no_action_defined"}

    action_name, args = proposal
    decision = policy.decide(action_name, diagnosis)
    audit(run_id, "action_proposed", action=action_name, args=args,
          diagnosis_category=diagnosis.get("category"),
          diagnosis_confidence=diagnosis.get("confidence"), decision=decision)

    print(f"\nProposed action: {action_name}({args})")
    print(f"Policy decision: {decision}")

    if decision == "deny":
        audit(run_id, "action_denied", action=action_name)
        print("Denied by policy. Not executed.")
        return {"run_id": run_id, "executed": False, "reason": "policy_denied"}

    if decision == "ask" and not auto_yes:
        resp = input("Approve this action? [y/N] ").strip().lower()
        if resp != "y":
            audit(run_id, "action_declined", action=action_name, approver="human")
            print("Declined. Not executed.")
            return {"run_id": run_id, "executed": False, "reason": "human_declined"}
        audit(run_id, "action_approved", action=action_name, approver="human")
    elif decision == "ask" and auto_yes:
        # --auto-yes exists only for scripted evaluation runs, never for
        # real incidents. It is a separate, explicit opt-in per run, not
        # a default, precisely so "ask" can't silently become "auto".
        audit(run_id, "action_approved", action=action_name, approver="auto_yes_flag")
    else:
        audit(run_id, "action_approved", action=action_name, approver="policy_auto")

    fn = write_tools.WRITE_ACTIONS[action_name]["fn"]
    success, detail = fn(**args)
    audit(run_id, "action_executed", action=action_name, success=success, detail=detail)
    print(f"Executed: {detail}")

    if not success:
        return {"run_id": run_id, "executed": True, "succeeded": False, "detail": detail}

    if target_kind == "function":
        verified, verify_detail = write_tools.verify_action(target_name)
    else:
        verified, verify_detail = write_tools.verify_deployment_action(target_name)
    audit(run_id, "action_verified", verified=verified, detail=verify_detail)
    print(f"Verification: {'PASSED' if verified else 'FAILED'} - {verify_detail}")

    return {"run_id": run_id, "executed": True, "succeeded": success,
            "verified": verified, "detail": detail, "verify_detail": verify_detail}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Act on a diagnosis.")
    parser.add_argument("--function", required=True,
                        help="Name of the Function or Deployment to act on.")
    parser.add_argument("--target-kind", choices=["function", "deployment"], default="function",
                        help="'function' for hello-function (via the Function CR), "
                             "'deployment' for mini-faas-gateway/mini-faas-manager "
                             "(patched directly, since nothing reconciles them away).")
    parser.add_argument("--diagnosis-file", required=True,
                        help="Path to a JSON file containing the diagnosis "
                             "(the DIAGNOSIS block agent.py prints).")
    parser.add_argument("--auto-yes", action="store_true",
                        help="Skip interactive approval for 'ask' actions. "
                             "Only for scripted evaluation, never for real use.")
    a = parser.parse_args()

    with open(a.diagnosis_file) as f:
        diagnosis = json.load(f)

    result = run_action(a.function, diagnosis, target_kind=a.target_kind, auto_yes=a.auto_yes)
    print(f"\n{json.dumps(result, indent=2)}")
