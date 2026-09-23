"""Validated fio JSON+ counters and measurement-window statistics.

Histogram keys are fio's bucket representatives, in nanoseconds. Percentiles and
window maxima derived from these buckets are approximate, not exact timings.
"""

import math
import statistics


PERCENTILES = (50, 90, 95, 99, 99.9)
TAIL_SAMPLE_ADVISORY = 100


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}: expected a number")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{label}: expected a finite nonnegative number")
    return value


def _counter(value, label):
    value = _number(value, label)
    if int(value) != value:
        raise ValueError(f"{label}: expected an integer counter")
    return int(value)


def _histogram(clat, label):
    bins = clat.get("bins", {})
    if not isinstance(bins, dict):
        raise ValueError(f"{label}: bins must be a mapping")
    result = {}
    for key, count in bins.items():
        try:
            bucket = int(key)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}: invalid histogram bucket {key!r}") from exc
        if bucket < 0 or bucket in result:
            raise ValueError(f"{label}: invalid or duplicate histogram bucket")
        result[bucket] = _counter(count, f"{label}.bins[{key}]")
    samples = _counter(clat.get("N", 0), f"{label}.N")
    coverage = sum(result.values())
    # fio copies live status statistics without stopping the worker. Histogram
    # counters and N can therefore differ slightly in intermediate snapshots.
    # Preserve that discrepancy below instead of treating a live snapshot as an
    # atomic capture. Missing or substantially incomplete histograms still fail.
    if (samples and not coverage) or abs(coverage - samples) > max(32, samples * 0.01):
        raise ValueError(f"{label}: histogram coverage does not match N; use fio JSON+")
    return result


def snapshot(payload):
    """Return the validated single fio job, without altering the payload."""
    if not isinstance(payload, dict):
        raise ValueError("fio payload must be an object")
    jobs = payload.get("jobs")
    if not isinstance(jobs, list) or len(jobs) != 1 or not isinstance(jobs[0], dict):
        raise ValueError("expected exactly one fio job")
    job = jobs[0]
    if job.get("error") != 0:
        raise ValueError(f"fio job error: {job.get('error', 'missing')}")
    for direction in ("read", "write"):
        stats = job.get(direction)
        if not isinstance(stats, dict):
            raise ValueError(f"missing fio {direction} statistics")
        for field in ("io_bytes", "total_ios", "runtime"):
            _counter(stats.get(field), f"{direction}.{field}")
        clat = stats.get("clat_ns", {})
        _histogram(clat, f"{direction}.clat_ns")
        for field in ("mean", "max"):
            _number(clat.get(field, 0), f"{direction}.clat_ns.{field}")
        lat = stats.get("lat_ns", {})
        _counter(lat.get("N", 0), f"{direction}.lat_ns.N")
        _number(lat.get("mean", 0), f"{direction}.lat_ns.mean")
    for field in ("job_runtime", "usr_cpu", "sys_cpu"):
        if field in job:
            _number(job[field], field)
    return job


def _delta(before, after, label):
    value = after - before
    if value < 0:
        raise ValueError(f"{label}: cumulative counter decreased")
    return value


def _mean_delta(before, after, label):
    count = _delta(before.get("N", 0), after.get("N", 0), f"{label}.N")
    total_after = after.get("N", 0) * after.get("mean", 0)
    total_before = before.get("N", 0) * before.get("mean", 0)
    total = total_after - total_before
    # Printed cumulative means are rounded; tolerate only cancellation noise.
    tolerance = max(1e-6, 1e-9 * max(total_before, total_after))
    if total < -tolerance:
        raise ValueError(f"{label}: cumulative latency sum decreased")
    return max(0, total) / count if count else None


def _percentiles(bins):
    total = sum(bins.values())
    result = {str(p): None for p in PERCENTILES}
    if not total:
        return result
    ordered = sorted(bins.items())
    for p in PERCENTILES:
        target = math.ceil(total * p / 100)
        running = 0
        for bucket, count in ordered:
            running += count
            if running >= target:
                result[str(p)] = bucket
                break
    return result


