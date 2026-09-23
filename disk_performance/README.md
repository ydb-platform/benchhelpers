# NVMe performance measurements

Run `fio_device.sh` with a device to get practical NVMe characterization across **fresh, medium, and sustained** profiles. Each profile is prepared once, then measured with repeated, time-bounded fio workloads. Results include per-profile read/write heatmaps, raw repetitions, medians, variation, and device-state drift references.

## Run

**All data on the selected target is discarded and overwritten.** Store results outside the tested NVMe.

```bash
sudo ./fio_device.sh --filename /dev/nvmeXn1
```

The script displays identity, configuration, workload counts, preparation budgets, and an estimated time schedule before asking you to type the full target path. `--yes` skips that confirmation. A new timestamped results directory is created by default; `--results-dir` selects another location and must be new or empty.

Partitions require explicit opt-in:

```bash
sudo ./fio_device.sh --filename /dev/nvmeXn1p2 --allow-partition \
  --results-dir /path/on/other/storage/nvme-results
```

An NVMe namespace (`nvmeXn1`) is the block device exposed by the SSD. A partition (`nvmeXn1p2`) selects a range within it. Writes/discards remain inside the selected partition, but caches, flash management, spare capacity, and temperature are shared with the rest of the device. Occupancy means a percentage of the **test partition**, and allocation elsewhere is uncontrolled. The report records partition/namespace sizes and their ratio. The namespace tree, including sibling partitions, must be free of mounts, swap, holders, and open device users.

```bash
# Read-only checks and preview; no target or result writes.
sudo ./fio_device.sh --filename /dev/nvmeXn1p2 --allow-partition --dry-run

# Used-state characterization only.
sudo ./fio_device.sh --filename /dev/nvmeXn1p2 --allow-partition --profiles medium

# Denser optional matrix, with a custom repeat count and measured duration.
sudo ./fio_device.sh --filename /dev/nvmeXn1 --matrix full \
  --run-count 3 --ramp-time 3s --measurement-time 10s
```

Ctrl-C stops active work and preserves partial results; it cannot restore discarded data and does not automatically resume the run. No destructive hardware test is part of automated tests.

## Preparation and interpretation

The default occupied and measured ranges are **80% of the target**. `--occupancy-percent` controls the initialized occupancy for medium and sustained; `--size-percent` controls the measured active range and defaults to occupancy. For medium/sustained the measured range cannot exceed initialized occupancy. Ranges round down to whole MiB.

| Profile | One-time preparation | Default preparation cap |
|---|---|---|
| `fresh` | Discard, then initialize the measured range once, with no additional aging. | 30 minutes including discard/fill. |
| `medium` | Discard, initialize 80% once, lightly overwrite randomly for at most 120 seconds **and at most 5% of occupied bytes**, then recover 60 seconds. | 30 minutes including discard/fill/overwrite/recovery. |
| `sustained` | Discard, initialize 80% once, then apply bounded high-QD 4 KiB random-write conditioning. | 60 minutes **including discard and fill**. |

Measurements do not rebuild preparation between cases or repeats. Device state evolves during a profile; early/late 4 KiB QD1 read references quantify some of that drift separately from matrix samples. Fresh means the initial profile baseline, not that every subsequent write runs on a newly discarded device.

Sustained preparation records whether its conditioning workload appeared stable or reached its time cap. Stability checks use five approximately one-minute windows of write throughput and mean completion latency: range/mean at most 20%, fitted change/mean at most 10%, and two consecutive passing assessments. This can describe a temporary plateau; it does not prove cache exhaustion or long-term steady state. That status is separate from the success of subsequent measurements. There is **no full-capacity write floor for each low-QD test**, and no unlimited conditioning loop. This is practical characterization, **not steady-state certification for every workload or SNIA compliance**. SNIA's distinction between initial and workload-dependent conditioning is useful background, but its standard is a different protocol: [SNIA SSS PTS 2.0.2](https://www.snia.org/sites/default/files/2025-02/SNIA-SSS-PTS-2.0.2.pdf).

