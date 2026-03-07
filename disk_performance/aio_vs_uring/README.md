# aio_uring fio latency benchmark

Quick helper scripts for comparing `libaio` and `io_uring` modes with fio latency runs.

## Files

- `fio_latency_aio_uring.sh` - runs fio for selected engines and queue depths, stores per-run JSON files.
- `aggregate.py` - aggregates JSON results into table/CSV and can generate speed/latency-vs-inflight plots.

## Quick start

Run benchmark (write only):

```bash
bash fio_latency_aio_uring.sh \
  --filename /dev/nvme0n1 \
  --run-count 10 \
  --iodepth-from 1 \
  --iodepth-to 64 \
  --results-dir results
```

Include reads:

```bash
bash fio_latency_aio_uring.sh \
  --filename /dev/nvme0n1 \
  --reads \
  --run-count 10 \
  --iodepth-from 1 \
  --iodepth-to 64 \
  --results-dir results
```

## Aggregate results

Each test point (`engine + iodepth + workload`) is usually measured multiple times via
`--run-count` (default: `10`), with files like:

- `aio_qd1_write_run1.json`
- `aio_qd1_write_run2.json`
- ...

`aggregate.py` behavior:

- Table/CSV output picks the **single median run** per test point (median by `Speed_Bps`).
- Output includes `Run` and `RunsInGroup` columns so it is clear which run was selected.
- Plots show **median points** with **min/max whiskers** across all runs for each point.

Table output:

```bash
python3 aggregate.py results --format table
```

CSV output:

```bash
python3 aggregate.py results --format csv
```

Plots (all modes on one chart):

```bash
python3 aggregate.py results --plot --prefix speed_inflight
```

This creates:
- `results/speed_inflight.png`
- `results/speed_inflight_latency.png`
- `results/speed_inflight_latency_upto_32.png`
- `results/speed_inflight_latency_upto_16.png`
- `results/speed_inflight_write_latency_bars_qd_1_4_16.png`
- `results/speed_inflight_read_latency_bars_qd_1_4_16.png` (when `--reads` is used)
