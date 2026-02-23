#!/bin/bash

set -u

usage() {
    echo "Usage: $0 --filename <filename> [--size-percent <1-100>] [--rand-run-count <n>]"
}

size_percent=100
rand_run_count=1
probe_iodepth=32
probe_ramp_time=10s
probe_runtime=30s
probe_percentile_list="10:50:90:95:99:99.9"
temp_files=()

cleanup_temp_files() {
    local f
    for f in "${temp_files[@]}"; do
        [[ -n "$f" ]] && rm -f "$f"
    done
}

trap cleanup_temp_files EXIT INT TERM

print_probe_summary() {
    local label="$1"
    local result_file="$2"

    python3 - "$label" "$result_file" "$probe_iodepth" <<'PY'
import json
import sys

label = sys.argv[1]
path = sys.argv[2]
iodepth = sys.argv[3]

with open(path, "r", encoding="utf-8") as f:
    payload = json.load(f)

jobs = payload.get("jobs", [])
if not jobs:
    print(f"{label}: no jobs in {path}")
    sys.exit(1)

write_stats = jobs[0].get("write", {})
bw_kib = float(write_stats.get("bw", 0.0))
iops = float(write_stats.get("iops", 0.0))
pct = write_stats.get("clat_ns", {}).get("percentile", {})

def us(key: str) -> int:
    val = pct.get(key)
    if val is None:
        return 0
    return int(float(val) / 1000.0)

print(
    f"{label}: bw={bw_kib / 1024.0:.2f} MiB/s "
    f"iops={int(iops)} "
    f"iodepth={iodepth} "
    f"p50={us('50.000000')}us "
    f"p90={us('90.000000')}us "
    f"p95={us('95.000000')}us "
    f"p99={us('99.000000')}us "
    f"p99.9={us('99.900000')}us"
)
PY
}

print_fill_summary() {
    local label="$1"
    local result_file="$2"

    python3 - "$label" "$result_file" <<'PY'
import json
import sys

label = sys.argv[1]
path = sys.argv[2]

with open(path, "r", encoding="utf-8") as f:
    payload = json.load(f)

jobs = payload.get("jobs", [])
if not jobs:
    print(f"{label}: no jobs in {path}")
    sys.exit(1)

write_stats = jobs[0].get("write", {})
bw_kib = float(write_stats.get("bw", 0.0))
iops = float(write_stats.get("iops", 0.0))
print(f"{label}: bw={bw_kib / 1024.0:.2f} MiB/s iops={int(iops)}")
PY
}

run_probe_test() {
    local label="$1"
    local result_file
    result_file="./.fill_disk_probe_${$}_${RANDOM}.json"
    temp_files+=("$result_file")

    sudo fio --name=write_latency_probe \
        --filename="$filename" \
        "${target_size_args[@]}" \
        --time_based --ramp_time="$probe_ramp_time" --runtime="$probe_runtime" \
        --ioengine=libaio --direct=1 --verify=0 --randrepeat=0 \
        --bs=4K --iodepth="$probe_iodepth" --rw=randwrite --numjobs=1 \
        --iodepth_batch_submit="$probe_iodepth" \
        --iodepth_batch_complete_max="$probe_iodepth" \
        --percentile_list="$probe_percentile_list" \
        --output-format=json \
        --output="$result_file" \
        1>/dev/null
    if [[ $? -ne 0 ]]; then
        echo "write latency probe failed ($label)"
        exit 1
    fi

    print_probe_summary "$label" "$result_file"
}

run_fill_test() {
    local fio_name="$1"
    local rw="$2"
    local bs="$3"
    local iodepth="$4"
    local label="$5"
    local result_file
    result_file="./.fill_disk_fill_${$}_${RANDOM}.json"
    temp_files+=("$result_file")

    sudo fio --name="$fio_name" \
        --filename="$filename" \
        "${target_size_args[@]}" \
        --rw="$rw" \
        --bs="$bs" \
        --iodepth="$iodepth" \
        --direct=1 \
        --numjobs=1 \
        --ioengine=libaio \
        --group_reporting \
        --output-format=json \
        --output="$result_file" \
        1>/dev/null
    if [[ $? -ne 0 ]]; then
        echo "$fio_name failed"
        exit 1
    fi

    print_fill_summary "$label" "$result_file"
}

while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --filename)
            filename="$2"
            shift 2
            ;;
        --size-percent)
            size_percent="$2"
            shift 2
            ;;
        --rand-run-count)
            rand_run_count="$2"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "Unknown parameter passed: $1"
            usage
            exit 1
            ;;
    esac
done

if [[ -z "${filename:-}" ]]; then
    echo "filename is required"
    usage
    exit 1
fi

if [[ ! -e "$filename" ]]; then
    echo "file $filename not exists"
    exit 1
fi

if ! [[ "$size_percent" =~ ^[0-9]+$ ]] || (( size_percent < 1 || size_percent > 100 )); then
    echo "size-percent must be an integer in [1,100], got: $size_percent"
    exit 1
fi

if ! [[ "$rand_run_count" =~ ^[1-9][0-9]*$ ]]; then
    echo "rand-run-count must be a positive integer, got: $rand_run_count"
    exit 1
fi

# Precondition/probe over a selected portion of target size.
target_size_args=(--size="${size_percent}%")

# Reset medium state before preconditioning when target is a block device.
if [[ -b "$filename" ]]; then
    echo "Discarding block device before preconditioning..."
    if ! which blkdiscard >/dev/null; then
        echo "blkdiscard not found, install util-linux package"
        exit 1
    fi
    sudo blkdiscard "$filename"
    if [[ $? -ne 0 ]]; then
        echo "blkdiscard failed for $filename"
        exit 1
    fi
fi

run_probe_test "Initial probe (after discard if applied)"

echo "Running test: seq_fill (${size_percent}% of target)"
run_fill_test "seq_fill" "write" "1M" "32" "seq_fill (${size_percent}% of target)"

for run_idx in $(seq 1 "$rand_run_count"); do
    echo "Random preconditioning run $run_idx/$rand_run_count"
    run_fill_test "rand_precondition" "randwrite" "8K" "32" "rand_precondition run $run_idx (${size_percent}% of target)"
    run_probe_test "Probe after random preconditioning run $run_idx"
done

