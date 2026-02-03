#!/bin/bash

# Script to run the stress tool with DDiskWriteLoad configuration.
# Outputs results in the same JSON group format as run_stress_tool_pdisk_write.sh:
#   [
#     { Label, LogMode, TestType, Params, InFlights: [...] }
#   ]
#
# Usage:
#   ./run_stress_tool_ddisk_write.sh --tool <ydb_stress_tool_path> [--duration <seconds>] [--warmup <seconds>] [--label <label>]
#     [--run-count <N>] [--inflight-from <N>] [--inflight-to <N>] [--areas-count <N>]
#     [--area-size <bytes>] [--expected-chunk-size <bytes>]
#     [--node-id <N>] [--pdisk-id <N>] [--ddisk-slot-id <N>]
#     --disk <disk_path> --output <output_file>

set -e

YDB_STRESS_TOOL=""
LABEL=""
DISK_PATH=""
OUTPUT_FILE=""

DURATION=120
WARMUP_SECONDS=15
RUN_COUNT=10
INFLIGHT_FROM=1
INFLIGHT_TO=128

# Areas behave like chunks: by default Areas count == current inflight.
AREAS_COUNT=""
AREA_SIZE=134217728
EXPECTED_CHUNK_SIZE=134217728

NODE_ID=1
PDISK_ID=1
DDISK_SLOT_ID=1
TAG=1

usage() {
    cat << EOF
Usage: $0 --tool <ydb_stress_tool_path> [--duration <seconds>] [--warmup <seconds>] [--label <label>] [--run-count <N>] [--inflight-from <N>] [--inflight-to <N>] [--areas-count <N>] [--area-size <bytes>] [--expected-chunk-size <bytes>] [--node-id <N>] [--pdisk-id <N>] [--ddisk-slot-id <N>] --disk <disk_path> --output <output_file>

Examples:
  $0 --tool ./ydb-stress-tool --disk /dev/nvme0n1p2 --output ./out.json
  $0 --tool ./ydb-stress-tool --disk /dev/nvme0n1p2 --output ./out.json --run-count $RUN_COUNT --inflight-from $INFLIGHT_FROM --inflight-to $INFLIGHT_TO
  $0 --tool ./ydb-stress-tool --disk /dev/nvme0n1p2 --output ./out.json --areas-count 32
EOF
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
        --warmup)
            WARMUP_SECONDS="$2"
            shift 2
            ;;
        --label)
            LABEL="$2"
            shift 2
            ;;
        --disk)
            DISK_PATH="$2"
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
        --areas-count)
            AREAS_COUNT="$2"
            shift 2
            ;;
        --area-size)
            AREA_SIZE="$2"
            shift 2
            ;;
        --expected-chunk-size)
            EXPECTED_CHUNK_SIZE="$2"
            shift 2
            ;;
        --node-id)
            NODE_ID="$2"
            shift 2
            ;;
        --pdisk-id)
            PDISK_ID="$2"
            shift 2
            ;;
        --ddisk-slot-id)
            DDISK_SLOT_ID="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            usage
            exit 1
            ;;
    esac
done

if [ -z "$YDB_STRESS_TOOL" ] || [ -z "$DISK_PATH" ] || [ -z "$OUTPUT_FILE" ]; then
    echo "Error: Tool, disk path and output file are required"
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

if [ ! -e "$DISK_PATH" ]; then
    echo "Error: Disk path $DISK_PATH does not exist"
    exit 1
fi

if [ ! -b "$DISK_PATH" ]; then
    echo "Error: $DISK_PATH is not a block device"
    exit 1
fi

if ! [[ "$DURATION" =~ ^[0-9]+$ ]] || [ "$DURATION" -lt 1 ]; then
    echo "Error: --duration must be an integer >= 1 (got '$DURATION')"
    usage
    exit 1
fi

if ! [[ "$WARMUP_SECONDS" =~ ^[0-9]+$ ]] || [ "$WARMUP_SECONDS" -lt 0 ]; then
    echo "Error: --warmup must be an integer >= 0 (got '$WARMUP_SECONDS')"
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

if [ -n "$AREAS_COUNT" ]; then
    if ! [[ "$AREAS_COUNT" =~ ^[0-9]+$ ]] || [ "$AREAS_COUNT" -lt 1 ]; then
        echo "Error: --areas-count must be an integer >= 1 (got '$AREAS_COUNT')"
        usage
        exit 1
    fi
fi

for v in "$AREA_SIZE" "$EXPECTED_CHUNK_SIZE" "$NODE_ID" "$PDISK_ID" "$DDISK_SLOT_ID" "$TAG"; do
    if ! [[ "$v" =~ ^[0-9]+$ ]] || [ "$v" -lt 1 ]; then
        echo "Error: numeric option values must be integers >= 1 (got '$v')"
        usage
        exit 1
    fi
done

TEMP_DIR=$(mktemp -d)
trap 'rm -rf "$TEMP_DIR"' EXIT

generate_config() {
    local areas_count=$1
    local config_file=$2

    cat > "$config_file" << EOF
DDiskTestList: {
    DDiskTestList: {
        DDiskWriteLoad: {
            Tag: $TAG
            DDiskId: {
                NodeId: $NODE_ID
                PDiskId: $PDISK_ID
                DDiskSlotId: $DDISK_SLOT_ID
            }
EOF

    for ((i = 0; i < areas_count; i++)); do
        echo "            Areas: { AreaSize: $AREA_SIZE Weight: 1 Sequential: false }" >> "$config_file"
    done

    cat >> "$config_file" << EOF
            DurationSeconds: $DURATION
            DelayBeforeMeasurementsSeconds: $WARMUP_SECONDS
            IntervalMsMin: 0
            IntervalMsMax: 0
            InFlightWrites: $INFLIGHT_TO
            ExpectedChunkSize: $EXPECTED_CHUNK_SIZE
        }
    }
}
EOF
}

EFFECTIVE_AREAS="$INFLIGHT_TO"
if [ -n "$AREAS_COUNT" ]; then
    EFFECTIVE_AREAS="$AREAS_COUNT"
fi

CONFIG_FILE="$TEMP_DIR/config_ddisk_areas_${EFFECTIVE_AREAS}.cfg"
generate_config "$EFFECTIVE_AREAS" "$CONFIG_FILE"

echo "Running test with InFlights=$INFLIGHT_FROM..$INFLIGHT_TO, RunCount=$RUN_COUNT, AreasCount=$EFFECTIVE_AREAS"
if ! RESULT=$(sudo "$YDB_STRESS_TOOL" \
    --path "$DISK_PATH" \
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

if ! GROUP_JSON=$(echo "$RESULT" | jq --arg label "$LABEL" --arg log_mode "DDISK" '. + {Label: $label, LogMode: $log_mode}' 2>&1); then
    echo "Error parsing stress tool output as JSON:"
    echo "jq error: $GROUP_JSON"
    echo "Raw output:"
    echo "$RESULT"
    exit 1
fi

echo "[$GROUP_JSON]" > "$OUTPUT_FILE"

