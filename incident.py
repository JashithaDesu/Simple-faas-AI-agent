import argparse
import json

from act import audit as act_audit
from act import run_action
from agent import run_agent


def run_incident(target_name: str, alert: str, target_kind: str = "function",
                  auto_yes: bool = False, max_steps: int = 12) -> dict:
    print("=== DIAGNOSING ===")
    diag_result = run_agent(alert, max_steps=max_steps)
    diagnosis = diag_result["diagnosis"]
    diag_run_id = diag_result["run_id"]

    print(f"\nDiagnosis (run {diag_run_id}, {diag_result['seconds']}s, "
          f"{len(diag_result['tool_calls'])} tool calls):")
    print(json.dumps(diagnosis, indent=2) if diagnosis else diag_result["raw_final"])

    if diagnosis is None:
        print("\nNo valid diagnosis produced — stopping before the action phase.")
        return {"diagnosis_run_id": diag_run_id, "diagnosis": None, "action": None}

    print("\n=== ACTING ===")
    action_result = run_action(target_name, diagnosis, target_kind=target_kind,
                                auto_yes=auto_yes)
    # Link the two run IDs in the audit log — the diagnosis and the action
    # it produced are separate audit entries up to this point; this ties
    # them together so the full chain is traceable from one record.
    act_audit(action_result["run_id"], "linked_diagnosis", diagnosis_run_id=diag_run_id)

    return {"diagnosis_run_id": diag_run_id, "diagnosis": diagnosis, "action": action_result}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Diagnose and act on an incident, end to end.")
    parser.add_argument("--function", required=True,
                        help="Name of the Function or Deployment to investigate and fix.")
    parser.add_argument("--target-kind", choices=["function", "deployment"], default="function")
    parser.add_argument("--alert", required=True, help="The incoming alert text.")
    parser.add_argument("--auto-yes", action="store_true",
                        help="Skip interactive approval for 'ask' actions. "
                             "Only for scripted/demo runs, never for real incidents.")
    parser.add_argument("--max-steps", type=int, default=12)
    a = parser.parse_args()

    result = run_incident(a.function, a.alert, target_kind=a.target_kind,
                          auto_yes=a.auto_yes, max_steps=a.max_steps)
