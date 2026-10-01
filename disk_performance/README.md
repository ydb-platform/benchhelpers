# Bounded NVMe profile benchmark

`nvme_bench.py` measures a raw NVMe namespace or partition after explicit preparation states. The default `comparison` protocol gives a compact, paired profile comparison. The balanced/full `sweep` matrix describes evolving device state. **Every preparation discards and overwrites the selected target.** A partition run changes only that partition, but namespace state elsewhere is uncontrolled.

The runner checks mounts, holders, users, swap, target identity, output location, NUMA placement, physical cores, and fio support before destructive work. It holds a namespace lock, monitors SMART when `nvme-cli` exists, and preserves a partial manifest on failure. Results must go in a new or empty directory outside the target namespace. Root privileges and `--allow-partition` are required when applicable. Python plot dependencies are listed in `requirements.txt`; the runner also needs `fio`, `blkdiscard`, `lsblk`, `blockdev`, `fuser`, `swapon`, and `findmnt`.

## Preview and execution

```bash
sudo python3 nvme_bench.py --filename /dev/nvme3n1p2 --allow-partition \
  --protocol comparison --numa-node 1 --workers 4 --trials 3 \
  --results-dir /path/on/other/storage/results --dry-run

sudo python3 nvme_bench.py --filename /dev/nvme3n1p2 --allow-partition \
  --protocol comparison --numa-node 1 --workers 4 --trials 3 \
  --results-dir /path/on/other/storage/results --yes
```

The read-only dry run prints the complete ordered schedule, per-worker ranges, depths, CPU assignments, seeds, minimum write exposure, and finite caps. `--yes` suppresses the target-path confirmation. `--max-run-time` sets a stricter whole-run device-work deadline. Capped preparations with full initialization can yield exploratory measurements, excluded from the qualified comparison. Incomplete initialization stops their observations.

`--numa-node N` defaults to node 0. The runner intersects that node's online CPUs with permitted affinity, binds its own affinity to the effective pool before children, passes `numa_mem_policy=bind:N` to fio, and pins each fio worker to a distinct physical core. The manifest records the pool, chosen cores, memory policy, and controller NUMA separately; controller location does not override the selection. Sustained conditioning always requires four physical cores, even with `--workers 1` or `2`. fio's memory-policy support is checked before discard.

## Default comparison

Defaults are `--protocol comparison --trials 3 --run-count 1 --workers 4 --seed 20260909`. Each profile is discarded and initialized independently in every trial. Trial order rotates F/M/S, M/S/F, S/F/M (fresh, medium, sustained). Further trials repeat the rotation. Each preparation has seven ordered primary anchors and two final read references. Every sample has a five-second excluded ramp and 20 measured seconds.

| Order | Workload | Aggregate QD | Workers |
| --- | --- | ---: | ---: |
| 1 | 1 MiB sequential read | 64 | 1 |
| 2 | 4 KiB random read | 1 | 1 |
| 3 | 4 KiB random read | 64 | 4 |
| 4 | 8 KiB random read | 64 | 4 |
| 5 | 4 KiB random 70% read / 30% write | 32 | 4 |
| 6 | 4 KiB random write | 64 | 4 |
| 7 | 4 KiB random write | 1 | 1 |
| Final | Repeat 4 KiB random read anchors | 1 / 64 | 1 / 4 |

The default schedule has nine preparations, 63 primary samples, and 18 final references. Its nominal whole-run device-work cap is 6 hours 41 minutes 27 seconds: preparation caps, scheduled sample time, two seconds of overhead per sample, and five minutes of general overhead. This is a cap, not an ETA. Initialization and required medium/sustained exposure total about 31.1 TB of host writes on a 3.2 TB target at 80% occupancy, before stability and measurement writes. `--size-percent` may reduce the measurement range but never initialized occupancy; the measurement range cannot exceed occupancy. Sequential measurements are bounded streaming windows, not whole-range scans. The write-QD1 anchor follows write-QD64 and precedes the final read references, so it measures that ordered, write-evolved device state. New comparison manifests record `comparison_protocol_revision: 2`; older schema-3 comparison manifests remain readable.

`--workers` chooses 1, 2, or 4 for random measurements. Actual worker count is `min(requested workers, aggregate QD)`; sequential and QD1 use one. Aggregate QD is split across workers and equal aligned shards cover the eligible range without overlap. Layout stays fixed across profiles/trials. Seeds derive deterministically from trial, workload, worker, and repeat and match across profiles. Per-worker achieved-depth buckets are distinct from requested aggregate QD. Comparison uses ordinary direct `io_uring`; specialized `libaio` and polling options remain sweep-only.

