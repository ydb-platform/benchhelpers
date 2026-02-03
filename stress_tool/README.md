# YDB Stress Tool Suite

Tools for running [ydb_stress_tool](https://github.com/ydb-platform/ydb/tree/main/ydb/tools/stress_tool) and analyzing results.

## Prerequisites

- `jq >= 1.7` command-line tool must be installed (version)
- Root/sudo access for running the stress tool
- A block device for testing
- Python 3 (for plotting)
- `matplotlib` (for plotting, install with `pip3 install matplotlib`)

## Scripts

### 1. run_stress_tool_pdisk_write.sh

Runs the YDB stress tool PDisk write load test and collects results.
The script runs **a single log mode** per invocation (default: `LOG_NONE`).

#### Usage
```bash
./run_stress_tool_pdisk_write.sh --tool <ydb_stress_tool_path> \
  [--duration <seconds>] \
  [--label <label>] \
  [--log-mode <LOG_NONE|LOG_SEQUENTIAL>] \
  [--run-count <N>] \
  [--inflight-from <N>] \
  [--inflight-to <N>] \
  [--chunks-count <N>] \
  [--warmup <seconds>] \
  --disk <disk_path> \
  --output <output_file>
```

#### Arguments
- `--tool`: Path to the ydb_stress_tool executable (required)
- `--duration`: Test duration in seconds (default: 120)
- `--label`: Label for the test results (default: tool filename)
- `--log-mode`: `LOG_NONE` (default) or `LOG_SEQUENTIAL`
- `--run-count`: How many times to run each inflight value (default: 1)
- `--inflight-from`: Starting inflight value (default: 1)
- `--inflight-to`: Ending inflight value (default: 32)
- `--chunks-count`: Number of `Chunks` in config. Default: equals max `InFlight` (`--inflight-to`) (if set, forced for all inflights)
- `--warmup`: Delay before measurements in seconds (default: 15)
- `--disk`: Path to the block device for testing (required)
- `--output`: Path to the output JSON file (required)

#### CPU isolation (recommended)
For stable results it is recommended to isolate CPU resources for the benchmark. Two common options:

- **Exclusive cpuset cgroup (recommended)**:
- **Or Run under `taskset`**:

```bash
taskset -c 0-16 ./run_stress_tool_pdisk_write.sh ...args...
```

#### Example
```bash
./run_stress_tool_pdisk_write.sh --tool ./ydb_stress_tool --duration 60 --label "pdisk write" \
  --disk /dev/nvme_01 --output result.json --run-count 3 --inflight-from 1 --inflight-to 32
```

### 2. run_stress_tool_ddisk_write.sh

Runs the YDB stress tool DDisk write load test and collects results.
Output JSON format is **compatible** with `plot.py` and `table.py` (same `InFlights` shape).

DDisk has no log mode; the script still emits a `LogMode` field for compatibility (it is set to `DDISK`).

#### Usage
```bash
./run_stress_tool_ddisk_write.sh --tool <ydb_stress_tool_path> \
  [--duration <seconds>] \
  [--warmup <seconds>] \
  [--label <label>] \
  [--run-count <N>] \
  [--inflight-from <N>] \
  [--inflight-to <N>] \
  [--areas-count <N>] \
  [--area-size <bytes>] \
  [--expected-chunk-size <bytes>] \
  [--node-id <N>] \
  [--pdisk-id <N>] \
  [--ddisk-slot-id <N>] \
  --disk <disk_path> \
  --output <output_file>
```

#### Arguments (high level)
- **`--areas-count`**: Number of `Areas` in config. Default: equals max `InFlight` (`--inflight-to`) (if set, forced for all inflights)
- **`--area-size`**: `AreaSize` in bytes (default: `134217728`)
- **`--expected-chunk-size`**: `ExpectedChunkSize` in bytes (default: `134217728`)
- **`--node-id` / `--pdisk-id` / `--ddisk-slot-id`**: DDiskId components (defaults: `1`)

#### Example
```bash
./run_stress_tool_ddisk_write.sh --tool ./ydb_stress_tool --label "ddisk write" \
  --disk /dev/nvme_01 --output ddisk_result.json --run-count 3 --inflight-from 1 --inflight-to 32
```

### 3. res_to_csv.sh

Converts the JSON results from `run_stress_tool_pdisk_write.sh` into CSV format.
For latency percentiles it uses **the last run** (`Runs[-1]`) for each inflight value.

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

### 4. plot.py

Plots:

- Throughput (`Speed`) vs `InFlight` (min/max whiskers, median dot)
- IOPS vs `InFlight` (min/max whiskers, median dot)
- Latency p50/p90/p95/p99 vs `InFlight` from the **median-IOPS run**

#### Usage
```bash
python3 plot.py <input_json> <output_prefix>
```

#### Example
```bash
python3 plot.py result.json /tmp/pdisk_write
# writes:
#   /tmp/pdisk_write_speed.png
#   /tmp/pdisk_write_iops.png
#   /tmp/pdisk_write_latency.png
```

### 5. table.py

Prints the same information as `plot.py`, but as **human-readable tables** to stdout:

- Throughput (`Speed`) vs `InFlight` (min/median/max)
- IOPS vs `InFlight` (min/median/max)
- Latency percentiles p50/p90/p95/p99 vs `InFlight` from the **median-IOPS run**

#### Usage
```bash
python3 table.py <input_json>
```

#### Example
```bash
python3 table.py result.json
```

## Complete Workflow Example

1. Run the stress test:
```bash
./run_stress_tool_pdisk_write.sh --tool ./ydb_stress_tool --duration 120 --label "Regular" \
  --disk /dev/disk/by-partlabel/kikimr_nvme_01 --output result.json \
  --run-count 3 --inflight-from 1 --inflight-to 32
```

2. Convert results to CSV:
```bash
./res_to_csv.sh --input result.json --percentile p99.00 --output results.csv
```

3. Plot results:
```bash
python3 plot.py result.json ./pdisk_write
```

4. Print tables:
```bash
python3 table.py result.json
```

## Output Format

### JSON Output (`run_stress_tool_pdisk_write.sh` / `run_stress_tool_ddisk_write.sh`)
The output is a JSON array (even if it contains a single group). Each group contains `InFlights` with summary stats and per-run results:
```json
[
  {
    "Label": "Regular",
    "LogMode": "LOG_NONE",
    "TestType": "PDiskWriteLoad",
    "InFlights": [
      {
        "InFlight": 1,
        "Speed": { "min": "202.3 MB/s", "median": "202.9 MB/s", "max": "205.3 MB/s" },
        "IOPS": { "min": "24889.5", "median": "24958.9", "max": "25258.2" },
        "Runs": [
          { "p99.00": "51 us", "...": "..." },
          { "p99.00": "44 us", "...": "..." }
        ]
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
