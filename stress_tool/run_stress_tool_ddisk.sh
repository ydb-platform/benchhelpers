#!/bin/bash

# Script to run the stress tool with DDiskLoad configuration (reads or writes).
# Outputs results in the same JSON group format as run_stress_tool_pdisk_write.sh:
#   [
#     { Label, LogMode, TestType, Params, InFlights: [...] }
#   ]
#
# Usage:
#   ./run_stress_tool_ddisk.sh --tool <ydb_stress_tool_path> [--reads] [--duration <seconds>] [--warmup <seconds>] [--label <label>]
#     [--run-count <N>] [--inflight-from <N>] [--inflight-to <N>] [--areas-count <N>]
#     [--area-size <bytes>] [--expected-chunk-size <bytes>] [--init-inflight <N>]
#     [--background-write-ratio <0..1>]... [--background-write-size-kib <N>]
#     [--disable-ddisk-checksums] [--config-output-dir <directory>]
#     [--node-id <N>] [--pdisk-id <N>] [--ddisk-slot-id <N>]
#     --disk <disk_path> --output <output_file>

set -e

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

YDB_STRESS_TOOL=""
LABEL=""
DISK_PATHS=()
OUTPUT_FILE=""

DURATION=30
WARMUP_SECONDS=15
RUN_COUNT=1
INFLIGHT_FROM=1
INFLIGHT_TO=128

IS_READ_LOAD=false
BACKGROUND_WRITE_RATIOS=()
BACKGROUND_WRITE_SIZE_KIB=4
DISABLE_DDISK_CHECKSUMS=false
CONFIG_OUTPUT_DIR=""

# NBS has 32 direct block groups in a 4 GiB region. Keep this workload at
# 32 x 128 MiB areas unless the caller explicitly requests something else.
AREAS_COUNT=32
AREA_SIZE=134217728
EXPECTED_CHUNK_SIZE=134217728
IO_SIZE_BYTES=4096
INIT_INFLIGHT=128

NODE_ID=1
PDISK_ID=1
DDISK_SLOT_ID=1
TAG=1

usage() {
    cat << EOF
Usage: $0 --tool <ydb_stress_tool_path> [--reads] [--duration <seconds>] [--warmup <seconds>] [--label <label>] [--run-count <N>] [--inflight-from <N>] [--inflight-to <N>] [--areas-count <N>] [--area-size <bytes>] [--expected-chunk-size <bytes>] [--init-inflight <N>] [--background-write-ratio <0..1>]... [--background-write-size-kib <N>] [--disable-ddisk-checksums] [--config-output-dir <directory>] --disk <disk_path> [--disk <disk_path2> ...] --output <output_file>

Options:
  --reads              Perform read load instead of write load.
  --background-write-ratio
                       Background writes per measured read. Repeat to run a
                       matrix; defaults to 0. Only valid with --reads.
  --background-write-size-kib
                       Background write size in KiB (default: 4).
  --disable-ddisk-checksums
                       Pass the same option to ydb_stress_tool.
  --config-output-dir  Also keep each generated config in this directory.

Examples:
  $0 --tool ./ydb-stress-tool --disk /dev/nvme0n1p2 --output ./out.json
  $0 --tool ./ydb-stress-tool --reads --disk /dev/nvme0n1p2 --output ./out.json \
     --background-write-ratio 0.0 --background-write-ratio 0.1 \
     --background-write-ratio 0.25 --background-write-ratio 0.5 \
     --background-write-ratio 1.0
  $0 --tool ./ydb-stress-tool --disk /dev/nvme0n1p2 --output ./out.json --run-count $RUN_COUNT --inflight-from $INFLIGHT_FROM --inflight-to $INFLIGHT_TO
  $0 --tool ./ydb-stress-tool --disk /dev/nvme0n1p2 --output ./out.json --areas-count 32
  $0 --tool ./ydb-stress-tool --disk /dev/nvme0n1p2 --disk /dev/nvme1n1p2 --output ./out.json
EOF
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --tool)
            YDB_STRESS_TOOL="$2"
            shift 2
            ;;
        --reads)
            IS_READ_LOAD=true
            shift
            ;;
        --background-write-ratio)
            BACKGROUND_WRITE_RATIOS+=("$2")
            shift 2
            ;;
        --background-write-size-kib)
            BACKGROUND_WRITE_SIZE_KIB="$2"
            shift 2
            ;;
        --disable-ddisk-checksums)
            DISABLE_DDISK_CHECKSUMS=true
            shift
            ;;
        --config-output-dir)
            CONFIG_OUTPUT_DIR="$2"
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
        --init-inflight)
            INIT_INFLIGHT="$2"
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

if ! command -v python3 &> /dev/null; then
    echo "Error: python3 is required to normalize and combine results."
    exit 1
fi

if [ "$IS_READ_LOAD" != true ] && ! command -v blkdiscard &> /dev/null; then
    echo "Error: blkdiscard is not installed. Please install util-linux (blkdiscard) to run this script."
    exit 1
