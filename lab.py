"""Manage the test lab: RBAC setup, fault injection, reset.

This runs with YOUR kubectl credentials (admin), because it plays the role of
"the incident". The agent itself never uses these credentials.

    python lab.py setup        # namespace + read-only ServiceAccount, then verify RBAC
    python lab.py inject 4     # reset, then inject scenario 4 and wait for it to develop
    python lab.py status
    python lab.py reset
"""
import subprocess
import sys
import time
from pathlib import Path

import settings
from scenarios import SCENARIOS

ROOT = Path(__file__).parent
NS = settings.NAMESPACE
AGENT_USER = f"system:serviceaccount:{NS}:{settings.SERVICE_ACCOUNT}"


def kubectl(*args, check=True) -> str:
    r = subprocess.run(["kubectl", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed:\n{r.stderr}")
    return (r.stdout + r.stderr).strip()


def setup() -> None:
    print(kubectl("apply", "-f", str(ROOT / "k8s" / "rbac.yaml")))
    print("\nRBAC check for the agent identity (expect read=yes, write=no):")
    for verb, res in [("list", "pods"), ("get", "pods/log"), ("delete", "pods"),
                      ("patch", "deployments"), ("list", "secrets")]:
        answer = kubectl("auth", "can-i", verb, res, "-n", NS, f"--as={AGENT_USER}", check=False)
        print(f"  {verb:<7} {res:<12} -> {answer}")


def reset() -> None:
    kubectl("delete", "deployment", "--all", "-n", NS, "--wait=true", check=False)
    kubectl("wait", "--for=delete", "pod", "--all", "-n", NS, "--timeout=120s", check=False)
    # Events outlive their objects (~1h). Without this, an old scenario's
    # events would leak into the next one and contaminate the evaluation.
    kubectl("delete", "events", "--all", "-n", NS, check=False)
    print(f"namespace {NS} reset")


def inject(sid: int) -> None:
    s = SCENARIOS[sid]
    reset()
    print(f"injecting scenario {sid}: {s['name']}")
    for action, target in s["steps"]:
        if action == "apply":
            print(kubectl("apply", "-f", str(ROOT / target)))
        elif action == "wait_rollout":
            print(kubectl("rollout", "status", f"deployment/{target}", "-n", NS, "--timeout=180s"))
    print(f"waiting {s['settle_seconds']}s for the fault to develop...")
    time.sleep(s["settle_seconds"])
    print(status())


def status() -> str:
    return kubectl("get", "deployments,replicasets,pods", "-n", NS, check=False)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "setup":
        setup()
    elif cmd == "inject" and len(sys.argv) == 3 and int(sys.argv[2]) in SCENARIOS:
        inject(int(sys.argv[2]))
    elif cmd == "reset":
        reset()
    elif cmd == "status":
        print(status())
    else:
        print(__doc__)
        sys.exit(1)
