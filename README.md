# AIOps Incident Agent:

An LLM agent that receives a Kubernetes alert, investigates with **read-only** tools,
and returns a structured diagnosis. A human acts on it. Day 2 adds write actions behind
a policy layer and human approval.

```
alert ──► agent loop ──► tools (impersonated, read-only ServiceAccount) ──► k8s API
              │                                                   │
              └──── JSON diagnosis ◄──── evidence ◄───────────────┘
                         │
                logs/audit.jsonl  (every call, every result)
```

## Guardrails (enforced outside the model)

| Guardrail | Where | Why it matters |
|---|---|---|
| Read-only identity | `k8s/rbac.yaml` + impersonation in `tools.py` | The API server refuses writes even if the model tries |
| Fixed namespace | `settings.NAMESPACE`, no tool parameter | The model cannot look outside its sandbox |
| Tool allow-list | `TOOLS` registry in `tools.py` | Unknown tool names are rejected |
| Output truncation | `MAX_TOOL_OUTPUT_CHARS` | A giant log cannot flood the context |
| Audit trail | `logs/audit.jsonl` | Every observation and conclusion is traceable |

The prompt also says "read-only", but nothing depends on the model obeying it.

## Prerequisites

- Ubuntu, Docker, [kind](https://kind.sigs.k8s.io/), `kubectl`, Python 3.10+
- An LLM with tool calling, via any OpenAI-compatible endpoint:
  - **Local (free):** [Ollama](https://ollama.com) with `ollama pull qwen2.5:7b`
  - **Hosted:** set `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` for your provider

## Setup

```bash
kind create cluster --name agent-lab        # skip if you already have one
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python lab.py setup
```

`setup` prints an RBAC check. You should see `list pods -> yes`, `delete pods -> no`,
`patch deployments -> no`, `list secrets -> no`. **If delete says yes, stop and fix it before going further.**

## Run one scenario by hand

```bash
python lab.py inject 1          # resets the namespace, injects the fault, waits
python agent.py --scenario 1    # agent investigates; you see every tool call
```

Scenarios:

| # | Deployment | Fault | What a good diagnosis names |
|---|---|---|---|
| 1 | payments | `APP_MODE=prodution` typo, app exits on startup | the `APP_MODE` variable (only visible in logs) |
| 2 | reports | allocates 250MB with a 100Mi limit | OOMKilled / memory limit |
| 3 | frontend | image `nginx:9.99.99` does not exist | the bad image tag |
| 4 | catalog | v2 runs but fails its readiness probe; rollout stuck | readiness failure in the new revision, roll back |

## Evaluate

```bash
python evaluate.py --baseline       # rule-based, no LLM: ~2 minutes
python evaluate.py --runs 3         # the agent, each scenario 3x
```

Results go to `results/*.csv` plus a summary table. **Run the baseline first.** The rule
script will likely get all four categories right, because scenarios 2-4 are written
directly in pod status. The agent's value must show up in the *root cause* and *action*
columns, especially scenario 1, where the cause is only in the logs. If the agent doesn't
beat the baseline there, that's a real finding. Write it down; don't hide it.

## Troubleshooting

- **The model answers without calling any tools, or calls them with wrong arguments:**
  7-8B local models are unreliable at multi-step tool use. Try `qwen2.5:14b` or a hosted
  model, and record the difference. That comparison is itself a result.
- **Answers look like the model forgot earlier tool output:** Ollama's default context window
  is small, and older context is silently cut. Start Ollama with a bigger one, e.g.
  `OLLAMA_CONTEXT_LENGTH=16384 ollama serve`.
- **Scenario 2 never shows OOMKilled:** check `python lab.py status`; the `polinux/stress`
  image must be pullable. Increase `settle_seconds` in `scenarios.py` if pods are still starting.
- **Scenario 4 not stuck:** v1 must finish rolling out before v2 is applied. `lab.py` waits
  for this. Check `kubectl rollout history deployment/catalog -n agent-lab`.

## Optional: Prometheus

If you have Prometheus reachable (e.g. port-forwarded), set
`PROMETHEUS_URL=http://localhost:9090` and a `query_prometheus` tool is offered to the
model automatically. It is left out when unset, so the model doesn't waste calls on it.

## Layout

```
settings.py    config (env-overridable)
tools.py       read-only tools + guardrails
agent.py       the agent loop, JSON output, audit log
scenarios.py   alerts + ground truth
lab.py         RBAC setup, fault injection, reset
evaluate.py    scoring + rule-based baseline
k8s/           RBAC and fault manifests
```
