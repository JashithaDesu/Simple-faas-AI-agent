#!/bin/bash
# Samples CPU% and RSS memory for the Ollama inference process and the
# agent's own Python process every 2s during one real incident.py run,
# then summarizes. ps-based sampling is approximate, not a real profiler —
# good enough for an honest "roughly X%% CPU, Y MB" resume-level claim,
# not a rigorous benchmark.
set -euo pipefail

OUT=monitor-results.csv
echo "ts,proc,pid,cpu_pct,rss_mb" > "$OUT"

sample_loop() {
	while true; do
		ts=$(date +%s)
		OLLAMA_PID=$(pgrep -f llama-server | head -1 || true)
		if [ -z "$OLLAMA_PID" ]; then
			OLLAMA_PID=$(pgrep -x ollama | head -1 || true)
		fi
		if [ -n "$OLLAMA_PID" ]; then
			read -r cpu rss < <(ps -o %cpu=,rss= -p "$OLLAMA_PID" 2>/dev/null || echo "")
			if [ -n "${cpu:-}" ]; then
				echo "$ts,ollama,$OLLAMA_PID,$cpu,$((rss / 1024))" >> "$OUT"
			fi
		fi
		PY_PID=$(pgrep -f "python incident.py" | head -1 || true)
		if [ -n "$PY_PID" ]; then
			read -r cpu rss < <(ps -o %cpu=,rss= -p "$PY_PID" 2>/dev/null || echo "")
			if [ -n "${cpu:-}" ]; then
				echo "$ts,agent,$PY_PID,$cpu,$((rss / 1024))" >> "$OUT"
			fi
		fi
		sleep 2
	done
}

sample_loop &
MONITOR_PID=$!
trap 'kill "$MONITOR_PID" 2>/dev/null || true' EXIT

echo "=== Starting monitored run (sampling every 2s) ==="
START=$(date +%s)

AGENT_NAMESPACE=default AGENT_SERVICE_ACCOUNT=agent-reader AGENT_WRITE_SERVICE_ACCOUNT=agent-writer \
	python incident.py --function hello-function \
	--alert "hello-function pods are not starting, requests to the gateway are timing out" \
	--auto-yes

END=$(date +%s)
kill "$MONITOR_PID" 2>/dev/null || true
trap - EXIT

echo ""
echo "Resource summary"
echo "Total wall-clock time: $((END - START))s"
echo " ollama (inference) process"
awk -F, '$2=="ollama"{c+=$4;m+=$5;n++; if($4>maxc)maxc=$4; if($5>maxm)maxm=$5}
	END{if(n>0) printf "avg_cpu=%.1f%% max_cpu=%.1f%% avg_rss=%.0fMB max_rss=%.0fMB samples=%d\n", c/n, maxc, m/n, maxm, n;
	    else print "no samples captured (process not found while sampling)"}' "$OUT"
echo "agent (python) process"
awk -F, '$2=="agent"{c+=$4;m+=$5;n++; if($4>maxc)maxc=$4; if($5>maxm)maxm=$5}
	END{if(n>0) printf "avg_cpu=%.1f%% max_cpu=%.1f%% avg_rss=%.0fMB max_rss=%.0fMB samples=%d\n", c/n, maxc, m/n, maxm, n;
	    else print "no samples captured (process not found while sampling)"}' "$OUT"
echo ""
echo "Full samples written to $OUT"