def _tail_quality(sample_counts):
    """Advisory tail coverage, not a statistical independence/confidence test."""
    active = any(sample_counts)
    pooled = sum(sample_counts)
    minimum = min(sample_counts)
    # Subtract percentages before division to avoid avoidable cancellation at
    # thresholds such as 100,000 samples for p99.9.
    factors = {str(p): round(100 - p, 10) / 100 for p in PERCENTILES}
    per_run = {p: minimum * fraction for p, fraction in factors.items()}
    pooled_tail = {p: pooled * fraction for p, fraction in factors.items()}
    return {
        "active": active,
        "advisory_min_tail_samples": TAIL_SAMPLE_ADVISORY,
        "minimum_run_tail_samples": per_run,
        "pooled_tail_samples": pooled_tail,
        "low_sample_percentiles": [p for p, count in per_run.items() if active and count < TAIL_SAMPLE_ADVISORY],
        "pooled_low_sample_percentiles": [p for p, count in pooled_tail.items() if active and count < TAIL_SAMPLE_ADVISORY],
    }


def window_metrics(before, after):
    """Subtract cumulative JSON+ jobs to describe only the selected window.

    ``before=None`` measures from the beginning of the fio job. Every call
    validates its input, preventing failed/partial jobs becoming valid results.
    """
    snapshot({"jobs": [after]})
    if before is not None:
        snapshot({"jobs": [before]})
    full_job = before is None
    before = before or {}
    result = {}
    for direction in ("read", "write"):
        old = before.get(direction, {})
        new = after[direction]
        count = _delta(old.get("total_ios", 0), new["total_ios"], f"{direction}.total_ios")
        nbytes = _delta(old.get("io_bytes", 0), new["io_bytes"], f"{direction}.io_bytes")
        runtime = _delta(old.get("runtime", 0), new["runtime"], f"{direction}.runtime")
        if (count or nbytes) and runtime <= 0:
            raise ValueError(f"{direction}: nonzero I/O without measurement time")
        old_clat = old.get("clat_ns", {})
        new_clat = new.get("clat_ns", {})
        old_bins = _histogram(old_clat, f"{direction}.clat_ns")
        new_bins = _histogram(new_clat, f"{direction}.clat_ns")
        bins = {}
        for bucket in old_bins.keys() | new_bins.keys():
            value = _delta(old_bins.get(bucket, 0), new_bins.get(bucket, 0), f"{direction}.bins[{bucket}]")
            if value:
                bins[bucket] = value
        samples = sum(bins.values())
        if count and not samples:
            raise ValueError(f"{direction}: completed I/O has no completion-latency samples")
        exact_maximum = full_job and "max" in new_clat
        result[direction] = {
            "iops": count * 1000 / runtime if runtime else 0,
            "bw_bytes": nbytes * 1000 / runtime if runtime else 0,
            "io_count": count,
            "io_bytes": nbytes,
            "runtime_ms": runtime,
            "sample_count": samples,
            "clat_sample_count": _delta(old_clat.get("N", 0), new_clat.get("N", 0), f"{direction}.clat_ns.N"),
            "histogram_sample_drift": samples - (new_clat.get("N", 0) - old_clat.get("N", 0)),
            "clat_mean_ns": _mean_delta(old_clat, new_clat, f"{direction}.clat_ns"),
            "clat_max_ns": (new_clat["max"] if exact_maximum else max(bins)) if bins else None,
            "clat_max_approximate": bool(bins) and not exact_maximum,
            "percentiles_ns": _percentiles(bins),
            "histogram_ns": {str(k): v for k, v in sorted(bins.items())},
            "lat_mean_ns": _mean_delta(old.get("lat_ns", {}), new.get("lat_ns", {}), f"{direction}.lat_ns"),
            "tail_quality": _tail_quality([samples]),
        }
    read, write = result["read"], result["write"]
    total_ios = read["io_count"] + write["io_count"]
    total_bytes = read["io_bytes"] + write["io_bytes"]
    old_runtime = before.get("job_runtime", max((before.get(d, {}).get("runtime", 0) for d in ("read", "write")), default=0))
    new_runtime = after.get("job_runtime", max(after[d]["runtime"] for d in ("read", "write")))
    runtime = _delta(old_runtime, new_runtime, "job_runtime")
    result.update({
        "total_iops": read["iops"] + write["iops"],
        "total_bw_bytes": read["bw_bytes"] + write["bw_bytes"],
        "achieved_read_pct": 100 * read["io_count"] / total_ios if total_ios else None,
        "achieved_read_byte_pct": 100 * read["io_bytes"] / total_bytes if total_bytes else None,
        "cpu_user": max(0, (after.get("usr_cpu", 0) * new_runtime - before.get("usr_cpu", 0) * old_runtime) / runtime) if runtime else None,
        "cpu_system": max(0, (after.get("sys_cpu", 0) * new_runtime - before.get("sys_cpu", 0) * old_runtime) / runtime) if runtime else None,
        "depth": dict(after.get("iodepth_level", {})),
        "depth_scope": "cumulative_job",
    })
    return result