fi

if [ ! -f "$SCRIPT_DIR/normalize_stress_result.py" ]; then
    echo "Error: helper not found: $SCRIPT_DIR/normalize_stress_result.py"
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

if ! [[ "$AREAS_COUNT" =~ ^[0-9]+$ ]] || [ "$AREAS_COUNT" -lt 1 ]; then
    echo "Error: --areas-count must be an integer >= 1 (got '$AREAS_COUNT')"
    usage
    exit 1
fi

for v in "$AREA_SIZE" "$EXPECTED_CHUNK_SIZE" "$INIT_INFLIGHT" "$BACKGROUND_WRITE_SIZE_KIB" "$NODE_ID" "$PDISK_ID" "$DDISK_SLOT_ID" "$TAG"; do
    if ! [[ "$v" =~ ^[0-9]+$ ]] || [ "$v" -lt 1 ]; then
        echo "Error: numeric option values must be integers >= 1 (got '$v')"
        usage
        exit 1
    fi
done

if [ "$BACKGROUND_WRITE_SIZE_KIB" -lt 4 ] || [ $((BACKGROUND_WRITE_SIZE_KIB & (BACKGROUND_WRITE_SIZE_KIB - 1))) -ne 0 ]; then
    echo "Error: --background-write-size-kib must be a power of two and >= 4"
    exit 1
fi

BACKGROUND_WRITE_SIZE_BYTES=$((BACKGROUND_WRITE_SIZE_KIB * 1024))
if [ $((AREA_SIZE % IO_SIZE_BYTES)) -ne 0 ] || [ $((EXPECTED_CHUNK_SIZE % IO_SIZE_BYTES)) -ne 0 ]; then
    echo "Error: area and expected chunk sizes must be divisible by IoSizeBytes=$IO_SIZE_BYTES"
    exit 1
fi
if [ $((AREA_SIZE % BACKGROUND_WRITE_SIZE_BYTES)) -ne 0 ] || [ $((EXPECTED_CHUNK_SIZE % BACKGROUND_WRITE_SIZE_BYTES)) -ne 0 ]; then
    echo "Error: area and expected chunk sizes must be divisible by background write size"
    exit 1
fi

