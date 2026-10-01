"""Read-only tools the agent can call.

Guardrails enforced in CODE and RBAC, not in the prompt:
  1. Every Kubernetes call impersonates the agent-reader ServiceAccount, so the
     API server applies its read-only Role. Write attempts get 403 Forbidden.
  2. The namespace is fixed by settings; the model has no parameter to change it.
  3. Tool output is truncated so one huge log cannot flood the context window.
"""
import json

import requests
from kubernetes import client, config
from kubernetes.client.rest import ApiException

import settings

NS = settings.NAMESPACE

config.load_kube_config()
_api_client = client.ApiClient()
_api_client.set_default_header(
    "Impersonate-User", f"system:serviceaccount:{NS}:{settings.SERVICE_ACCOUNT}"
)
core = client.CoreV1Api(_api_client)
apps = client.AppsV1Api(_api_client)

REVISION_KEY = "deployment.kubernetes.io/revision"
CHANGE_CAUSE_KEY = "kubernetes.io/change-cause"


# ---------- helpers ----------

def _truncate(text: str) -> str:
    limit = settings.MAX_TOOL_OUTPUT_CHARS
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def _owner(meta) -> str:
    refs = meta.owner_references or []
    return f"{refs[0].kind}/{refs[0].name}" if refs else "none"


def _container_state(cs) -> str:
    s = cs.state
    if s.waiting:
        return f"waiting ({s.waiting.reason})"
    if s.terminated:
        return f"terminated ({s.terminated.reason}, exit {s.terminated.exit_code})"
    if s.running:
        return "running"
    return "unknown"


def _last_termination(cs) -> str:
    t = cs.last_state.terminated if cs.last_state else None
    return f"{t.reason}, exit {t.exit_code}" if t else "none"


def _probe(p):
    if not p:
        return None
    if p.http_get:
        return f"httpGet path={p.http_get.path} port={p.http_get.port}"
    if p.tcp_socket:
        return f"tcpSocket port={p.tcp_socket.port}"
    exec_action = getattr(p, "_exec", None)
    if exec_action:
        return f"exec {exec_action.command}"
    return "other"


def _revision(obj) -> int:
    return int((obj.metadata.annotations or {}).get(REVISION_KEY, "0"))


def _owned_replicasets(deployment_name: str):
    return [
        rs for rs in apps.list_namespaced_replica_set(NS).items
        if any(o.kind == "Deployment" and o.name == deployment_name
               for o in (rs.metadata.owner_references or []))
    ]


# ---------- tools ----------

def list_deployments() -> str:
    items = apps.list_namespaced_deployment(NS).items
    if not items:
        return f"No deployments in namespace {NS}."
    return "\n".join(
        f"deployment={d.metadata.name} desired={d.spec.replicas} "
        f"updated={d.status.updated_replicas or 0} ready={d.status.ready_replicas or 0} "
        f"available={d.status.available_replicas or 0}"
        for d in items
    )


def list_pods() -> str:
    pods = core.list_namespaced_pod(NS).items
    if not pods:
        return f"No pods in namespace {NS}."
    lines = []
    for p in pods:
        statuses = p.status.container_statuses or []
        if not statuses:
            lines.append(f"pod={p.metadata.name} phase={p.status.phase} owner={_owner(p.metadata)} "
                         f"(no container status yet)")
        for cs in statuses:
            lines.append(
                f"pod={p.metadata.name} owner={_owner(p.metadata)} phase={p.status.phase} "
                f"container={cs.name} image={cs.image} ready={cs.ready} "
                f"restarts={cs.restart_count} state={_container_state(cs)} "
                f"last_termination={_last_termination(cs)}"
            )
    return "\n".join(lines)


def get_pod_logs(pod_name: str, previous: bool = False, tail_lines: int = 50) -> str:
    tail_lines = max(1, min(int(tail_lines), 200))
    logs = core.read_namespaced_pod_log(
        pod_name, NS, previous=bool(previous), tail_lines=tail_lines
    )
    return logs or "(no log output)"


def get_events(object_name: str = "") -> str:
    events = core.list_namespaced_event(NS).items
    if object_name:
        events = [e for e in events if object_name in (e.involved_object.name or "")]
    if not events:
        return "No matching events."

    def ts(e):
        return e.last_timestamp or e.event_time or e.metadata.creation_timestamp

    events.sort(key=ts)
    return "\n".join(
        f"{ts(e):%H:%M:%S} {e.type} {e.reason} "
        f"{e.involved_object.kind}/{e.involved_object.name} (x{e.count or 1}): "
        f"{(e.message or '').strip()}"
        for e in events[-30:]
    )


def describe_deployment(name: str) -> str:
    d = apps.read_namespaced_deployment(name, NS)
    ann = d.metadata.annotations or {}
    containers = []
    for c in d.spec.template.spec.containers:
        env = [
            f"{e.name}={e.value}" if e.value_from is None else f"{e.name}=<valueFrom>"
            for e in (c.env or [])
        ]
        res = c.resources
        containers.append({
            "name": c.name,
            "image": c.image,
            "command": c.command,
            "args": c.args,
            "env": env,
            "requests": res.requests if res else None,
            "limits": res.limits if res else None,
            "readiness_probe": _probe(c.readiness_probe),
            "liveness_probe": _probe(c.liveness_probe),
        })
    ru = d.spec.strategy.rolling_update
    info = {
        "name": name,
        "revision": ann.get(REVISION_KEY),
        "change_cause": ann.get(CHANGE_CAUSE_KEY),
        "replicas": {
            "desired": d.spec.replicas,
            "updated": d.status.updated_replicas or 0,
            "ready": d.status.ready_replicas or 0,
            "available": d.status.available_replicas or 0,
            "unavailable": d.status.unavailable_replicas or 0,
        },
        "strategy": {
            "type": d.spec.strategy.type,
            "max_unavailable": ru.max_unavailable if ru else None,
            "max_surge": ru.max_surge if ru else None,
        },
        "conditions": [
            f"{c.type}={c.status} ({c.reason}): {c.message}"
            for c in (d.status.conditions or [])
        ],
        "containers": containers,
    }
    return json.dumps(info, indent=2, default=str)


