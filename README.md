# AIOps Incident Agent

An LLM agent that investigates a Kubernetes alert using **read-only** tools,
produces a structured root-cause diagnosis, then — behind an explicit policy
layer and human approval — can propose and execute a real fix. Retargeted
from the original project's synthetic `agent-lab` fault scenarios to
diagnose and repair a real system: [mini-faas](https://github.com/JashithaDesu/Simple-faas),
a self-built Kubernetes-native FaaS platform.

```
alert ──► agent loop ──► read-only tools (agent-reader SA) ──► k8s API
              │                                                   │
              └──── JSON diagnosis ◄──── evidence ◄───────────────┘
                         │
                         ▼
                 propose_action() ──► policy.decide() ──► auto / ask / deny
                         │                                     │
                         ▼                              (human approval
                 write tool (agent-writer SA,                 if "ask")
                 RBAC-scoped to 1 CRD + 2 named Deployments)
                         │
                         ▼
                 verify_action() — polls real cluster state,
                 not just "did the patch get accepted"
                         │
                         ▼
                logs/audit.jsonl  (every step, every decision, every result)
```

## Status: this is honest, run-verified evidence — not a full evaluation suite

The original plan was 5 runs × 4 fault categories. What's below is smaller
but real: every number is from an actual run against a real, injected fault
on a real cluster, not a projection or a synthetic scenario. Where
something was tested once instead of five times, or not tested at all,
that's stated plainly, not implied otherwise.

## Guardrails (enforced outside the model, in RBAC and code)

| Guardrail | Where | Verified how |
|---|---|---|
| Read-only diagnosis identity | `k8s/rbac-mini-faas.yaml`, impersonation in `tools.py` | `kubectl auth can-i`: `list pods`→yes, `delete pods`→no, `patch deployments`→no |
| Write identity scoped to one CRD + 2 named Deployments | `k8s/rbac-writer.yaml` (`resourceNames` allowlist) | `kubectl auth can-i`: `patch functions`→yes, `delete functions`→no, `patch deployment/mini-faas-gateway`→yes, `patch deployment/hello-function`→**no** (must go through the Function CR, by design — mini-faas's own controller reconciles that Deployment and would silently revert a direct patch) |
| Policy layer decides auto/ask/deny — not the model | `policy.py` | Both branches run live: a high-confidence image-tag fix auto-executed; a medium-confidence version of the same fault, and a memory-limit fix (`NEVER_AUTO` regardless of confidence), both correctly paused for a human `y/N` |
| Write parameters are code-determined, not model-chosen | `act.py: find_previous_image`, `OOM_MEMORY_LIMIT` | The model never supplies a target image or memory value — code reads real ReplicaSet history or uses a fixed, documented default |
| Verification checks the symptom, not just "patch accepted" | `write_tools.py: verify_action`, `verify_deployment_action` | Polls the Function's own `status.phase` or the Deployment's `readyReplicas`, not the write call's return code |
| Full audit trail | `logs/audit.jsonl` | Every proposal, decision, approver (`policy_auto` / `human`), execution, and verification result, timestamped and re-derivable |

## Real diagnosis runs (image_pull_error, hello-function)

All four runs injected the identical real fault — `hello-function`'s image
patched to a nonexistent Docker Hub tag — and are independently comparable.

| Run | Seconds | LLM calls | Tool calls | Prompt tokens | Completion tokens | Correct category | Confidence |
|---|---|---|---|---|---|---|---|
| `0f090ea7` | 800.2 | — | 4 | — | — | correct: image_pull_error | high |
| `25c5b234` | 513.5 | — | 2 | — | — | correct: image_pull_error | high |
| `07be2e0d` | 269.7 | — | 4 | — | — | correct: image_pull_error | high |
| `c209ce2a` | 342.0 | 5 | 4 | 9,589 | 305 | correct: image_pull_error | high |

**4/4 correct diagnoses**, every time correctly naming the actual broken
image tag from live cluster evidence, not a guess. Token/call-level
instrumentation was added after the first three runs, so only the last run
has that detail — the three earlier numbers are real, just less granular.

**Timing varies 270s to 800s on identical input.** The likely explanation,
not independently confirmed: Ollama unloads a model after ~5 minutes idle
by default, so a "cold" run pays a reload cost a "warm" run doesn't. Don't
treat this as a single latency figure — report it as a range with that
caveat, not an average.

## Write actions — each verified against the live cluster, not just unit-tested

| Action | Target | Policy decision tested | Verified result |
|---|---|---|---|
| `set_function_image` | `hello-function` (via its Function CR) | `auto` (high confidence) | Function reached `Warm` within 30s |
| `set_function_image` | `hello-function` | `ask` (medium confidence) -> human approved | Function reached `Warm` within 30s |
| `set_deployment_image` | `mini-faas-gateway` (direct Deployment patch) | `auto` (high confidence) | Deployment reached `1/1 ready` within 30s |
| `set_deployment_memory_limit` | `mini-faas-gateway`, after a real reproduced OOM kill (`8Mi` limit -> `Reason: OOMKilled`, `Exit Code: 137`) | `ask` (`NEVER_AUTO`, regardless of confidence) -> human approved | Memory raised to `256Mi`, Deployment reached `1/1 ready` within 30s |

`rollout_failure` (a stuck rollout) shares `set_deployment_image`'s fix
logic in `act.py`, and the policy always routes it to `ask` — but this
category was never live-triggered. Coded and policy-reviewed, not run.

## Resource profile (one monitored run, `monitor.sh`)

`ps`-based sampling every 2s during a full diagnosis — approximate, not a
real profiler, but internally consistent: the peak RSS for Ollama
(~4.9GB) lines up with a 7B parameter model's expected quantized memory
footprint, which is a reasonable sanity check that these aren't noise.

| Process | Avg CPU | Max CPU | Avg RSS | Max RSS |
|---|---|---|---|---|
| Ollama (inference) | 314.8% | 378.0% | 4,654 MB | 4,905 MB |
| Agent (Python) | 3.3% | 88.8% | 109 MB | 110 MB |

CPU figures above 100% reflect genuine multi-core utilization during CPU
inference — expected for a 7B model with no GPU offload, not a
measurement error.

## Prerequisites

- Ubuntu, Docker, [kind](https://kind.sigs.k8s.io/), `kubectl`, Python 3.10+
- A running [mini-faas](https://github.com/JashithaDesu/Simple-faas) deployment (Function CRD, controller, gateway)
- An LLM with tool calling: **local (free)** — [Ollama](https://ollama.com) + `ollama pull qwen2.5:7b`, or any OpenAI-compatible hosted endpoint via `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL`

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

kubectl apply -f k8s/rbac-mini-faas.yaml   # read-only identity
kubectl apply -f k8s/rbac-writer.yaml      # write identity, RBAC-scoped
```

Confirm both guardrails before anything else:

```bash
kubectl auth can-i list pods --as=system:serviceaccount:default:agent-reader -n default      # yes
kubectl auth can-i delete pods --as=system:serviceaccount:default:agent-reader -n default    # no
kubectl auth can-i patch deployment/hello-function --as=system:serviceaccount:default:agent-writer -n default  # no
```

## Run it

Diagnose only (Day 1):

```bash
AGENT_NAMESPACE=default AGENT_SERVICE_ACCOUNT=agent-reader \
  python agent.py --alert "hello-function pods are not starting, requests to the gateway are timing out"
```

Diagnose and act, chained (Day 1 + Day 2, one command):

```bash
AGENT_NAMESPACE=default AGENT_SERVICE_ACCOUNT=agent-reader AGENT_WRITE_SERVICE_ACCOUNT=agent-writer \
  python incident.py --function hello-function \
  --alert "hello-function pods are not starting, requests to the gateway are timing out"
```

Act on a target Deployment instead of a Function CR (gateway/manager only,
enforced by RBAC):

```bash
python act.py --function mini-faas-gateway --target-kind deployment --diagnosis-file /tmp/diag.json
```

Monitor CPU/memory during a real run:

```bash
./monitor.sh
```

## What's genuinely untested

Stated plainly, not buried:

- **No automated 5-run evaluation across all categories.** `image_pull_error` has 4 real samples; `oom_killed` and the `set_deployment_image` path each have exactly 1; `rollout_failure` has none.
- **The original project's synthetic scenarios** (`k8s/01-payments-crashloop.yaml` through `04-catalog-v2.yaml`, `lab.py`, `scenarios.py`'s `payments`/`reports`/`frontend`/`catalog` fixtures) are unused — kept for reference, superseded by testing against mini-faas's real resources instead.
- **No GPU acceleration tested.** All timing is CPU-only inference; a GPU would very likely cut latency substantially, untested here.
- **Resource profiling is one sample from one run**, not a statistically meaningful distribution.

## Layout

```
settings.py       config (env-overridable: namespace, read/write SA names, LLM endpoint)
tools.py          read-only tools + guardrails
write_tools.py    write tools (Function CR + 2 named Deployments only) + verification
policy.py         auto / ask / deny decision logic
agent.py          Day 1 loop: diagnosis, per-step timing, token counts, audit log
act.py            Day 2 loop: propose -> policy -> approve -> execute -> verify
incident.py       chains agent.py + act.py into one command
monitor.sh        CPU/memory sampling during a real run
k8s/               RBAC manifests (rbac-mini-faas.yaml, rbac-writer.yaml) +
                   the original project's unused synthetic fault manifests
```
