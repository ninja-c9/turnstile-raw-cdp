#!/usr/bin/env bash
# Example 05: Automated multi-domain account farming loop with exponential backoff.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

TARGET="${TARGET:-500}"
MAX_HOURS="${MAX_HOURS:-48}"
OUT="${OUT:-keys.jsonl}"
LOG="${LOG:-farm.log}"

# Override DOMAINS="domain1.com domain2.com" in environment or .env
read -r -a DOMAINS <<< "${DOMAINS:-example.com}"

have() { [ -f "$OUT" ] && wc -l < "$OUT" | tr -d ' ' || echo 0; }

t0=$(date +%s)
end=$((t0 + MAX_HOURS * 3600))
n=$(have)
fails=0

echo "== iterafarm start target=$TARGET have=$n" >> "$LOG"
while [ "$n" -lt "$TARGET" ] && [ "$(date +%s)" -lt "$end" ]; do
  d="${DOMAINS[$((RANDOM % ${#DOMAINS[@]}))]}"
  if timeout 120 python3 04_iteracompute_register.py --domain "$d" --output "$OUT" >>"$LOG" 2>&1; then
    n=$(have)
    fails=0
    echo "[$(date +%H:%M:%S)] OK $n/$TARGET ($((n * 30))M tokens/mo)" >> "$LOG"
    sleep 48
  else
    fails=$((fails + 1))
    echo "[$(date +%H:%M:%S)] fail #$fails, sleeping $((30 * fails))s" >> "$LOG"
    sleep $((30 * fails))
    if [ "$fails" -ge 5 ]; then
      echo "[$(date +%H:%M:%S)] 5 consecutive failures — cooling down 10m" >> "$LOG"
      sleep 600
      fails=0
    fi
  fi
done
echo "== iterafarm done have=$(have)/$TARGET" >> "$LOG"
