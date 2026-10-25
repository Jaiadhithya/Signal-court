#!/usr/bin/env bash
# Run the live-demo command, then verify precisely the report it produced.
set -uo pipefail
SECONDS=0
log=''

finish() {
    status=$?
    trap - EXIT
    printf 'Total wall seconds: %s\n' "$SECONDS"
    if [[ -n "$log" ]]; then
        rm -f -- "$log"
    fi
    exit "$status"
}
trap finish EXIT

mode=warm
company=''
for arg in "$@"; do
    case "$arg" in
        --fallback) mode=replay ;;
        --*) printf 'Unknown option: %s\n' "$arg" >&2; exit 2 ;;
        *)
            if [[ -n "$company" ]]; then
                printf 'Usage: %s [--fallback] "Company name"\n' "$0" >&2
                exit 2
            fi
            company=$arg
            ;;
    esac
done
if [[ -z "$company" ]]; then
    printf 'Usage: %s [--fallback] "Company name"\n' "$0" >&2
    exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd) || exit 1
cd -- "$script_dir/.." || exit 1
log=$(mktemp "${TMPDIR:-/tmp}/signal-rehearse.XXXXXXXX") || exit 1

# Unbuffer Python progress even though tee also captures the printed report path.
PYTHONUNBUFFERED=1 uv run --frozen signal run --company "$company" --mode "$mode" --open 2>&1 | tee "$log"
statuses=("${PIPESTATUS[@]}")
if (( statuses[0] != 0 )); then
    exit "${statuses[0]}"
fi
if (( statuses[1] != 0 )); then
    exit "${statuses[1]}"
fi

report_path=''
while IFS= read -r line || [[ -n "$line" ]]; do
    line=${line%$'\r'}
    case "$line" in
        */report.html|*\\report.html|*/finding.html|*\\finding.html) report_path=$line ;;
    esac
done < "$log"
if [[ -z "$report_path" ]]; then
    printf 'Run succeeded without printing a report path; verification skipped.\n' >&2
    exit 1
fi

# Normalize separators without evaluating or splitting the printed path.
report_path=${report_path//\\//}
run_dir=${report_path%/*}
if [[ -z "$run_dir" ]]; then
    run_dir=/
elif [[ "$run_dir" == [A-Za-z]: ]]; then
    run_dir+=/
fi
uv run --frozen signal verify "$run_dir"
exit "$?"