def _repeat_statistics(values):
    """Summarize individual run estimates, retaining inactive None values."""
    values = [value for value in values if value is not None]
    for value in values:
        _number(value, "repeat statistic")
    if not values:
        return {key: None for key in ("min", "median", "max", "stdev")}
    return {"min": min(values), "median": statistics.median(values),
            "max": max(values), "stdev": statistics.stdev(values) if len(values) > 1 else 0}


def aggregate_runs(runs):
    """Summarize successful runs of one point without confusing two estimators.

    Rates and latency statistics are medians of per-run values. Percentiles of
    pooled histogram counts are *separate* from medians of per-run percentiles.
    Counts/runtime and histograms sum across runs; achieved ratios use those
    pooled counts. Consequently median rates need not equal pooled count/time,
    and median total IOPS need not equal the sum of direction medians. Standard
    deviations are sample SD (zero with one run), not uncertainty estimates.

    The caller excludes failed runs and retains their failure status separately.
    Advisory tail-count flags never claim statistical confidence or independence.
    """
    runs = list(runs)
    if not runs:
        raise ValueError("cannot aggregate an empty run list")
    summary = {"run_count": len(runs), "repeat_statistics": {}, "quality_flags": [],
               "ratio_scope": "pooled_io_counts", "count_scope": "sum_of_runs",
               "rate_scope": "median_of_runs", "percentile_scope": "median_of_run_percentiles",
               "stdev_scope": "sample_standard_deviation",
               "depth_scope": "median_of_run_cumulative_distributions"}
    if len(runs) == 1:
        summary["quality_flags"].append("single_repeat")
    for metric in ("total_iops", "total_bw_bytes", "cpu_user", "cpu_system"):
        stats = _repeat_statistics([run.get(metric) for run in runs])
        summary[metric] = stats["median"]
        if metric.startswith("total_"):
            summary["repeat_statistics"][metric] = stats
    depths = set().union(*(run.get("depth", {}).keys() for run in runs))
    summary["depth"] = {key: _repeat_statistics([run.get("depth", {}).get(key) for run in runs])["median"]
                        for key in sorted(depths)}
    for direction in ("read", "write"):
        sources = [run[direction] for run in runs]
        result, stats, bins = {}, {}, {}
        for source in sources:
            samples = _counter(source["sample_count"], f"{direction}.sample_count")
            hist = _histogram({"N": samples, "bins": source.get("histogram_ns", {})}, direction)
            if sum(hist.values()) != samples:
                raise ValueError(f"{direction}: run histogram does not match sample_count")
            for bucket, count in hist.items():
                bins[bucket] = bins.get(bucket, 0) + count
        for metric in ("iops", "bw_bytes", "clat_mean_ns", "clat_max_ns", "lat_mean_ns"):
            stats[metric] = _repeat_statistics([source.get(metric) for source in sources])
            result[metric] = stats[metric]["median"]
        for metric in ("io_count", "io_bytes", "runtime_ms", "sample_count", "clat_sample_count"):
            result[metric] = sum(_counter(source.get(metric, 0), f"{direction}.{metric}") for source in sources)
        result["histogram_sample_drift"] = sum(source.get("histogram_sample_drift", 0) for source in sources)
        result["histogram_ns"] = {str(bucket): count for bucket, count in sorted(bins.items())}
        result["pooled_percentiles_ns"] = _percentiles(bins)
        stats["percentiles_ns"] = {
            str(p): _repeat_statistics([source.get("percentiles_ns", {}).get(str(p)) for source in sources])
            for p in PERCENTILES}
        result["percentiles_ns"] = {p: values["median"] for p, values in stats["percentiles_ns"].items()}
        result["clat_max_approximate"] = any(source.get("clat_max_approximate", True)
                                              for source in sources if source.get("clat_max_ns") is not None)
        result["maximum_observed_clat_ns"] = stats["clat_max_ns"]["max"]
        result["tail_quality"] = _tail_quality([source["sample_count"] for source in sources])
        summary["quality_flags"].extend(f"{direction}:p{p}_low_tail_sample_count"
                                         for p in result["tail_quality"]["low_sample_percentiles"])
        summary[direction] = result
        summary["repeat_statistics"][direction] = stats
    io_count = sum(summary[direction]["io_count"] for direction in ("read", "write"))
    io_bytes = sum(summary[direction]["io_bytes"] for direction in ("read", "write"))
    summary["achieved_read_pct"] = 100 * summary["read"]["io_count"] / io_count if io_count else None
    summary["achieved_read_byte_pct"] = 100 * summary["read"]["io_bytes"] / io_bytes if io_bytes else None
    return summary


