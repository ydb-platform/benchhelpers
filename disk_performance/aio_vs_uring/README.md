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
  --iodepth-from 1 \
  --iodepth-to 64 \
  --results-dir results
```

Include reads:

```bash
bash fio_latency_aio_uring.sh \
  --filename /dev/nvme0n1 \
  --reads \
  --iodepth-from 1 \
  --iodepth-to 64 \
  --results-dir results
```

## Aggregate results

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
