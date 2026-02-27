#!/bin/bash

set -e

YDB_STRESS_TOOL=""
LABEL=""
DISK_PATHS=()
OUTPUT_FILE=""

# UringRouterTestList defaults.
DURATION=30
REQUEST_SIZE=4096
QUEUE_DEPTH=128
USE_ALIGNED_DATA="true"
USE_WRITE_FIXED="true"

RUN_COUNT=10
INFLIGHT_FROM=1
INFLIGHT_TO=128
LOG_MODE="URING"

usage() {
    cat << EOF
Usage: $0 --tool <ydb_stress_tool_path> [--duration <seconds>] [--label <label>] [--run-count <N>] [--inflight-from <N>] [--inflight-to <N>] [--request-size <bytes>] [--queue-depth <N>] [--use-aligned-data <true|false>] [--use-write-fixed <true|false>] --disk <disk_path> [--disk <disk_path2> ...] --output <output_file>

Examples:
  $0 --tool ./ydb-stress-tool --disk /dev/nvme0n1 --output ./out_uring.json
  $0 --tool ./ydb-stress-tool --disk /dev/nvme0n1 --output ./out_uring.json --duration 60 --run-count 3 --inflight-from 1 --inflight-to 64
  $0 --tool ./ydb-stress-tool --disk /dev/nvme0n1 --output ./out_uring.json --request-size 4096 --queue-depth 128 --use-aligned-data true --use-write-fixed true
  $0 --tool ./ydb-stress-tool --disk /dev/nvme0n1 --disk /dev/nvme1n1 --output ./out_uring_2dev.json
EOF
}

normalize_bool() {
    local v
    v=$(echo "$1" | tr '[:upper:]' '[:lower:]')
    if [ "$v" != "true" ] && [ "$v" != "false" ]; then
        return 1
    fi
    echo "$v"
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --tool)
            YDB_STRESS_TOOL="$2"
            shift 2
            ;;
        --duration)
            DURATION="$2"
            shift 2
            ;;
        --label)
            LABEL="$2"
            shift 2
            ;;
        --disk)
            DISK_PATHS+=("$2")
            shift 2
            ;;
        --output)
            OUTPUT_FILE="$2"
            shift 2
            ;;
        --run-count)
            RUN_COUNT="$2"
            shift 2
            ;;
        --inflight-from)
            INFLIGHT_FROM="$2"
            shift 2
            ;;
        --inflight-to)
            INFLIGHT_TO="$2"
            shift 2
            ;;
        --request-size)
            REQUEST_SIZE="$2"
            shift 2
            ;;
        --queue-depth)
            QUEUE_DEPTH="$2"
            shift 2
            ;;
        --use-aligned-data)
            USE_ALIGNED_DATA="$2"
            shift 2
            ;;
        --use-write-fixed)
            USE_WRITE_FIXED="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            usage
            exit 1
            ;;
    esac
done

