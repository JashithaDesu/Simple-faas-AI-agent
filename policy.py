"""The policy layer: the one place that decides whether a proposed write
action runs on its own, needs a human to approve it, or is refused
outright. Kept as explicit rules, not model judgment — the model proposes,
this module (and only this module) decides what's allowed to happen
automatically.

Three outcomes, returned as a string:
  "auto"  - execute without asking a human
  "ask"   - show the human the proposed action, require explicit yes
  "deny"  - never run this, regardless of confidence or who asks
"""

# Actions in this set can never run automatically, no matter how confident
# the diagnosis is.
#   - set_function_min_replicas: scaling down risks dropping in-flight
#     traffic in a way that's hard to verify was actually safe.
#   - set_deployment_memory_limit: raising a memory ceiling is a
#     cost/stability tradeoff, not a rollback to a known-good state — a
#     bad-tag revert undoes a mistake; a memory bump changes a resource
#     commitment and can mask a real leak. A human sees every one of these.
NEVER_AUTO = {"set_function_min_replicas", "set_deployment_memory_limit"}

# Diagnosis categories where even a correct-looking fix is risky enough
# that a human should see it first before anything executes. A stuck
# rollout can have more than one real cause behind the same symptom, so
# it never auto-executes regardless of confidence.
LOW_TRUST_CATEGORIES = {"unknown", "rollout_failure"}

# Actions this policy has an actual rule for. Anything else is denied,
# not guessed at — a new write tool is unusable until it's given an
# explicit rule here.
KNOWN_ACTIONS = {
    "set_function_image", "set_deployment_image",
    "set_deployment_memory_limit", "set_function_min_replicas",
}

# Image-revert actions (on either a Function or a plain Deployment) share
# one rule: only auto-execute when confidence is high AND the category is
# specifically the one this fix is known to address.
IMAGE_REVERT_ACTIONS = {"set_function_image", "set_deployment_image"}


def decide(action_name: str, diagnosis: dict) -> str:
    if action_name not in KNOWN_ACTIONS:
        return "deny"

    confidence = diagnosis.get("confidence", "low")
    category = diagnosis.get("category", "unknown")

    if action_name in NEVER_AUTO:
        return "ask"

    if category in LOW_TRUST_CATEGORIES:
        return "ask"

    if action_name in IMAGE_REVERT_ACTIONS:
        if confidence == "high" and category == "image_pull_error":
            return "auto"
        return "ask"

    return "ask"