def list_replicasets(deployment_name: str) -> str:
    items = sorted(_owned_replicasets(deployment_name), key=_revision)
    if not items:
        return f"No ReplicaSets found for deployment {deployment_name}."
    lines = []
    for rs in items:
        images = ",".join(c.image for c in rs.spec.template.spec.containers)
        ann = rs.metadata.annotations or {}
        lines.append(
            f"replicaset={rs.metadata.name} revision={_revision(rs)} image={images} "
            f"desired={rs.spec.replicas} ready={rs.status.ready_replicas or 0} "
            f"change_cause={ann.get(CHANGE_CAUSE_KEY, 'none')}"
        )
    return "\n".join(lines)


def query_prometheus(promql: str) -> str:
    url = settings.PROMETHEUS_URL.rstrip("/") + "/api/v1/query"
    r = requests.get(url, params={"query": promql}, timeout=10)
    r.raise_for_status()
    results = r.json().get("data", {}).get("result", [])
    if not results:
        return "Query returned no results."
    return "\n".join(
        f"{json.dumps(item.get('metric', {}))} => {item.get('value', [None, None])[1]}"
        for item in results[:30]
    )


# ---------- registry ----------

def _schema(name, description, properties=None, required=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
            },
        },
    }


TOOLS = {
    "list_deployments": {
        "fn": list_deployments,
        "schema": _schema("list_deployments",
                          "List deployments in the namespace with desired/ready/available replica counts."),
    },
    "list_pods": {
        "fn": list_pods,
        "schema": _schema("list_pods",
                          "List pods with phase, readiness, restart count, current container state "
                          "(e.g. CrashLoopBackOff, ImagePullBackOff) and last termination reason "
                          "(e.g. OOMKilled, Error)."),
    },
    "get_pod_logs": {
        "fn": get_pod_logs,
        "schema": _schema(
            "get_pod_logs",
            "Get recent log lines from a pod. Use previous=true to read logs from the "
            "last crashed container instance.",
            {
                "pod_name": {"type": "string", "description": "Exact pod name from list_pods."},
                "previous": {"type": "boolean", "description": "Read the previous (crashed) container's logs."},
                "tail_lines": {"type": "integer", "description": "Number of lines, 1-200. Default 50."},
            },
            ["pod_name"],
        ),
    },
    "get_events": {
        "fn": get_events,
        "schema": _schema(
            "get_events",
            "Get recent Kubernetes events (scheduling, image pulls, probe failures, kills). "
            "Optionally filter by an object name or name prefix.",
            {"object_name": {"type": "string", "description": "Optional name or prefix, e.g. a deployment name."}},
        ),
    },
    "describe_deployment": {
        "fn": describe_deployment,
        "schema": _schema(
            "describe_deployment",
            "Show a deployment's current revision, change cause, replica status, rollout "
            "conditions, and container spec (image, env, resource limits, probes).",
            {"name": {"type": "string", "description": "Deployment name."}},
            ["name"],
        ),
    },
    "list_replicasets": {
        "fn": list_replicasets,
        "schema": _schema(
            "list_replicasets",
            "List a deployment's ReplicaSets (rollout history) with revision, image, "
            "ready count and change cause. Useful to compare a new version with the previous one.",
            {"deployment_name": {"type": "string"}},
            ["deployment_name"],
        ),
    },
}

# Only offer Prometheus if it is actually configured, so the model doesn't waste calls.
if settings.PROMETHEUS_URL:
    TOOLS["query_prometheus"] = {
        "fn": query_prometheus,
        "schema": _schema(
            "query_prometheus",
            "Run an instant PromQL query and return matching series with their current values.",
            {"promql": {"type": "string", "description": "A PromQL expression."}},
            ["promql"],
        ),
    }

TOOL_SCHEMAS = [t["schema"] for t in TOOLS.values()]


def run_tool(name: str, args) -> str:
    """Execute a tool by name. Never raises: errors go back to the model as text."""
    if name not in TOOLS:
        return f"ERROR: unknown tool '{name}'. Available tools: {', '.join(TOOLS)}"
    if not isinstance(args, dict):
        return "ERROR: tool arguments must be a JSON object."
    try:
        return _truncate(TOOLS[name]["fn"](**args))
    except TypeError as e:
        return f"ERROR: bad arguments for {name}: {e}"
    except ApiException as e:
        if e.status == 403:
            return "ERROR 403 Forbidden: the agent's ServiceAccount is not permitted to do this."
        if e.status == 404:
            return f"ERROR 404: object not found in namespace {NS}."
        return f"ERROR {e.status} {e.reason}: {str(e.body)[:300]}"
    except requests.RequestException as e:
        return f"ERROR contacting Prometheus: {e}"
