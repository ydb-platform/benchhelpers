#!/bin/bash

# Script to run the stress tool with different configurations
# Usage: ./run_stress_tool_pdisk_write.sh --tool <ydb_stress_tool_path> [--duration <seconds>] [--label <label>] [--log-mode <LOG_NONE|LOG_SEQUENTIAL>] [--run-count <N>] [--inflight-from <N>] [--inflight-to <N>] [--chunks-count <N>] [--disable-pdisk-encryption] --disk <disk_path> --output <output_file>

set -e

YDB_STRESS_TOOL=""
DURATION=120
LABEL=""
DISK_PATHS=()
OUTPUT_FILE=""
LOG_MODE="LOG_NONE"
RUN_COUNT=10
INFLIGHT_FROM=1
INFLIGHT_TO=32
CHUNKS_COUNT=""
CHUNK_SLOTS=32768
WARMUP_SECONDS=15
DISABLE_PDISK_ENCRYPTION=false

usage() {
    cat << EOF
Usage: $0 --tool <ydb_stress_tool_path> [--duration <seconds>] [--label <label>] [--log-mode <LOG_NONE|LOG_SEQUENTIAL>] [--run-count <N>] [--inflight-from <N>] [--inflight-to <N>] [--chunks-count <N>] [--warmup <seconds>] [--disable-pdisk-encryption] --disk <disk_path> [--disk <disk_path2> ...] --output <output_file>

Examples:
  $0 --tool ./ydb-stress-tool --disk /dev/nvme0n1 --output ./out.json
  $0 --tool ./ydb-stress-tool --disk /dev/nvme0n1 --output ./out.json --log-mode LOG_SEQUENTIAL
  $0 --tool ./ydb-stress-tool --disk /dev/nvme0n1 --output ./out.json --run-count $RUN_COUNT --inflight-from $INFLIGHT_FROM --inflight-to $INFLIGHT_TO
  $0 --tool ./ydb-stress-tool --disk /dev/nvme0n1 --output ./out.json --inflight-from 1 --inflight-to 32 --chunks-count 32
  $0 --tool ./ydb-stress-tool --disk /dev/nvme0n1 --output ./out.json --warmup 30
  $0 --tool ./ydb-stress-tool --disk /dev/nvme0n1 --disk /dev/nvme1n1 --output ./out.json
  $0 --tool ./ydb-stress-tool --disk /dev/nvme0n1 --output ./out.json --disable-pdisk-encryption
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
        --log-mode)
            LOG_MODE="$2"
            shift 2
            if [ -z "$LOG_MODE" ]; then
                echo "Error: --log-mode requires a value"
                usage
                exit 1
            fi
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
        --chunks-count)
            CHUNKS_COUNT="$2"
            shift 2
            ;;
        --warmup)
            WARMUP_SECONDS="$2"
            shift 2
            ;;
        --disable-pdisk-encryption)
            DISABLE_PDISK_ENCRYPTION=true
            shift
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
rm "$OUTPUT_FILE"  # Remove the test file we just created

if [ -z "$LABEL" ]; then
    LABEL=$(basename "$YDB_STRESS_TOOL")
fi

if ! command -v jq &> /dev/null; then
    echo "Error: jq is not installed. Please install jq to run this script."
    exit 1
fi

if ! command -v blkdiscard &> /dev/null; then
    echo "Error: blkdiscard is not installed. Please install util-linux (blkdiscard) to run this script."
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

if [ "$LOG_MODE" != "LOG_SEQUENTIAL" ] && [ "$LOG_MODE" != "LOG_NONE" ]; then
    echo "Error: Invalid --log-mode '$LOG_MODE' (allowed: LOG_NONE or LOG_SEQUENTIAL)"
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

if [ -n "$CHUNKS_COUNT" ]; then
    if ! [[ "$CHUNKS_COUNT" =~ ^[0-9]+$ ]] || [ "$CHUNKS_COUNT" -lt 1 ]; then
        echo "Error: --chunks-count must be an integer >= 1 (got '$CHUNKS_COUNT')"
        usage
        exit 1
    fi
fi

if ! [[ "$WARMUP_SECONDS" =~ ^[0-9]+$ ]] || [ "$WARMUP_SECONDS" -lt 0 ]; then
    echo "Error: --warmup must be an integer >= 0 (got '$WARMUP_SECONDS')"
    usage
    exit 1
fi

TEMP_DIR=$(mktemp -d)
trap 'rm -rf "$TEMP_DIR"' EXIT

generate_config() {
    local log_mode=$1
    local chunks_count=$2
    local config_file=$3

    cat > "$config_file" << EOF
PDiskTestList: {
    PDiskTestList: {
        PDiskWriteLoad: {
            Tag: 7
            PDiskId: 1
            PDiskGuid: 12345
            VDiskId: {
                GroupID: 1
                GroupGeneration: 5
                Ring: 1
                Domain: 1
                VDisk: 1
            }
EOF

    for ((i = 0; i < chunks_count; i++)); do
        echo "            Chunks: { Slots: $CHUNK_SLOTS Weight: 1 }" >> "$config_file"
    done

    cat >> "$config_file" << EOF
            DurationSeconds: $DURATION
            DelayBeforeMeasurementsSeconds: $WARMUP_SECONDS
            IntervalMsMin: 0
            IntervalMsMax: 0
            LogMode: $log_mode
            Sequential: false
            IsWardenlessTest: true
        }
    }
    EnableTrim: true
    DeviceInFlight: 128
}
EOF
}

EFFECTIVE_CHUNKS="$INFLIGHT_TO"
if [ -n "$CHUNKS_COUNT" ]; then
    EFFECTIVE_CHUNKS="$CHUNKS_COUNT"
fi

CONFIG_FILE="$TEMP_DIR/config_${LOG_MODE}_chunks_${EFFECTIVE_CHUNKS}.cfg"
generate_config "$LOG_MODE" "$EFFECTIVE_CHUNKS" "$CONFIG_FILE"

PATH_ARGS=()
for dp in "${DISK_PATHS[@]}"; do
    PATH_ARGS+=(--path "$dp")
done

STRESS_TOOL_ARGS=()
if [ "$DISABLE_PDISK_ENCRYPTION" = true ]; then
    STRESS_TOOL_ARGS+=(--disable-pdisk-encryption)
fi

echo "Discarding test devices before benchmark run..."
for dp in "${DISK_PATHS[@]}"; do
    echo "  sudo blkdiscard $dp"
    sudo blkdiscard "$dp"
done

echo "Running test with LogMode=$LOG_MODE, InFlights=$INFLIGHT_FROM..$INFLIGHT_TO, RunCount=$RUN_COUNT, ChunksCount=$EFFECTIVE_CHUNKS, Disks=${#DISK_PATHS[@]}"
if ! RESULT=$(sudo "$YDB_STRESS_TOOL" \
    "${PATH_ARGS[@]}" \
    "${STRESS_TOOL_ARGS[@]}" \
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

if ! GROUP_JSON=$(echo "$RESULT" | jq --arg lbl "$LABEL" --arg log_mode "$LOG_MODE" '. + {Label: $lbl, LogMode: $log_mode}' 2>&1); then
    echo "Error parsing stress tool output as JSON:"
    echo "jq error: $GROUP_JSON"
    echo "Raw output:"
    echo "$RESULT"
    exit 1
fi

echo "[$GROUP_JSON]" > "$OUTPUT_FILE"