If initialization cannot finish within the preparation cap, the run stops before measurements. After a complete fill, capped conditioning or shortened medium recovery can still produce measurements, but those cells carry `*` qualifications and the preparation table explains the incomplete steps. `not_requested` describes fresh preparation or explicitly disabled medium conditioning; skipped conditioning due to insufficient time is recorded as capped.

Discard is the reset mechanism. The script does not format namespaces or change firmware, write cache, IRQ placement, or power settings. Uninitialized space is discarded, so occupancy also affects capacity available to the controller for reclamation. Data elsewhere on a partitioned device can affect results.

## Matrix, repetitions, and runtime

| Preset | Sequential cases | Random cases | Cases/profile |
|---|---|---|---:|
| `balanced` (default) | 128 KiB/1 MiB; read/write; QD1/64 | 4 KiB/8 KiB; read% 100,90,70,50,0; QD1,4,16,64 | 48 |
| `full` | Same eight sequential cases | 4 KiB/8 KiB; read% 100,95,90,80,70,50,30,10,0; QD1,4,16,32,64 | 98 |

Default: **five repeats/case, three-second excluded warmup, ten measured seconds/repeat**. Balanced therefore schedules 240 samples/profile and 720 samples across three profiles. Each profile's sample schedule is `48 × 5 × (3 + 10) = 3,120 seconds`, or **52 minutes**, before process overhead, references, and preparation. Default preparation caps add at most two hours across the three profiles; the printed whole-run budget also allows process overhead. Full matrix schedules approximately 106 minutes/profile before those additions.

`--max-run-time` sets a whole-device-work deadline. When omitted, a finite cap is derived from preparation caps, sample/reference durations, and process-overhead allowances. It is a bound on device work, not a promise that optional report rendering finishes at exactly that deadline. The estimate and budget remain in the manifest/report. A cap or interrupted run can yield partial results; completed repetitions remain visible with their reduced sample count.

With the defaults, the schedule including references and a two-second/sample overhead allowance is **60.5 minutes/profile**; the automatically calculated device-work cap is **5 hours 6 minutes 30 seconds** for all three profiles. Preparation can finish early. This cap is not an ETA or a guarantee under kernel faults: the runner bounds its waits and sends termination signals, but uninterruptible kernel I/O may persist and is reported as an error.

| Preset with default timings | Samples/profile | Measurement schedule/profile, including references and overhead | All three profiles: device-work cap |
|---|---:|---:|---:|
| `balanced` | 240 | 60 min 30 sec | **5 h 6 min 30 sec** |
| `full` | 490 | 123 min | **8 h 14 min** |
| `--run-type smoke` | 20 | 1 min 28 sec | **12 min 24 sec** |

These totals include all preparation caps plus five minutes of shared allowance. Running only fresh or medium has a default balanced cap of **1 h 35 min 30 sec**; sustained alone has **2 h 5 min 30 sec**. Four saturation probes add one minute per selected profile with default timings. Custom durations, ratios and repeat counts recalculate the cap and can extend it beyond these hours; setting a shorter `--max-run-time` stops the run rather than compressing the matrix.

Preparation feasibility still depends on capacity and write speed: initializing 80% of a 4 TB target within 30 minutes requires at least 1.78 GB/s average, with additional time needed for discard and medium overwrite/recovery. A larger or slower SSD may need higher preparation caps or a smaller initialized/measured range. The preview records the bytes to initialize, and fill progress records the estimated remaining time.

Mixed cases use one fio `randrw` job with `rwmixread`. Ratios describe read/write **operation percentages**; both directions share the total QD and use equal block sizes. Achieved operation/byte ratios are reported separately. Pure endpoints are reused as heatmap rows without duplicate cases.