if [ -z "$YDB_STRESS_TOOL" ] || [ ${#DISK_PATHS[@]} -eq 0 ] || [ -z "$OUTPUT_FILE" ]; then
    echo "Error: Tool, at least one --disk, and output file are required"
    usage
    exit 1
fi

if [ -e "$OUTPUT_FILE" ]; then
    echo "Error: Output file $OUTPUT_FILE already exists"
    exit 1
fi

OUTPUT_DIR=$(dirname "$OUTPUT_FILE")
if [ ! -d "$OUTPUT_DIR" ]; then
    echo "Error: Output directory $OUTPUT_DIR does not exist"
    exit 1
fi

if ! touch "$OUTPUT_FILE" 2>/dev/null; then
    echo "Error: Cannot create output file $OUTPUT_FILE (permission denied)"
    exit 1
fi
rm "$OUTPUT_FILE"

if [ -z "$LABEL" ]; then
    LABEL=$(basename "$YDB_STRESS_TOOL")
fi

if ! command -v jq &> /dev/null; then
    echo "Error: jq is not installed. Please install jq to run this script."
    exit 1
fi

if [ ! -f "$YDB_STRESS_TOOL" ]; then
    echo "Error: Tool not found at $YDB_STRESS_TOOL"
    exit 1
fi

if [ ! -x "$YDB_STRESS_TOOL" ]; then
    echo "Error: Tool at $YDB_STRESS_TOOL is not executable"
    exit 1
fi

for dp in "${DISK_PATHS[@]}"; do
    if [ ! -e "$dp" ]; then
        echo "Error: Disk path $dp does not exist"
        exit 1
    fi
    if [ ! -b "$dp" ]; then
        echo "Error: $dp is not a block device"
        exit 1
    fi
done

if ! [[ "$DURATION" =~ ^[0-9]+$ ]] || [ "$DURATION" -lt 1 ]; then
    echo "Error: --duration must be an integer >= 1 (got '$DURATION')"
    usage
    exit 1
fi

if ! [[ "$RUN_COUNT" =~ ^[0-9]+$ ]] || [ "$RUN_COUNT" -lt 1 ]; then
    echo "Error: --run-count must be an integer >= 1 (got '$RUN_COUNT')"
    usage
    exit 1
fi

if ! [[ "$INFLIGHT_FROM" =~ ^[0-9]+$ ]] || [ "$INFLIGHT_FROM" -lt 1 ]; then
    echo "Error: --inflight-from must be an integer >= 1 (got '$INFLIGHT_FROM')"
    usage
    exit 1
fi

if ! [[ "$INFLIGHT_TO" =~ ^[0-9]+$ ]] || [ "$INFLIGHT_TO" -lt 1 ]; then
    echo "Error: --inflight-to must be an integer >= 1 (got '$INFLIGHT_TO')"
    usage
    exit 1
fi

if [ "$INFLIGHT_FROM" -gt "$INFLIGHT_TO" ]; then
    echo "Error: --inflight-from ($INFLIGHT_FROM) must be <= --inflight-to ($INFLIGHT_TO)"
    usage
    exit 1
fi

if ! [[ "$REQUEST_SIZE" =~ ^[0-9]+$ ]] || [ "$REQUEST_SIZE" -lt 1 ]; then
    echo "Error: --request-size must be an integer >= 1 (got '$REQUEST_SIZE')"
    usage
    exit 1
fi

if ! [[ "$QUEUE_DEPTH" =~ ^[0-9]+$ ]] || [ "$QUEUE_DEPTH" -lt 1 ]; then
    echo "Error: --queue-depth must be an integer >= 1 (got '$QUEUE_DEPTH')"
    usage
    exit 1
fi

if [ "$QUEUE_DEPTH" -lt "$INFLIGHT_TO" ]; then
    echo "Error: --queue-depth ($QUEUE_DEPTH) must be >= --inflight-to ($INFLIGHT_TO)"
    usage
    exit 1
fi

if ! USE_ALIGNED_DATA_NORM=$(normalize_bool "$USE_ALIGNED_DATA"); then
    echo "Error: --use-aligned-data must be true or false (got '$USE_ALIGNED_DATA')"
    usage
    exit 1
fi

if ! USE_WRITE_FIXED_NORM=$(normalize_bool "$USE_WRITE_FIXED"); then
    echo "Error: --use-write-fixed must be true or false (got '$USE_WRITE_FIXED')"
    usage
    exit 1
fi

TEMP_DIR=$(mktemp -d)
trap 'rm -rf "$TEMP_DIR"' EXIT

CONFIG_FILE="$TEMP_DIR/config_uring_qd_${QUEUE_DEPTH}.cfg"
cat > "$CONFIG_FILE" << EOF
UringRouterTestList: {
    DurationSeconds: $DURATION
    RequestSize: $REQUEST_SIZE
    QueueDepth: $QUEUE_DEPTH
    UseAlignedData: $USE_ALIGNED_DATA_NORM
    UseWriteFixed: $USE_WRITE_FIXED_NORM
}
EOF

PATH_ARGS=()
for dp in "${DISK_PATHS[@]}"; do
    PATH_ARGS+=(--path "$dp")
done

echo "Running uring test with InFlights=$INFLIGHT_FROM..$INFLIGHT_TO, RunCount=$RUN_COUNT, RequestSize=$REQUEST_SIZE, QueueDepth=$QUEUE_DEPTH, Disks=${#DISK_PATHS[@]}"
if ! RESULT=$(sudo "$YDB_STRESS_TOOL" \
    "${PATH_ARGS[@]}" \
    --type NVME \
    --no-logo \
    --cfg "$CONFIG_FILE" \
    --output-format json \
    --run-count "$RUN_COUNT" \
    --inflight-from "$INFLIGHT_FROM" \
    --inflight-to "$INFLIGHT_TO" 2>&1); then
    echo "Error running stress tool:"
    echo "$RESULT"
    exit 1
fi

if ! GROUP_JSON=$(echo "$RESULT" | jq --arg label "$LABEL" --arg log_mode "$LOG_MODE" '. + {Label: $label, LogMode: $log_mode}' 2>&1); then
    echo "Error parsing stress tool output as JSON:"
    echo "jq error: $GROUP_JSON"
    echo "Raw output:"
    echo "$RESULT"
    exit 1
fi

echo "[$GROUP_JSON]" > "$OUTPUT_FILE"