## Preparation and qualification

Every preparation discards the selected target and writes identical occupancy, 80% by default, using one sequential 1 MiB stream at QD32. Full initialization is mandatory. Fresh adds no aging; its cap is 30 minutes including discard/fill. Medium targets random writes of 5% of occupied bytes within 120 seconds, then idles 60 seconds. Its 30-minute cap includes discard/fill, exposure, and recovery. Missing exposure or recovery leaves it unqualified.

Sustained uses four random-write workers at QD32 each (aggregate 128), with one disjoint equal occupied-range shard per worker. Each must write at least its shard size. Only after all four floors are reached does the runner collect approximately 60-second windows. The latest five windows must have throughput and mean completion-latency range/mean at most 20%, fitted change/mean at most 10%, and every worker below 90% CPU in every window. Two consecutive assessments must pass. Missing CPU data cannot qualify. The 60-minute cap includes discard/fill. Write volume does not imply unique block coverage. The stability result describes this conditioning workload only, not universal SSD or SNIA steady state.

Mixed and pure writes intentionally follow reads, so later anchors observe earlier writes. The manifest records preceding writes, including ramps where target kernel counters permit. The final references show local read drift; they cannot establish equivalence or stability throughout the sequence.

## Results

`manifest.json` uses schema 3 with unique trial, preparation, observation, execution, and reference identities and explicit observation-to-preparation links. Each execution retains its fio command, stdout/stderr, and final validated job data. `result.txt`, `report.html`, CSVs, and optional plots expose preparation qualification, exposure, cap headroom, run order, prior writes, CPU, and drift, including partial and exploratory records. `aggregate.py RESULTS_DIR` rerenders schema 3 and remains compatible with schema 1/2 manifests and legacy no-manifest directories.

The comparison report places 4 KiB random-read QD1 completion latency near the top, separating primary anchors from final references, and adds a separate write-QD1 table for the seventh anchor. Mean and p50/p90/p95/p99/p99.9 show median and range across qualified independent trials in microseconds; worst observed run maximum is labeled exact or approximate. Older result manifests without write-QD1 measurements explicitly show it as not measured; write-QD64 latency cannot supply that value. A collapsible table and `result.txt` retain directional latency for every observation. `results.csv` and `raw-runs.csv` include both directions' completion-latency percentiles, means, maxima, total-latency means, and approximation metadata. Run percentiles and pooled histogram percentiles have separate columns. The report's workload key decodes the anchors; mixed `rw4k_qd32` requests 70% reads and 30% writes. Each run's total IOPS sums both directions, while medians across repeats need not add.

Every expected fio job, counter, and histogram is validated. Worker counts and histograms are summed; rates use a shared measured interval; latency means are weighted by samples. Worker time series record their one-second averaging configuration and post-ramp origin; plots sum only intervals covered by every worker before downsampling and label incomplete log coverage. Older logs without interval metadata retain point-timestamp plots. Run percentiles, medians of run percentiles, and pooled histogram percentiles are separate. The report first summarizes local repeats, then the median and range of independent qualified trials and paired profile ratios by matching trial IDs. Preparations are counted separately from repeats. Overlapping ranges and local time windows are not independent evidence. Exit code zero alone does not imply enough qualified trials or acceptance.

`--run-type smoke` reduces duration/range to exercise workflow and worker layouts; it is not a performance comparison. Historical hardware result directories in this repository came from a different target or method and are context only.

## Evolving-state sweep and compatibility

```bash
sudo python3 nvme_bench.py --filename /dev/nvme3n1p2 --allow-partition \
  --protocol sweep --matrix balanced --numa-node 1 \
  --results-dir /path/on/other/storage/sweep --dry-run
```

`--matrix balanced` and `--matrix full` retain the broad matrix. `--read-percentages`, `--saturation-probe`, `--use-aio`, and `--polling` are sweep-only. Sweep defaults to one preparation/profile, five local samples/case, three seconds ramp, and ten measured seconds. Explicit `--matrix` without `--protocol` selects sweep with a migration notice. The older profile aliases `used-recovered` and `sustained-steady`, `--clean-device`, `--runtime`, `--prepare-only`, `fio_device.sh`, and `precondition.sh` remain available. Sweep heatmaps describe ordered evolving state, not independent starting states.
