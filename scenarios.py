"""The four injected failures, their alerts, and the ground truth used for scoring.

Alerts describe the SYMPTOM only, the way a real Alertmanager alert would.
The agent has to find the cause itself.
"""

CATEGORIES = [
    "crashloop_config_error",
    "oom_killed",
    "image_pull_error",
    "bad_rollout",
    "other",
    "unknown",
]

SCENARIOS = {
    1: {
        "name": "crashloop-bad-env",
        "deployment": "payments",
        "steps": [("apply", "k8s/01-payments-crashloop.yaml")],
        "settle_seconds": 40,
        "alert": "[FIRING] KubeDeploymentReplicasMismatch: deployment 'payments' "
                 "in namespace 'agent-lab' has 0/1 replicas available.",
        "expected_category": "crashloop_config_error",
        # Scored against root_cause + evidence: did it find the SPECIFIC cause?
        "root_cause_keywords": ["app_mode"],
        "action_keywords": ["app_mode"],
    },
    2: {
        "name": "oom-killed",
        "deployment": "reports",
        "steps": [("apply", "k8s/02-reports-oom.yaml")],
        "settle_seconds": 45,
        "alert": "[FIRING] KubeDeploymentReplicasMismatch: deployment 'reports' "
                 "in namespace 'agent-lab' has 0/1 replicas available.",
        "expected_category": "oom_killed",
        "root_cause_keywords": ["memory"],
        "action_keywords": ["memory"],
    },
    3: {
        "name": "image-pull-error",
        "deployment": "frontend",
        "steps": [("apply", "k8s/03-frontend-imagepull.yaml")],
        "settle_seconds": 40,
        "alert": "[FIRING] KubeDeploymentReplicasMismatch: deployment 'frontend' "
                 "in namespace 'agent-lab' has 0/1 replicas available.",
        "expected_category": "image_pull_error",
        "root_cause_keywords": ["9.99.99", "tag"],
        "action_keywords": ["image", "tag"],
    },
    4: {
        "name": "bad-rollout",
        "deployment": "catalog",
        "steps": [
            ("apply", "k8s/04-catalog-v1.yaml"),
            ("wait_rollout", "catalog"),
            ("apply", "k8s/04-catalog-v2.yaml"),
        ],
        "settle_seconds": 75,  # longer than progressDeadlineSeconds (60)
        "alert": "[FIRING] KubeDeploymentRolloutStuck: deployment 'catalog' "
                 "in namespace 'agent-lab' has not finished rolling out.",
        "expected_category": "bad_rollout",
        "root_cause_keywords": ["readiness", "404"],
        "action_keywords": ["rollback", "roll back", "undo"],
    },
}
