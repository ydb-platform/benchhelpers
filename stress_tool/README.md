# YDB Stress Tool Suite

Tools for running [ydb_stress_tool](https://github.com/ydb-platform/ydb/tree/main/ydb/tools/stress_tool) and analyzing results.

## Prerequisites

- `jq >= 1.7` command-line tool must be installed (version)
- Root/sudo access for running the stress tool
- A block device for testing

## Scripts

### 1. run_stress_tool.sh

Runs the YDB stress tool with different configurations and collects results.

#### Usage
```bash
./run_stress_tool.sh --tool <ydb_stress_tool_path> [--duration <seconds>] [--label <label>] --disk <disk_path> --output <output_file>
```

#### Arguments
- `--tool`: Path to the ydb_stress_tool executable (required)
- `--duration`: Test duration in seconds (default: 120)
- `--label`: Label for the test results (default: tool filename)
- `--disk`: Path to the block device for testing (required)
- `--output`: Path to the output JSON file (required)

#### Example
```bash
./run_stress_tool.sh --tool ./ydb_stress_tool --duration 120 --label "Regular" --disk /dev/nvme_01 --output result.json
```

### 2. res_to_csv.sh

Converts the JSON results from run_stress_tool.sh into CSV format.

#### Usage
```bash
./res_to_csv.sh --input <input_file> --percentile <percentile> [--output <output_file>]
```

#### Arguments
- `--input`: Path to the JSON results file (required)
- `--percentile`: Percentile to extract (e.g., p99.00) (required)
- `--output`: Path to the output CSV file (default: output.csv)

#### Example
```bash
./res_to_csv.sh --input result.json --percentile p99.00 --output results.csv
```

## Complete Workflow Example

1. Run the stress test:
```bash
./run_stress_tool.sh --tool ./ydb_stress_tool --duration 120 --label "Regular" --disk /dev/disk/by-partlabel/kikimr_nvme_01 --output result.json
```

2. Convert results to CSV:
```bash
./res_to_csv.sh --input result.json --percentile p99.00 --output results.csv
```

## Output Format

### JSON Output (run_stress_tool.sh)
The JSON output contains results for different log modes (LOG_SEQUENTIAL and LOG_NONE) with various InFlight values:
```json
[
  {
    "Label": "Regular",
    "LogMode": "LOG_SEQUENTIAL",
    "Results": [
      {
        "InFlight": 1,
        "p99.00": "123 us",
        ...
      },
      ...
    ]
  },
  ...
]
```

### CSV Output (res_to_csv.sh)
The CSV output shows the specified percentile values for each log mode:
```
LABEL_LOG,1,2
Regular_LOG_SEQUENTIAL,123,456
Regular_LOG_NONE,789,101
```
