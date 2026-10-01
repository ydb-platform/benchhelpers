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


def snapshot(payload, expected_names=None):
    """Validate JSON+ jobs and return one legacy job or a named job mapping.

    Explicit names require an exact match. In particular a fio group-reporting
    row cannot silently stand in for several independently measured workers.
    """
    if not isinstance(payload, dict):
        raise ValueError("fio payload must be an object")
    jobs = payload.get("jobs")
    if expected_names is None:
        if not isinstance(jobs, list) or len(jobs) != 1:
            raise ValueError("expected exactly one fio job")
        return _validate_job(jobs[0])
    if isinstance(expected_names, (str, bytes)):
        raise ValueError("expected_names must be a sequence of unique job names")
    names = list(expected_names)
    if not names or any(not isinstance(name, str) or not name for name in names) or len(names) != len(set(names)):
        raise ValueError("expected_names must be a sequence of unique job names")
    if not isinstance(jobs, list) or len(jobs) != len(names):
        raise ValueError(f"expected fio jobs {names!r}, received {len(jobs) if isinstance(jobs, list) else 'invalid jobs'}")
    result = {}
    for job in jobs:
        validated = _validate_job(job)
        name = validated.get("jobname")
        if not isinstance(name, str) or not name or name in result:
            raise ValueError(f"missing or duplicate fio job identity: {name!r}")
        result[name] = validated
    if set(result) != set(names):
        raise ValueError(f"fio job identities differ: expected {sorted(names)!r}, got {sorted(result)!r}")
    return {name: result[name] for name in names}


def _validate_job(job):
    if not isinstance(job, dict):
        raise ValueError("fio job must be an object")
    if job.get("error") != 0:
        raise ValueError(f"fio job error: {job.get('error', 'missing')}")
    for direction in ("read", "write"):
        stats = job.get(direction)
        if not isinstance(stats, dict):
            raise ValueError(f"missing fio {direction} statistics")
        for field in ("io_bytes", "total_ios", "runtime"):
            _counter(stats.get(field), f"{direction}.{field}")
        clat = stats.get("clat_ns", {})
        if not isinstance(clat, dict):
            raise ValueError(f"{direction}.clat_ns must be an object")
        _histogram(clat, f"{direction}.clat_ns")
        for field in ("mean", "max"):
            _number(clat.get(field, 0), f"{direction}.clat_ns.{field}")
        lat = stats.get("lat_ns", {})
        if not isinstance(lat, dict):
            raise ValueError(f"{direction}.lat_ns must be an object")
        _counter(lat.get("N", 0), f"{direction}.lat_ns.N")
        _number(lat.get("mean", 0), f"{direction}.lat_ns.mean")
    for field in ("job_runtime", "usr_cpu", "sys_cpu"):
        if field in job:
            _number(job[field], field)
    depth = job.get("iodepth_level", {})
    if not isinstance(depth, dict):
        raise ValueError("iodepth_level must be an object")
    for bucket, value in depth.items():
        _number(value, f"iodepth_level[{bucket}]")
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


def window_metrics(before, after, expected_names=None, interval_ms=None):
    """Subtract cumulative JSON+ jobs to describe only the selected window.

    ``before=None`` measures from the beginning of the fio job. Every call
    validates its input, preventing failed/partial jobs becoming valid results.
    """
    if expected_names is not None:
        return _worker_window_metrics(before, after, expected_names, interval_ms)
    if interval_ms is not None:
        raise ValueError("interval_ms requires expected_names")
    return _single_window_metrics(before, after)


def _single_window_metrics(before, after):
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
            "lat_sample_count": _delta(old.get("lat_ns", {}).get("N", 0),
                                       new.get("lat_ns", {}).get("N", 0), f"{direction}.lat_ns.N"),
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
        "cpu_user": (max(0, (after["usr_cpu"] * new_runtime - before.get("usr_cpu", 0) * old_runtime) / runtime)
                     if runtime and "usr_cpu" in after and (full_job or "usr_cpu" in before) else None),
        "cpu_system": (max(0, (after["sys_cpu"] * new_runtime - before.get("sys_cpu", 0) * old_runtime) / runtime)
                       if runtime and "sys_cpu" in after and (full_job or "sys_cpu" in before) else None),
        "depth": dict(after.get("iodepth_level", {})),
        "depth_scope": "cumulative_job",
    })
    return result


