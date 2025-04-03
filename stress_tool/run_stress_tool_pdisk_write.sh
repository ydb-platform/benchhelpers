#!/bin/bash

# Script to run the stress tool with different configurations
# Usage: ./run_stress_tool_pdisk_write.sh --tool <ydb_stress_tool_path> [--duration <seconds>] [--label <label>] --disk <disk_path> --output <output_file>

set -e

YDB_STRESS_TOOL=""
DURATION=120
LABEL=""
DISK_PATH=""
OUTPUT_FILE=""

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
            DISK_PATH="$2"
            shift 2
            ;;
        --output)
            OUTPUT_FILE="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 --tool <ydb_stress_tool_path> [--duration <seconds>] [--label <label>] --disk <disk_path> --output <output_file>"
            exit 1
            ;;
    esac
done

if [ -z "$YDB_STRESS_TOOL" ] || [ -z "$DISK_PATH" ] || [ -z "$OUTPUT_FILE" ]; then
    echo "Error: Tool, disk path and output file are required"
    echo "Usage: $0 --tool <ydb_stress_tool_path> [--duration <seconds>] [--label <label>] --disk <disk_path> --output <output_file>"
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

TEMP_DIR=$(mktemp -d)
trap 'rm -rf "$TEMP_DIR"' EXIT

generate_config() {
    local log_mode=$1
    local in_flight=$2
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
            Chunks: { Slots: 16384 Weight: 1 }
            Chunks: { Slots: 16384 Weight: 1 }
            Chunks: { Slots: 16384 Weight: 1 }
            Chunks: { Slots: 16384 Weight: 1 }
            Chunks: { Slots: 16384 Weight: 1 }
            Chunks: { Slots: 16384 Weight: 1 }
            Chunks: { Slots: 16384 Weight: 1 }
            Chunks: { Slots: 16384 Weight: 1 }
            DurationSeconds: $DURATION
            IntervalMsMin: 0
            IntervalMsMax: 0
            InFlightWrites: $in_flight
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

declare -a ALL_RESULTS

for LOG_MODE in "LOG_SEQUENTIAL" "LOG_NONE"; do
    MODE_RESULTS=()
    
    for IN_FLIGHT in 1 2 3 4 5 6 7 8 16 24 32; do
        CONFIG_FILE="$TEMP_DIR/config_${LOG_MODE}_${IN_FLIGHT}.cfg"
        generate_config "$LOG_MODE" "$IN_FLIGHT" "$CONFIG_FILE"
        
        echo "Running test with LogMode=$LOG_MODE, InFlightWrites=$IN_FLIGHT"
        RESULT=$(sudo taskset -c 0-16 "$YDB_STRESS_TOOL" --path "$DISK_PATH" --type NVME --output-format json --no-logo --cfg "$CONFIG_FILE")
        
        MODE_RESULTS+=("$RESULT")
    done
    
    if ! MODE_COMBINED=$(echo "${MODE_RESULTS[@]}" | jq -s 'flatten' 2>&1); then
        echo "Error combining results for LogMode=$LOG_MODE:"
        echo "jq error: $MODE_COMBINED"
        echo "Individual results: ${MODE_RESULTS[@]}"
        exit 1
    fi
    
    if ! MODE_FINAL=$(echo "$MODE_COMBINED" | jq --arg label "$LABEL" --arg log_mode "$LOG_MODE" '{Label: $label, LogMode: $log_mode, Results: .}' 2>&1); then
        echo "Error creating final structure for LogMode=$LOG_MODE:"
        echo "jq error: $MODE_FINAL"
        exit 1
    fi
    
    ALL_RESULTS+=("$MODE_FINAL")
done

if ! FINAL_RESULT=$(echo "${ALL_RESULTS[@]}" | jq -s '.' 2>&1); then
    echo "Error combining final results:"
    echo "jq error: $FINAL_RESULT"
    echo "Individual results: ${ALL_RESULTS[@]}"
    exit 1
fi

echo "$FINAL_RESULT" > "$OUTPUT_FILE"
