"""All configuration in one place. Override any value with an environment variable."""
import os

# Kubernetes scope: the agent can only ever see this one namespace.
NAMESPACE = os.getenv("AGENT_NAMESPACE", "agent-lab")
SERVICE_ACCOUNT = os.getenv("AGENT_SERVICE_ACCOUNT", "agent-reader")
WRITE_SERVICE_ACCOUNT = os.getenv("AGENT_WRITE_SERVICE_ACCOUNT", "agent-writer")

# Optional. If unset, the Prometheus tool is not offered to the model at all.
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "")

# Any OpenAI-compatible endpoint. Default: local Ollama.
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "ollama")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:7b")

# Cap on tool output so one huge log can't flood the model's context.
MAX_TOOL_OUTPUT_CHARS = int(os.getenv("MAX_TOOL_OUTPUT_CHARS", "4000"))
