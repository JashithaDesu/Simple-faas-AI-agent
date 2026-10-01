"""The agent loop: alert in, structured diagnosis out.

    python agent.py --scenario 1          # use a scenario's alert (inject it first with lab.py)
    python agent.py --alert "some text"   # any custom alert
"""
import argparse
import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from openai import OpenAI

import settings
from scenarios import CATEGORIES, SCENARIOS
from tools import TOOL_SCHEMAS, run_tool

LOG_DIR = Path(__file__).parent / "logs"
llm = OpenAI(base_url=settings.LLM_BASE_URL, api_key=settings.LLM_API_KEY)

SYSTEM_PROMPT = f"""You are an on-call SRE assistant investigating an alert in Kubernetes namespace "{settings.NAMESPACE}".

Rules:
- Your tools are READ-ONLY. You cannot change the cluster. A human operator will act on your recommendation.
- Investigate before concluding: check pod states, events, logs, and deployment / rollout history as needed.
- Only state facts you actually saw in tool output. If the evidence is insufficient, say so and lower your confidence.
- Identify the specific cause, not just the symptom.

When you are finished, reply with ONLY this JSON object (no prose, no markdown fences):
{{
  "category": one of {json.dumps(CATEGORIES)},
  "root_cause": "one or two sentences naming the specific cause",
  "evidence": ["short facts taken from tool output"],
  "proposed_action": "the concrete fix a human operator should apply",
  "confidence": "high" | "medium" | "low"
}}"""


def audit(run_id: str, event: str, **fields) -> None:
    """Append-only JSONL audit trail of everything the agent saw and did."""
    LOG_DIR.mkdir(exist_ok=True)
    record = {"ts": datetime.now(timezone.utc).isoformat(), "run_id": run_id,
              "event": event, **fields}
    with open(LOG_DIR / "audit.jsonl", "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def parse_diagnosis(text):
    if not text:
        return None
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)  # reasoning models
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        d = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict):
        return None
    if d.get("category") not in CATEGORIES:
        d["category_raw"] = d.get("category")
        d["category"] = "unknown"
    return d


def _chat(messages, use_tools=True):
    kwargs = {"model": settings.LLM_MODEL, "messages": messages, "temperature": 0}
    if use_tools:
        kwargs["tools"] = TOOL_SCHEMAS
    return llm.chat.completions.create(**kwargs).choices[0].message


def run_agent(alert: str, max_steps: int = 12, verbose: bool = True) -> dict:
    run_id = uuid.uuid4().hex[:8]
    started = time.time()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Incoming alert:\n{alert}"},
    ]
    calls = []
    audit(run_id, "alert", alert=alert, model=settings.LLM_MODEL)

    final_text = None
    for _ in range(max_steps):
        msg = _chat(messages)
        messages.append(msg.model_dump(exclude_none=True))
        if not msg.tool_calls:
            final_text = msg.content or ""
            break
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
                result = run_tool(name, args)
            except json.JSONDecodeError:
                args = tc.function.arguments
                result = "ERROR: tool arguments were not valid JSON."
            calls.append({"tool": name, "args": args})
            audit(run_id, "tool_call", tool=name, args=args, result=result[:500])
            if verbose:
                print(f"  -> {name}({args})")
                print("     " + result[:300].replace("\n", "\n     "))
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    if final_text is None:  # hit the step limit while still calling tools
        messages.append({"role": "user",
                         "content": "Step limit reached. Give your final answer now as the JSON object."})
        final_text = _chat(messages, use_tools=False).content or ""
        messages.append({"role": "assistant", "content": final_text})

    diagnosis = parse_diagnosis(final_text)
    if diagnosis is None:  # one repair attempt for malformed output
        messages.append({"role": "user",
                         "content": "Your reply was not valid JSON. Reply with ONLY the JSON object."})
        final_text = _chat(messages, use_tools=False).content or ""
        diagnosis = parse_diagnosis(final_text)

    seconds = round(time.time() - started, 1)
    audit(run_id, "diagnosis", diagnosis=diagnosis, raw=final_text[:1000],
          tool_calls=len(calls), seconds=seconds)
    return {"run_id": run_id, "diagnosis": diagnosis, "raw_final": final_text,
            "tool_calls": calls, "seconds": seconds}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the incident agent once.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--scenario", type=int, choices=sorted(SCENARIOS))
    group.add_argument("--alert", type=str)
    a = parser.parse_args()

    alert = SCENARIOS[a.scenario]["alert"] if a.scenario else a.alert
    print(f"ALERT: {alert}\nMODEL: {settings.LLM_MODEL}\n")
    out = run_agent(alert)
    print("\nDIAGNOSIS:")
    print(json.dumps(out["diagnosis"], indent=2) if out["diagnosis"] else out["raw_final"])
    print(f"\n{len(out['tool_calls'])} tool calls, {out['seconds']}s, run_id={out['run_id']}")