def check_stability(windows, read_pct):
    """Check range and regression change in the latest five one-minute windows.

    The caller owns the consecutive-pass requirement, bounded conditioning
    deadline, and ensuring windows have equal duration. This protocol has no
    write-volume floor. Both metrics in each active direction must converge;
    absent directions are not treated as zero-valued samples.
    """
    if not isinstance(read_pct, (int, float)) or not 0 <= read_pct <= 100:
        raise ValueError("read_pct must be between 0 and 100")
    if len(windows) < 5:
        return {"stable": False, "checks": {}, "reason": "need five complete windows"}
    checks = {}
    for direction in ("read", "write"):
        if (direction == "read" and read_pct == 0) or (direction == "write" and read_pct == 100):
            continue
        for metric in ("bw_bytes", "clat_mean_ns"):
            values = [window.get(direction, {}).get(metric) for window in windows[-5:]]
            valid = all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in values)
            valid = valid and all(window.get(direction, {}).get("io_count", 0) > 0 for window in windows[-5:])
            if not valid:
                checks[f"{direction}.{metric}"] = {"stable": False, "reason": "missing or zero active samples"}
                continue
            mean = sum(values) / 5
            range_ratio = (max(values) - min(values)) / mean
            # x=0..4, mean(x)=2, sum((x-2)^2)=10.
            slope = sum((i - 2) * value for i, value in enumerate(values)) / 10
            change_ratio = abs(slope * 4) / mean
            checks[f"{direction}.{metric}"] = {
                "stable": range_ratio <= 0.2 + 1e-12 and change_ratio <= 0.1 + 1e-12,
                "mean": mean,
                "range_ratio": range_ratio,
                "slope_change_ratio": change_ratio,
            }
    return {"stable": all(check["stable"] for check in checks.values()), "checks": checks}
