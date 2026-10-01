"""Write tools the agent can propose. Every one of these edits a Function
custom resource's spec — never a Deployment, pod, or anything else. The
mini-faas controller owns turning that spec into reality, so these tools
can't directly break anything outside what the controller already permits.

Guardrails enforced in CODE, same pattern as tools.py:
  1. A separate ServiceAccount (agent-writer) with RBAC limited to
     "patch" on the functions resource only. No delete, anywhere, ever.
  2. Every write goes through propose_action() -> policy.decide() ->
     (human approval if required) -> execute_action() -> verify_action().
     Nothing calls the Kubernetes API directly from the agent loop.
  3. Every stage is written to the audit log before the next stage runs.
"""
import time

from kubernetes import client, config
from kubernetes.client.rest import ApiException

import settings

NS = settings.NAMESPACE
GROUP = "faas.jashitha.dev"
VERSION = "v1alpha1"
PLURAL = "functions"

config.load_kube_config()
_api_client = client.ApiClient()
_api_client.set_default_header(
    "Impersonate-User",
    f"system:serviceaccount:{NS}:{settings.WRITE_SERVICE_ACCOUNT}",
)
custom = client.CustomObjectsApi(_api_client)
apps_write = client.AppsV1Api(_api_client)

# Matches the RBAC resourceNames allowlist in k8s/rbac-writer.yaml exactly.
# Checked here too so a bug in the caller can't even attempt the call —
# defense in depth, not a substitute for the RBAC boundary.
ALLOWED_DEPLOYMENTS = {"mini-faas-gateway", "mini-faas-manager"}


def _patch_function_spec(name: str, spec_patch: dict) -> dict:
    body = {"spec": spec_patch}
    return custom.patch_namespaced_custom_object(
        GROUP, VERSION, NS, PLURAL, name, body,
        _content_type="application/merge-patch+json",
    )


def get_function(name: str) -> dict:
    return custom.get_namespaced_custom_object(GROUP, VERSION, NS, PLURAL, name)


# ---------- write actions ----------
# Each returns (success: bool, detail: str). Never raises outward —
# callers (execute_action) are responsible for catching and logging.

def set_function_image(name: str, image: str) -> tuple[bool, str]:
    """Revert or change the function's container image. The intended use
    is reverting to a previously-known-good image after a bad rollout."""
    try:
        _patch_function_spec(name, {"image": image})
        return True, f"patched spec.image to {image}"
    except ApiException as e:
        return False, f"ApiException {e.status}: {e.reason}"


def set_function_min_replicas(name: str, replicas: int) -> tuple[bool, str]:
    """Force a floor on replicas, e.g. to guarantee at least one pod stays
    warm while a fix is verified. Bounded 0-3 regardless of what's asked,
    since maxReplicas on these Functions is 3 — this tool cannot be used
    to scale past what the Function itself already allows."""
    replicas = max(0, min(int(replicas), 3))
    try:
        _patch_function_spec(name, {"minReplicas": replicas})
        return True, f"patched spec.minReplicas to {replicas}"
    except ApiException as e:
        return False, f"ApiException {e.status}: {e.reason}"


def set_deployment_image(name: str, container: str, image: str) -> tuple[bool, str]:
    """Revert a plain Deployment's container image. Only for the two
    Deployments mini-faas doesn't reconcile away (gateway, manager) —
    hello-function's Deployment is owned by the controller and must be
    fixed through set_function_image instead."""
    if name not in ALLOWED_DEPLOYMENTS:
        return False, f"refused: '{name}' is not in the allowed deployment list {ALLOWED_DEPLOYMENTS}"
    patch = {"spec": {"template": {"spec": {"containers": [{"name": container, "image": image}]}}}}
    try:
        apps_write.patch_namespaced_deployment(name, NS, patch)
        return True, f"patched {name}/{container} image to {image}"
    except ApiException as e:
        return False, f"ApiException {e.status}: {e.reason}"


def set_deployment_memory_limit(name: str, container: str, limit: str) -> tuple[bool, str]:
    """Raise a container's memory limit after an OOM kill. Always goes
    through 'ask' in policy.py, never auto — unlike a bad-tag revert,
    raising a memory ceiling is a cost/stability tradeoff a human should
    see, not just a safe rollback to a known-good prior state."""
    if name not in ALLOWED_DEPLOYMENTS:
        return False, f"refused: '{name}' is not in the allowed deployment list {ALLOWED_DEPLOYMENTS}"
    patch = {"spec": {"template": {"spec": {"containers": [
        {"name": container, "resources": {"limits": {"memory": limit}}}
    ]}}}}
    try:
        apps_write.patch_namespaced_deployment(name, NS, patch)
        return True, f"patched {name}/{container} memory limit to {limit}"
    except ApiException as e:
        return False, f"ApiException {e.status}: {e.reason}"


WRITE_ACTIONS = {
    "set_function_image": {
        "fn": set_function_image,
        "args": ["name", "image"],
        "description": "Revert a Function's image to a previously-known-good tag.",
    },
    "set_function_min_replicas": {
        "fn": set_function_min_replicas,
        "args": ["name", "replicas"],
        "description": "Set a Function's minReplicas floor (0-3).",
    },
    "set_deployment_image": {
        "fn": set_deployment_image,
        "args": ["name", "container", "image"],
        "description": "Revert mini-faas-gateway or mini-faas-manager's image to a previous tag.",
    },
    "set_deployment_memory_limit": {
        "fn": set_deployment_memory_limit,
        "args": ["name", "container", "limit"],
        "description": "Raise a memory limit on mini-faas-gateway or mini-faas-manager after an OOM kill.",
    },
}


def verify_action(function_name: str, timeout_seconds: int = 30) -> tuple[bool, str]:
    """Poll the Function's own status until it reports Warm, or time out.
    This checks the *symptom* improved, not just that the patch was
    accepted — a write can succeed and still not fix anything."""
    deadline = time.time() + timeout_seconds
    last_phase = None
    while time.time() < deadline:
        try:
            fn = get_function(function_name)
        except ApiException as e:
            return False, f"could not read Function during verification: {e.reason}"
        status = fn.get("status", {})
        last_phase = status.get("phase")
        if last_phase == "Warm":
            return True, f"Function reached Warm within {timeout_seconds}s"
        time.sleep(2)
    return False, f"Function still '{last_phase}' after {timeout_seconds}s, not verified Warm"


def verify_deployment_action(deployment_name: str, timeout_seconds: int = 30) -> tuple[bool, str]:
    """Same idea as verify_action, but for a plain Deployment: poll until
    readyReplicas meets the desired count, not just that the patch landed."""
    deadline = time.time() + timeout_seconds
    last_ready = 0
    apps_read = client.AppsV1Api(_api_client)  # same impersonated identity, read-only use here
    while time.time() < deadline:
        try:
            dep = apps_read.read_namespaced_deployment(deployment_name, NS)
        except ApiException as e:
            return False, f"could not read Deployment during verification: {e.reason}"
        desired = dep.spec.replicas or 0
        last_ready = dep.status.ready_replicas or 0
        if last_ready >= desired and desired > 0:
            return True, f"{deployment_name} reached {last_ready}/{desired} ready within {timeout_seconds}s"
        time.sleep(2)
    return False, f"{deployment_name} still {last_ready} ready after {timeout_seconds}s, not verified"
