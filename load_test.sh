#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOT'
Usage:
  ./load_test.sh -u http://127.0.0.1:8000 -e positions -f targets.csv -c 10

CSV format (no header):
  chainId,address
Example:
  1,0x...
  42161,0x...

Options:
  -u  Base URL (required)
  -e  Endpoint: positions | liquidation (default: positions)
  -f  CSV file (required)
  -c  Concurrency (default: 5)
EOT
}

BASE_URL=""
ENDPOINT="positions"
CSV_FILE=""
CONCURRENCY=5

while getopts "u:e:f:c:h" opt; do
  case "$opt" in
    u) BASE_URL="$OPTARG" ;;
    e) ENDPOINT="$OPTARG" ;;
    f) CSV_FILE="$OPTARG" ;;
    c) CONCURRENCY="$OPTARG" ;;
    h) usage; exit 0 ;;
    *) usage; exit 1 ;;
  esac
done

if [ -z "$BASE_URL" ] || [ -z "$CSV_FILE" ]; then
  usage
  exit 1
fi

if [ ! -f "$CSV_FILE" ]; then
  echo "CSV file not found: $CSV_FILE"
  exit 1
fi

run_one() {
  local chainId="$1"
  local address="$2"
  local url
  url="$BASE_URL/api/v1/morpho/$address/$ENDPOINT?chainId=$chainId"

  local result
  result=$(curl -s -o /dev/null -w "%{http_code} %{time_total}\n" "$url")
  echo "$chainId,$address,$result"
}

export -f run_one
export BASE_URL
export ENDPOINT

TMP_OUT=$(mktemp)

grep -v '^\s*$' "$CSV_FILE" \
  | tr -d '\r' \
  | awk -F',' 'NF>=2 {print $1,$2}' \
  | xargs -n 2 -P "$CONCURRENCY" bash -c 'run_one "$@"' _ \
  | tee "$TMP_OUT"

awk '
  {count++; status[$3]++; sum+=$4; if(min==0||$4<min)min=$4; if($4>max)max=$4; times[count]=$4}
  END {
    if(count==0){print "No requests."; exit}
    avg=sum/count
    # simple p95
    n=count
    asort(times)
    p95_idx=int(n*0.95)
    if(p95_idx<1)p95_idx=1
    p95=times[p95_idx]
    printf("\nSummary: count=%d avg=%.4fs p95=%.4fs min=%.4fs max=%.4fs\n", count, avg, p95, min, max)
    printf("Status codes:")
    for (k in status) printf(" %s=%d", k, status[k])
    printf("\n")
  }
' "$TMP_OUT"

rm -f "$TMP_OUT"