The ratio × QD layout follows [`../stress_tool/`](../stress_tool/) at commit `b517443f9bceecbb65e3aa4bc63dc62e3af4d342`. That tool's background ratio means writes per read: for equal-size operations, read percentage is `100 / (1 + ratio)`; ratio 0.25 means 80/20, while ratio 1 means 50/50. Its scheduler differs from fio's mixed-job scheduler, so matching axes do not imply identical workloads.

Defaults use one direct-I/O worker, ordinary `io_uring`, explicit seeds/payload policy, independently sampled random offsets, and single-I/O submission batching. CPU placement, achieved depth, and CPU use are recorded. One worker may not saturate every NVMe. `--saturation-probe` adds four single-sample random QD256 diagnostics per profile (4/8 KiB read/write); these are distinct from the main matrix.

## Controls and dependencies

| Option | Default / meaning |
|---|---|
| `--profiles` | `fresh,medium,sustained`; unique subset. Old names `used-recovered` and `sustained-steady` are aliases. |
| `--matrix` | `balanced`; `full` provides the dense optional grid. |
| `--read-percentages` | Override preset random ratios with unique comma-separated integers 0–100. |
| `--run-count` | `5` planned repetitions per case; the first failure stops the run and preserves successful repetitions. |
| `--ramp-time` | `3s`, excluded warmup; zero allowed. |
| `--measurement-time`, `--runtime` | `10s`, measured time per repetition. |
| `--occupancy-percent` | `80`, medium/sustained occupancy. |
| `--size-percent` | Occupancy percentage by default; measured range. |
| `--medium-conditioning-time` | `120s`, also capped at 5% of occupied bytes; zero allowed. |
| `--recovery-time` | `60s`, medium recovery; zero allowed. |
| `--max-preparation-time` | `30m`, all fresh/medium preparation. |
| `--max-conditioning-time` | `60m`, all sustained preparation, including fill. |
| `--max-run-time` | Finite automatically calculated whole-run device-work cap. |
| `--saturation-probe` | Add four QD256 diagnostics/profile. |
| `--use-aio` | Use `libaio` instead of `io_uring`. |
| `--polling` | Specialized io_uring polling with registered files and separate physical SQ-poll CPU; requires support and conflicts with `--use-aio`. |
| `--prefix` | `nvme`, report title label. |
| `--run-type smoke` | Reduced matrix/range/durations for destructive workflow checks, not performance evidence. **Still discards the entire target.** |