if [ "$IS_READ_LOAD" != true ] && [ ${#BACKGROUND_WRITE_RATIOS[@]} -gt 0 ]; then
    echo "Error: --background-write-ratio is only valid with --reads"
    exit 1
fi
if [ ${#BACKGROUND_WRITE_RATIOS[@]} -eq 0 ]; then
    BACKGROUND_WRITE_RATIOS=("0.0")
fi
declare -A SEEN_BACKGROUND_WRITE_RATIOS=()
for ratio in "${BACKGROUND_WRITE_RATIOS[@]}"; do
    if ! python3 -c 'import math, sys; v=float(sys.argv[1]); raise SystemExit(0 if math.isfinite(v) and 0 <= v <= 1 else 1)' "$ratio" 2>/dev/null; then
        echo "Error: --background-write-ratio must be a finite number in [0, 1] (got '$ratio')"
        exit 1
    fi
    normalized_ratio=$(python3 -c 'import sys; print(format(float(sys.argv[1]), ".9g"))' "$ratio")
    if [ -n "${SEEN_BACKGROUND_WRITE_RATIOS[$normalized_ratio]}" ]; then
        echo "Error: duplicate --background-write-ratio value: $ratio"
        exit 1
    fi
    SEEN_BACKGROUND_WRITE_RATIOS[$normalized_ratio]=1
done

if [ -n "$CONFIG_OUTPUT_DIR" ]; then
    if [ ! -d "$CONFIG_OUTPUT_DIR" ] || [ ! -w "$CONFIG_OUTPUT_DIR" ]; then
        echo "Error: --config-output-dir must be an existing writable directory"
        exit 1
    fi
fi

TEMP_DIR=$(mktemp -d)
trap 'rm -rf "$TEMP_DIR"' EXIT

generate_config() {
    local areas_count=$1
    local background_write_ratio=$2
    local config_file=$3

    cat > "$config_file" << EOF
DDiskTestList: {
    DDiskTestList: {
        DDiskLoad: {
            Tag: $TAG
            DDiskId: {
                NodeId: $NODE_ID
                PDiskId: $PDISK_ID
                DDiskSlotId: $DDISK_SLOT_ID
            }
EOF

    for ((i = 0; i < areas_count; i++)); do
        echo "            Areas: { AreaSize: $AREA_SIZE Weight: 1 Sequential: false InitType: INIT_ZEROES_FULL }" >> "$config_file"
    done

    local extra_fields=""
    if [ "$IS_READ_LOAD" = true ]; then
        extra_fields="
            IsReadLoad: true
            BackgroundWriteRatio: $background_write_ratio
            BackgroundWriteSizeKiB: $BACKGROUND_WRITE_SIZE_KIB"
    fi

    cat >> "$config_file" << EOF
            DurationSeconds: $DURATION
            DelayBeforeMeasurementsSeconds: $WARMUP_SECONDS
            IntervalMsMin: 0
            IntervalMsMax: 0
            InFlight: $INFLIGHT_TO
            InitInFlight: $INIT_INFLIGHT
            IoSizeBytes: $IO_SIZE_BYTES
            ExpectedChunkSize: $EXPECTED_CHUNK_SIZE$extra_fields
        }
    }
}
EOF
}

EFFECTIVE_AREAS="$AREAS_COUNT"

PATH_ARGS=()
for dp in "${DISK_PATHS[@]}"; do
    PATH_ARGS+=(--path "$dp")
done

if [ "$IS_READ_LOAD" = true ]; then
    LOAD_KIND="read"
    LOG_MODE="DDISK_READ"
else
    LOAD_KIND="write"
    LOG_MODE="DDISK_WRITE"
fi

if [ "$IS_READ_LOAD" != true ]; then
    echo "Discarding test devices before benchmark run..."
    for dp in "${DISK_PATHS[@]}"; do
        echo "  sudo blkdiscard $dp"
        sudo blkdiscard "$dp"
    done
fi

CHECKSUM_ARGS=()
CHECKSUM_MODE="enabled"
if [ "$DISABLE_DDISK_CHECKSUMS" = true ]; then
    CHECKSUM_ARGS+=(--disable-ddisk-checksums)
    CHECKSUM_MODE="disabled"
fi

GROUP_FILES=()
for ratio in "${BACKGROUND_WRITE_RATIOS[@]}"; do
    ratio_slug=${ratio//./p}
    config_name="config_ddisk_areas_${EFFECTIVE_AREAS}_bg_${ratio_slug}.cfg"
    if [ -n "$CONFIG_OUTPUT_DIR" ]; then
        CONFIG_FILE="$CONFIG_OUTPUT_DIR/$config_name"
    else
        CONFIG_FILE="$TEMP_DIR/$config_name"
    fi
    generate_config "$EFFECTIVE_AREAS" "$ratio" "$CONFIG_FILE"

    RAW_JSON="$TEMP_DIR/result_bg_${ratio_slug}.json"
    STDERR_LOG="$TEMP_DIR/result_bg_${ratio_slug}.stderr.log"
    GROUP_JSON="$TEMP_DIR/group_bg_${ratio_slug}.json"

    echo "Running DDisk ${LOAD_KIND}: BackgroundWriteRatio=$ratio, Checksums=$CHECKSUM_MODE, InFlights=$INFLIGHT_FROM..$INFLIGHT_TO, RunCount=$RUN_COUNT, AreasCount=$EFFECTIVE_AREAS, Disks=${#DISK_PATHS[@]}"
    if ! sudo "$YDB_STRESS_TOOL" \
        "${PATH_ARGS[@]}" \
        --type NVME \
        --name "DDisk ${LOAD_KIND} bg=${ratio} checksums=${CHECKSUM_MODE}" \
        --no-logo \
        --cfg "$CONFIG_FILE" \
        --output-format json \
        --run-count "$RUN_COUNT" \
        --inflight-from "$INFLIGHT_FROM" \
        --inflight-to "$INFLIGHT_TO" \
        "${CHECKSUM_ARGS[@]}" >"$RAW_JSON" 2>"$STDERR_LOG"; then
        echo "Error running stress tool for BackgroundWriteRatio=$ratio:"
        if [ -s "$STDERR_LOG" ]; then
            cat "$STDERR_LOG"
        fi
        exit 1
    fi

    if ! python3 "$SCRIPT_DIR/normalize_stress_result.py" \
        --input "$RAW_JSON" \
        --output "$GROUP_JSON" \
        --label "$LABEL" \
        --log-mode "$LOG_MODE" \
        --background-write-ratio "$ratio" \
        --background-write-size-kib "$BACKGROUND_WRITE_SIZE_KIB" \
        --areas-count "$EFFECTIVE_AREAS" \
        --init-inflight "$INIT_INFLIGHT" \
        --checksums "$CHECKSUM_MODE"; then
        echo "Error parsing stress tool JSON for BackgroundWriteRatio=$ratio"
        if [ -s "$STDERR_LOG" ]; then
            echo "Stress-tool stderr:"
            cat "$STDERR_LOG"
        fi
        exit 1
    fi
    GROUP_FILES+=("$GROUP_JSON")
done

python3 - "$OUTPUT_FILE" "${GROUP_FILES[@]}" <<'PY'
import json
import sys

output_path = sys.argv[1]
groups = []
for path in sys.argv[2:]:
    with open(path, "r", encoding="utf-8") as stream:
        groups.append(json.load(stream))
with open(output_path, "w", encoding="utf-8") as stream:
    json.dump(groups, stream, indent=2, sort_keys=True)
    stream.write("\n")
PY

echo "Wrote $OUTPUT_FILE"
