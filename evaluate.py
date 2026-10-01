"""Run every scenario N times and score the agent against ground truth.

    python evaluate.py --runs 3                 # LLM agent
    python evaluate.py --baseline               # rule-based baseline, no LLM
    python evaluate.py --runs 5 --scenarios 1 4

Three scores per run:
  category_ok   - picked the right failure category
  root_cause_ok - named the SPECIFIC cause (keyword match on root_cause + evidence)
  action_ok     - proposed the right kind of fix (keyword match on proposed_action)

Always run the baseline too. If a 30-line rule script scores as well as the
agent, the agent isn't adding value on that scenario, and you should say so.
"""
import argparse
import csv
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import lab
import settings
from scenarios import SCENARIOS
from tools import NS, _owned_replicasets, _revision, core

RESULTS_DIR = Path(__file__).parent / "results"


def baseline_diagnose(deployment: str) -> dict:
    """Hand-written rules on pod/ReplicaSet state. No LLM, no logs."""
    started = time.time()
    reasons = set()
    pods = core.list_namespaced_pod(NS, label_selector=f"app={deployment}").items
    for p in pods:
        for cs in p.status.container_statuses or []:
            if cs.state.waiting and cs.state.waiting.reason:
                reasons.add(cs.state.waiting.reason)
            if cs.last_state and cs.last_state.terminated and cs.last_state.terminated.reason:
                reasons.add(cs.last_state.terminated.reason)

    replicasets = _owned_replicasets(deployment)
    stuck_rollout = False
    if len(replicasets) > 1:
        newest = max(replicasets, key=_revision)
        stuck_rollout = (newest.status.ready_replicas or 0) < (newest.spec.replicas or 0)

    if reasons & {"ImagePullBackOff", "ErrImagePull"}:
        d = ("image_pull_error", "Container image cannot be pulled.", "Check the image name and tag.")
    elif "OOMKilled" in reasons:
        d = ("oom_killed", "Container exceeded its memory limit.", "Increase the memory limit.")
    elif reasons & {"CrashLoopBackOff", "Error"}:
        d = ("crashloop_config_error", "Container keeps crashing.", "Check container logs and configuration.")
    elif stuck_rollout:
        d = ("bad_rollout", "New ReplicaSet is not becoming ready.", "Roll back the deployment.")
    else:
        d = ("unknown", "No known pattern matched.", "Investigate manually.")

    return {
        "diagnosis": {"category": d[0], "root_cause": d[1], "evidence": sorted(reasons),
                      "proposed_action": d[2], "confidence": "n/a"},
        "tool_calls": [],
        "seconds": round(time.time() - started, 1),
    }


def score(s: dict, diag) -> tuple:
    if not diag:
        return False, False, False
    evidence = diag.get("evidence") or []
    if not isinstance(evidence, list):
        evidence = [evidence]
    cause_text = (str(diag.get("root_cause", "")) + " " + " ".join(map(str, evidence))).lower()
    action_text = str(diag.get("proposed_action", "")).lower()
    return (
        diag.get("category") == s["expected_category"],
        any(k in cause_text for k in s["root_cause_keywords"]),
        any(k in action_text for k in s["action_keywords"]),
    )


def summarize(rows: list) -> None:
    by_scenario = defaultdict(list)
    for r in rows:
        by_scenario[r["scenario"]].append(r)
    header = f"{'scenario':<22}{'category':>10}{'root cause':>12}{'action':>9}{'avg tools':>11}{'avg secs':>10}"
    print("\n" + header + "\n" + "-" * len(header))
    for name, rs in by_scenario.items():
        n = len(rs)
        frac = lambda k: f"{sum(r[k] for r in rs)}/{n}"
        avg = lambda k: f"{sum(r[k] for r in rs) / n:.1f}"
        print(f"{name:<22}{frac('category_ok'):>10}{frac('root_cause_ok'):>12}"
              f"{frac('action_ok'):>9}{avg('tool_calls'):>11}{avg('seconds'):>10}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--scenarios", type=int, nargs="+", default=sorted(SCENARIOS))
    parser.add_argument("--baseline", action="store_true")
    a = parser.parse_args()

    if not a.baseline:
        from agent import run_agent  # only import the LLM client when needed

    mode = "baseline" if a.baseline else settings.LLM_MODEL
    runs = 1 if a.baseline else a.runs  # baseline is deterministic
    rows = []
    for sid in a.scenarios:
        s = SCENARIOS[sid]
        lab.inject(sid)
        for i in range(1, runs + 1):
            print(f"\n=== scenario {sid} ({s['name']}) run {i}/{runs} [{mode}] ===")
            out = baseline_diagnose(s["deployment"]) if a.baseline else run_agent(s["alert"], verbose=False)
            diag = out["diagnosis"]
            cat_ok, cause_ok, action_ok = score(s, diag)
            print(f"category={diag.get('category') if diag else None} "
                  f"cat_ok={cat_ok} cause_ok={cause_ok} action_ok={action_ok}")
            rows.append({
                "mode": mode, "scenario": s["name"], "run": i,
                "expected": s["expected_category"],
                "got": diag.get("category") if diag else None,
                "category_ok": int(cat_ok), "root_cause_ok": int(cause_ok), "action_ok": int(action_ok),
                "tool_calls": len(out["tool_calls"]), "seconds": out["seconds"],
                "root_cause": diag.get("root_cause") if diag else None,
                "proposed_action": diag.get("proposed_action") if diag else None,
            })
    lab.reset()

    RESULTS_DIR.mkdir(exist_ok=True)
    safe_mode = mode.replace(":", "-").replace("/", "-")
    path = RESULTS_DIR / f"results-{safe_mode}-{datetime.now():%Y%m%d-%H%M%S}.csv"
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    summarize(rows)
    print(f"\nsaved {path}")


if __name__ == "__main__":
    main()