Durations accept seconds or suffixes `s`, `m`, `h`, `d`. `--clean-device` remains a deprecated alias for fresh-only. Standalone probe/random-pass options from the old scripts are not supported. See `--help` for the full interface and [fio's official manual](https://fio.readthedocs.io/en/latest/fio_doc.html) for engine and mixed-I/O semantics.

Required: Linux, Python 3, fio with the chosen engine, plus `blkdiscard`, `lsblk`, `blockdev`, `findmnt`, `swapon`, and `fuser`. `nvme-cli` enables identification and SMART/temperature/health monitoring; missing telemetry is explicitly recorded. The script does not install packages automatically.

**Matplotlib is optional.** HTML heatmap tables, text and CSV work without it; installing it adds embedded PNG heatmaps/QD/time-series plots:

```bash
python3 -m pip install -r requirements.txt
```

`sudo` may use another Python environment. An isolated alternative is `python3 -m pip install --target /absolute/dependency/directory -r requirements.txt`, followed by `sudo env PYTHONPATH=/absolute/dependency/directory ./fio_device.sh ...`. There is no hardcoded machine-specific dependency directory.

## Output and statistics

| Artifact | Contents |
|---|---|
| `report.html` | Offline profile tables/heatmaps, repeat spread/sample qualifications, preparation progress/status, budget, read-reference drift, device/telemetry, optional embedded plots. |
| `result.txt` | Terminal-friendly matrices and status summaries. |
| `results.csv` | One directional summary row per case: median metrics, repeat min/median/max/sample SD, counts, achieved ratios, quality/approximation flags. |
| `raw-runs.csv` | Separate rows for individual repetitions, including failures, run index, seed, elapsed time, counts, and raw-run metrics. |
| `manifest.json` | Versioned schema, all point/run records, preparation, budgets, reference measurements, device and telemetry. |
| `<profile>/<point>/<run>/` | Exact command, fio JSON+ output/stderr, final job result, and one-second throughput logs. Latency histograms are embedded in JSON+; separate histogram log files are not produced. |
| `preparation/` | One-time preparation records and raw commands/results. |

Heatmaps show **median read/write kIOPS and median per-run read/write p99 completion latency**. A median of run-level p99 values is not a pooled p99. Pooled percentiles from summed histogram counts are separate CSV/raw fields. Rate/latency medians may form a synthetic summary rather than a single representative run; counts sum successful runs, and achieved ratios use pooled counts. Spread uses sample standard deviation (one successful run has SD zero, not evidence of repeatability).

Corresponding heatmaps share scales across profiles. `N/A` means an inactive direction. Results based on successful but incomplete repeats remain numeric with `*` qualifications; absent results stay gray. Advisory tail-sample flags identify sparse percentiles without claiming statistical confidence. Maximum-latency approximations are labeled; median run maximum and maximum observed across runs are distinct fields. CSV units are bytes/s and nanoseconds; displayed units are explicit. Completion latency does not establish application durability or transaction latency.

The manifest updates after every repetition; HTML/CSV/text reports update during preparation, after each case, and at run termination. This keeps report rendering out of active measurements and limits its overhead. No fixed ranking between profiles is assumed: inspect drift, temperature, CPU/depth, achieved ratios, and repeat spread. Fio/device failures and reported critical health changes stop device work and preserve completed samples. A final SMART check runs before declaring completion when telemetry is available. Read-only engine capability failures and run-stop reasons are retained in the manifest and reports. Host writes use target kernel counters when available; missing counters make the reported total a labeled lower bound that may omit warmup writes. Early/late reference writes are included in accounting.

Optional time-series plots show one representative successful repeat per point, selected by total IOPS nearest the point median and labeled explicitly. All repeats remain in raw files. Long logs use bounded min/mean/max downsampling with spike retention; raw logs stay unchanged. Histogram payloads remain in JSON/manifest files rather than the HTML to keep report refreshes small. Optional plotting errors leave HTML tables, CSV, and text usable. CSV also preserves p90 and the scope of the achieved-depth distribution; fio's depth counters can include cumulative job activity rather than only the selected measurement interval.

```bash
python3 ./aggregate.py /path/to/results
```

The aggregator accepts current schema 2 and historical schema 1 manifests. Schema 1 keeps its historical measurement semantics. Directories without a manifest retain the old aggregation path, including legacy `--plot` behavior.

The namespace-wide advisory lock is held across preparation, idle recovery, measurements and reporting, preventing another instance of this runner from testing a sibling partition concurrently. It cannot stop unrelated programs from opening the device after validation; keep the device otherwise idle.

## Standalone preparation and development

```bash
sudo ./precondition.sh --filename /dev/nvmeXn1p2 --allow-partition \
  --profiles medium --results-dir /path/on/other/storage/preparation
```

Standalone preparation requires exactly one profile, performs only its bounded baseline preparation, and writes its records. It does not run the measurement matrix or certify steady state for arbitrary later workloads.

Automated tests use fixtures/mocked commands and require no real NVMe:

```bash
python3 -m unittest discover -p 'test_profile*.py' -v
python3 -m unittest discover -p 'test_nvme*.py' -v
```

Tests cover both matrices and all profiles with simulated slow fill/nonconverging conditioning, timeout/interruption paths, preparation qualifications, safety checks and partial reports. When fio is installed, diskless `null` engine tests exercise real subprocess handling and excluded warmup counters. These checks validate orchestration, not hardware performance. Existing schema 1 hardware results do not validate the current schema 2 workflow.

See [aio_vs_uring/README.md](aio_vs_uring/README.md) for separate aio-versus-io_uring experiments.
