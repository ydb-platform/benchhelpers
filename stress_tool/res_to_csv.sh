#!/bin/bash

# Script to convert multiple stress tool results (from run_stress_tool_XXX.sh) to CSV format
# Usage: ./res_to_csv.sh --input <input_file> --percentile <percentile> [--output <output_file>]
# Example: ./res_to_csv.sh --input results.json --percentile p99.00

set -e

INPUT_FILE=""
PERCENTILE=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --input)
            INPUT_FILE="$2"
            shift 2
            ;;
        --percentile)
            PERCENTILE="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 --input <input_file> --percentile <percentile> [--output <output_file>]"
            echo "Example: $0 --input results.json --percentile p99.00"
            exit 1
            ;;
    esac
done

if [ -z "$INPUT_FILE" ] || [ -z "$PERCENTILE" ]; then
    echo "Error: Input file and percentile are required"
    echo "Usage: $0 --input <input_file> --percentile <percentile> [--output <output_file>]"
    echo "Example: $0 --input results.json --percentile p99.00"
    exit 1
fi

if [ ! -f "$INPUT_FILE" ]; then
    echo "Error: Input file $INPUT_FILE does not exist"
    exit 1
fi

# Check if jq is installed
if ! command -v jq &> /dev/null; then
    echo "Error: jq is not installed. Please install jq to run this script."
    exit 1
fi

# Validate percentile format
if ! [[ "$PERCENTILE" =~ ^p[0-9]+(\.[0-9]+)?$ ]]; then
    echo "Error: Invalid percentile format. Use format like p99.00"
    exit 1
fi

# Get unique sorted InFlight values from all results
HEADER=$(jq -r '.[].Results[].InFlight | tostring | select(. != "") | tonumber' "$INPUT_FILE" | sort -n | uniq | tr '\n' ',' | sed 's/,$//')
echo "What,$HEADER"

# Process each result group
jq -r --arg p "$PERCENTILE" '
    .[] | 
    . as $group |
    $group.Results | 
    sort_by(.InFlight | tonumber) | 
    map(.[$p] | sub(" us"; "")) as $values |
    [$group.Label + " " + $group.LogMode] + $values | 
    @csv' "$INPUT_FILE" 