def _worker_window_metrics(before, after, expected_names, interval_ms):
    names = list(expected_names)
    if not isinstance(after, dict):
        raise ValueError("after must map fio job names to jobs")
    if set(after) != set(names):
        raise ValueError("after worker keys differ from expected fio jobs")
    current = snapshot({"jobs": list(after.values())}, names)
    if before is None:
        previous = None
    else:
        if not isinstance(before, dict):
            raise ValueError("before must map fio job names to jobs")
        if set(before) != set(names):
            raise ValueError("before worker keys differ from expected fio jobs")
        previous = snapshot({"jobs": list(before.values())}, names)
    workers = {name: _single_window_metrics(previous[name] if previous else None, current[name])
               for name in names}
    if interval_ms is None:
        interval_ms = max(
            _delta((previous[name].get("job_runtime", 0) if previous else 0),
                   current[name].get("job_runtime", max(current[name][d]["runtime"] for d in ("read", "write"))),
                   f"{name}.job_runtime") for name in names)
    interval_ms = _counter(interval_ms, "shared interval_ms")
    total_count = sum(workers[name][d]["io_count"] for name in names for d in ("read", "write"))
    if total_count and not interval_ms:
        raise ValueError("nonzero I/O without shared measurement time")
    result = {"workers": workers, "worker_names": names, "interval_ms": interval_ms,
              "depth": {}, "depth_scope": "per_worker_cumulative_job",
              "cpu_user": sum(workers[name]["cpu_user"] or 0 for name in names),
              "cpu_system": sum(workers[name]["cpu_system"] or 0 for name in names)}
    for direction in ("read", "write"):
        sources = [workers[name][direction] for name in names]
        bins = {}
        for source in sources:
            for bucket, count in source["histogram_ns"].items():
                bins[int(bucket)] = bins.get(int(bucket), 0) + count
        counts = sum(source["io_count"] for source in sources)
        nbytes = sum(source["io_bytes"] for source in sources)
        samples = sum(source["sample_count"] for source in sources)
        clat_n = sum(source["clat_sample_count"] for source in sources)
        lat_n = sum(source["lat_sample_count"] for source in sources)

        def weighted_mean(field, count_field, count):
            if not count:
                return None
            if any(source[field] is None and source[count_field] for source in sources):
                raise ValueError(f"{direction}.{field}: missing worker latency mean")
            return sum((source[field] or 0) * source[count_field] for source in sources) / count

        active_maxima = [source["clat_max_ns"] for source in sources if source["clat_max_ns"] is not None]
        result[direction] = {
            "iops": counts * 1000 / interval_ms if interval_ms else 0,
            "bw_bytes": nbytes * 1000 / interval_ms if interval_ms else 0,
            "io_count": counts, "io_bytes": nbytes,
            "runtime_ms": interval_ms if counts or nbytes else 0,
            "sample_count": samples, "clat_sample_count": clat_n,
            "lat_sample_count": lat_n,
            "histogram_sample_drift": samples - clat_n,
            "clat_mean_ns": weighted_mean("clat_mean_ns", "clat_sample_count", clat_n),
            "lat_mean_ns": weighted_mean("lat_mean_ns", "lat_sample_count", lat_n),
            "clat_max_ns": max(active_maxima) if active_maxima else None,
            "clat_max_approximate": any(source["clat_max_approximate"] for source in sources),
            "percentiles_ns": _percentiles(bins),
            "histogram_ns": {str(bucket): count for bucket, count in sorted(bins.items())},
            "tail_quality": _tail_quality([samples]),
        }
    read, write = result["read"], result["write"]
    total_bytes = read["io_bytes"] + write["io_bytes"]
    result.update({
        "total_iops": total_count * 1000 / interval_ms if interval_ms else 0,
        "total_bw_bytes": total_bytes * 1000 / interval_ms if interval_ms else 0,
        "achieved_read_pct": 100 * read["io_count"] / total_count if total_count else None,
        "achieved_read_byte_pct": 100 * read["io_bytes"] / total_bytes if total_bytes else None,
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
        result["lat_sample_count"] = sum(_counter(source.get("lat_sample_count", 0),
                                                  f"{direction}.lat_sample_count") for source in sources)
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
    if any("workers" in run for run in runs):
        if not all("workers" in run for run in runs):
            raise ValueError("cannot mix worker and legacy run metrics")
        names = list(runs[0]["workers"])
        if any(set(run["workers"]) != set(names) for run in runs):
            raise ValueError("worker identities differ between runs")
        summary["workers"] = {name: aggregate_runs([run["workers"][name] for run in runs]) for name in names}
        summary["worker_names"] = names
        summary["depth_scope"] = "per_worker_cumulative_job"
    return summary


def _trial_statistics(values):
    stats = _repeat_statistics(values)
    stats["range"] = stats["max"] - stats["min"] if stats["min"] is not None else None
    stats["count"] = sum(value is not None for value in values)
    return stats


def aggregate_trials(trials):
    """Summarize independent preparation trials after local-run aggregation.

    Trial medians and ranges use one value per preparation. Pooled histograms
    are supplied separately and never presented as independent trial evidence.
    """
    if isinstance(trials, dict):
        trial_ids = list(trials)
        values = list(trials.values())
    else:
        values = list(trials)
        trial_ids = list(range(len(values)))
    if not values:
        raise ValueError("cannot aggregate empty trials")
    if any(not isinstance(value, dict) for value in values):
        raise ValueError("trial summary must be an object")
    result = {
        "trial_count": len(values), "independent_preparations": len(values),
        "local_run_count": sum(_counter(value.get("run_count", 1), "trial.run_count") for value in values),
        "trial_ids": trial_ids, "trial_statistics": {}, "quality_flags": [],
        "rate_scope": "median_of_independent_trials",
        "percentile_scope": "median_of_trial_run_percentiles",
        "count_scope": "sum_of_local_runs_across_trials",
        "pooled_percentile_scope": "all_local_run_histograms_descriptive_only",
    }
    if len(values) == 1:
        result["quality_flags"].append("single_independent_trial")
    for metric in ("total_iops", "total_bw_bytes", "cpu_user", "cpu_system",
                   "achieved_read_pct", "achieved_read_byte_pct"):
        stats = _trial_statistics([trial.get(metric) for trial in values])
        result[metric] = stats["median"]
        result["trial_statistics"][metric] = stats
    for direction in ("read", "write"):
        sources = [trial[direction] for trial in values]
        result[direction] = {}
        result["trial_statistics"][direction] = {}
        for metric in ("iops", "bw_bytes", "clat_mean_ns", "clat_max_ns", "lat_mean_ns"):
            stats = _trial_statistics([source.get(metric) for source in sources])
            result[direction][metric] = stats["median"]
            result["trial_statistics"][direction][metric] = stats
        for metric in ("io_count", "io_bytes", "sample_count", "clat_sample_count", "lat_sample_count"):
            result[direction][metric] = sum(_counter(source.get(metric, 0), f"{direction}.{metric}")
                                            for source in sources)
        percentiles = {}
        for p in PERCENTILES:
            key = str(p)
            stats = _trial_statistics([source.get("percentiles_ns", {}).get(key) for source in sources])
            percentiles[key] = stats["median"]
            result["trial_statistics"][direction].setdefault("percentiles_ns", {})[key] = stats
        result[direction]["percentiles_ns"] = percentiles
        bins = {}
        for source in sources:
            samples = _counter(source["sample_count"], f"{direction}.sample_count")
            hist = _histogram({"N": samples, "bins": source.get("histogram_ns", {})}, direction)
            if sum(hist.values()) != samples:
                raise ValueError(f"{direction}: trial histogram does not match sample_count")
            for bucket, count in hist.items():
                bins[bucket] = bins.get(bucket, 0) + count
        result[direction]["histogram_ns"] = {str(bucket): count for bucket, count in sorted(bins.items())}
        result[direction]["pooled_percentiles_ns"] = _percentiles(bins)
        result[direction]["tail_quality"] = _tail_quality([source["sample_count"] for source in sources])
        result[direction]["clat_max_approximate"] = any(source.get("clat_max_approximate", True)
                                                        for source in sources if source.get("clat_max_ns") is not None)
    if any("workers" in trial for trial in values):
        if not all("workers" in trial for trial in values):
            raise ValueError("cannot mix worker and legacy trial summaries")
        names = list(values[0]["workers"])
        if any(set(trial["workers"]) != set(names) for trial in values):
            raise ValueError("worker identities differ between trials")
        result["workers"] = {name: aggregate_trials([trial["workers"][name] for trial in values])
                             for name in names}
        result["worker_names"] = names
    return result


def paired_profile_ratios(profile_trials, baseline_trials):
    """Compute one profile/baseline ratio per matched independent trial."""
    if not isinstance(profile_trials, dict) or not isinstance(baseline_trials, dict):
        raise ValueError("paired trial summaries must be keyed by trial identity")
    if not profile_trials or set(profile_trials) != set(baseline_trials):
        raise ValueError("profile and baseline trial identities must match")
    trial_ids = list(profile_trials)

    def ratios(path):
        per_trial = {}
        for trial_id in trial_ids:
            numerator, denominator = profile_trials[trial_id], baseline_trials[trial_id]
            for part in path:
                numerator, denominator = numerator.get(part), denominator.get(part)
                if numerator is None or denominator is None:
                    break
            if numerator is None or denominator is None:
                per_trial[trial_id] = None
                continue
            numerator = _number(numerator, f"profile {'.'.join(path)}")
            denominator = _number(denominator, f"baseline {'.'.join(path)}")
            per_trial[trial_id] = numerator / denominator if denominator else None
        return {"per_trial": per_trial, "statistics": _trial_statistics(list(per_trial.values()))}

    result = {"trial_count": len(trial_ids), "trial_ids": trial_ids,
              "ratio_scope": "paired_independent_trials",
              "total_iops": ratios(("total_iops",)),
              "total_bw_bytes": ratios(("total_bw_bytes",))}
    for direction in ("read", "write"):
        result[direction] = {metric: ratios((direction, metric))
                             for metric in ("iops", "bw_bytes", "clat_mean_ns")}
        result[direction]["percentiles_ns"] = {
            str(p): ratios((direction, "percentiles_ns", str(p))) for p in PERCENTILES}
    return result


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
