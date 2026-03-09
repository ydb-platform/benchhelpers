# aio_uring fio latency benchmark

A script for comparing `libaio` and `io_uring` modes using the [fio](https://github.com/axboe/fio) tool. The script runs fio
commands while varying iodepth and using different I/O engines. Base fio command example:
```
sudo fio --name=write_latency_test --filename=<DEVICE> --filesize=2500G  \
  --time_based --ramp_time=10s --runtime=1m                              \
  --rw=randwrite                                                         \
  --clocksource=cpu                                                      \
  --direct=1 --verify=0 --randrepeat=0 --randseed=17                     \
  --iodepth=16  --iodepth_batch_submit=1 --iodepth_batch_complete_max=1  \
  --bs=4K                                                                \
  --lat_percentiles=1 --percentile_list=10:50:90:95:99:99.9              \
  --output-format=json --output=<DIR/FNAME.json>                         \
  --ioengine=<ENGINE> <ENGINE ARGS>
```

The goal is to compare I/O modes, so before each run we "refresh" the target: by default this is `blkdiscard` for block devices (or optional fill-disk preconditioning).
By default, each `iodepth + engine + engine_args` combination is run 10 times, and we report the median-throughput run. Besides refresh/preconditioning, runs are randomized (fixed seed) to reduce ordering effects. Also, after each run there is a cooldown (default: 10s), and every hour there is a 5m cooldown to avoid heating the device.

By default, each run starts with a 10s ramp and then a 1m test. This is usually enough to compare engines while avoiding NVMe "spike down" behavior due to internal GC, etc.



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

## Aggregate results

Each test point (`engine + iodepth + workload`) is usually measured multiple times via
`--run-count` (default: `10`), with files like:

- `aio_qd1_write_run1.json`
- `aio_qd1_write_run2.json`
- ...

`aggregate.py` behavior:

- Table/CSV output picks the **single median run** per test point (median by `Speed_Bps`).
- Output includes `MedianRun` and `RunsInGroup` columns so it is clear which run was selected.
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
- `results/speed_inflight_read_latency_bars_qd_1_4_16.png` (only if read-result JSON files are present)
