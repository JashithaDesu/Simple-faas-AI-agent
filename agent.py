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
    completion = llm.chat.completions.create(**kwargs)
    usage = completion.usage
    return completion.choices[0].message, {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
    }

def run_agent(alert: str, max_steps: int = 12, verbose: bool = True,
               expected_category: str | None = None) -> dict:
    run_id = uuid.uuid4().hex[:8]
    started = time.time()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Incoming alert:\n{alert}"},
    ]
    calls = []
    steps = []  # per-step record: type, name, duration, tokens
    llm_call_count = 0
    audit(run_id, "alert", alert=alert, model=settings.LLM_MODEL)

    final_text = None
    for _ in range(max_steps):
        step_start = time.time()
        msg, usage = _chat(messages)
        llm_call_count += 1
        step_duration = round(time.time() - step_start, 2)
        steps.append({"type": "llm_call", "name": None, "duration_seconds": step_duration, **usage})
        audit(run_id, "llm_call", duration_seconds=step_duration, **usage)

        messages.append(msg.model_dump(exclude_none=True))
        if not msg.tool_calls:
            final_text = msg.content or ""
            break
        for tc in msg.tool_calls:
            name = tc.function.name
            tool_start = time.time()
            try:
                args = json.loads(tc.function.arguments or "{}")
                result = run_tool(name, args)
            except json.JSONDecodeError:
                args = tc.function.arguments
                result = "ERROR: tool arguments were not valid JSON."
            tool_duration = round(time.time() - tool_start, 2)
            calls.append({"tool": name, "args": args})
            steps.append({"type": "tool_call", "name": name, "duration_seconds": tool_duration,
                          "prompt_tokens": None, "completion_tokens": None})
            audit(run_id, "tool_call", tool=name, args=args, result=result[:500],
                  duration_seconds=tool_duration)
            if verbose:
                print(f"  -> {name}({args})  [{tool_duration}s]")
                print("     " + result[:300].replace("\n", "\n     "))
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    if final_text is None:
        messages.append({"role": "user",
                         "content": "Step limit reached. Give your final answer now as the JSON object."})
        msg, usage = _chat(messages, use_tools=False)
        llm_call_count += 1
        final_text = msg.content or ""
        messages.append({"role": "assistant", "content": final_text})

    diagnosis = parse_diagnosis(final_text)
    if diagnosis is None:
        messages.append({"role": "user",
                         "content": "Your reply was not valid JSON. Reply with ONLY the JSON object."})
        msg, usage = _chat(messages, use_tools=False)
        llm_call_count += 1
        final_text = msg.content or ""
        diagnosis = parse_diagnosis(final_text)

    seconds = round(time.time() - started, 1)
    total_prompt_tokens = sum(s["prompt_tokens"] or 0 for s in steps if s["type"] == "llm_call")
    total_completion_tokens = sum(s["completion_tokens"] or 0 for s in steps if s["type"] == "llm_call")

    correct = None
    if expected_category is not None and diagnosis is not None:
        correct = diagnosis.get("category") == expected_category

    audit(run_id, "diagnosis", diagnosis=diagnosis, raw=final_text[:1000],
          tool_calls=len(calls), llm_calls=llm_call_count, seconds=seconds,
          prompt_tokens=total_prompt_tokens, completion_tokens=total_completion_tokens,
          expected_category=expected_category, correct=correct)

    if verbose:
        print(f"\n--- Metrics ---")
        print(f"LLM calls: {llm_call_count}  Tool calls: {len(calls)}  "
              f"Prompt tokens: {total_prompt_tokens}  Completion tokens: {total_completion_tokens}")
        if expected_category is not None:
            print(f"Expected category: {expected_category}  "
                  f"Got: {diagnosis.get('category') if diagnosis else None}  "
                  f"Correct: {correct}")

    return {"run_id": run_id, "diagnosis": diagnosis, "raw_final": final_text,
            "tool_calls": calls, "llm_calls": llm_call_count, "steps": steps,
            "prompt_tokens": total_prompt_tokens, "completion_tokens": total_completion_tokens,
            "seconds": seconds, "correct": correct}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the incident agent once.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--scenario", type=int, choices=sorted(SCENARIOS))
    group.add_argument("--alert", type=str)
    a = parser.parse_args()

    alert = SCENARIOS[a.scenario]["alert"] if a.scenario else a.alert
    expected = SCENARIOS[a.scenario]["category"] if a.scenario else None
    print(f"ALERT: {alert}\nMODEL: {settings.LLM_MODEL}\n")
    out = run_agent(alert, expected_category=expected)
    print("\nDIAGNOSIS:")
    print(json.dumps(out["diagnosis"], indent=2) if out["diagnosis"] else out["raw_final"])
    print(f"\n{len(out['tool_calls'])} tool calls, {out['seconds']}s, run_id={out['run_id']}")
