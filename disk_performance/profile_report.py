#!/usr/bin/env python3
"""Offline reports for the manifest-based NVMe profile benchmark.

Schema 3 summarizes independent preparations and paired trial observations.
Schemas 1 and 2 retain their historical report semantics. This renderer never
averages percentiles or treats local repeats as independent preparations.
"""

import base64
import csv
import datetime as dt
import html
import heapq
import io
import json
import math
import statistics
from collections import Counter
from pathlib import Path


DEFAULT_RATIOS = [100, 95, 90, 80, 70, 50, 30, 10, 0]
DEFAULT_DEPTHS = [1, 4, 16, 32, 64]
METRICS = (
    ("read", "iops", "Read kIOPS", 1000),
    ("write", "iops", "Write kIOPS", 1000),
    ("read", "p99", "Read p99 completion latency (us)", 1000),
    ("write", "p99", "Write p99 completion latency (us)", 1000),
)


def metric_definitions(manifest):
    if manifest.get("schema_version", 1) < 2:
        return METRICS
    return tuple((direction, metric,
                  f"{direction.title()} median kIOPS" if metric == "iops" else f"{direction.title()} median run p99 clat (us)", divisor)
                 for direction, metric, _, divisor in METRICS)


def number(value):
    """Reject non-finite or malformed values rather than coloring them as data."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def fmt(value, precision=2):
    value = number(value)
    return "N/A" if value is None else f"{value:,.{precision}f}"


def esc(value):
    return html.escape(str(value), quote=True)


def display_json(value):
    """Exclude large histogram payloads from HTML; retain originals untouched."""
    def compact(item):
        if isinstance(item, dict):
            return {key: compact(child) for key, child in item.items() if key != "histogram_ns"}
        if isinstance(item, list):
            return [compact(child) for child in item]
        return item
    return esc(json.dumps(compact(value), indent=2, sort_keys=True))


def percentile(direction, key):
    for k, value in direction.get("percentiles_ns", {}).items():
        if number(k) == number(key):
            return number(value)
    return None


def active(point, direction):
    return point.get("read_pct") != (0 if direction == "read" else 100)


def completed_runs(point):
    count = number((point.get("metrics") or {}).get("run_count"))
    if count is not None:
        return int(count)
    return sum(run.get("status") == "complete" and bool(run.get("metrics"))
               for run in point.get("runs", []))


def qualified(point):
    count = completed_runs(point)
    requested = number(point.get("run_count"))
    return (bool(point.get("quality")) or bool((point.get("metrics") or {}).get("quality_flags"))
            or (count > 0 and (point.get("status") != "complete" or (requested is not None and count < requested))))


def cell(point, direction, metric, divisor):
    """Keep successful repeated measurements visible with quality qualifiers."""
    if point is None:
        return None, "missing"
    status = point.get("status", "pending")
    if status != "complete" and completed_runs(point) == 0:
        return None, status
    if not active(point, direction):
        return None, "N/A"
    metrics = point.get("metrics", {}).get(direction, {})
    value = percentile(metrics, "99") if metric == "p99" else number(metrics.get(metric))
    if value is None:
        return None, "missing"
    value /= divisor
    return value, fmt(value) + ("*" if qualified(point) else "")


def dimensions(manifest):
    config = manifest.get("config", {})
    points = manifest.get("points", [])
    profiles = config.get("profiles") or list(dict.fromkeys(p["profile"] for p in points))
    ratios = config.get("read_percentages", DEFAULT_RATIOS)
    depths = config.get("queue_depths", DEFAULT_DEPTHS)
    sizes = sorted({p["bs"] for p in points if p.get("pattern") == "random"})
    return profiles, ratios, depths, sizes


def matrix(manifest, profile, bs, direction, metric, divisor):
    _, ratios, depths, _ = dimensions(manifest)
    lookup = {(p["read_pct"], p["qd"]): p for p in manifest.get("points", [])
              if p["profile"] == profile and p["bs"] == bs and p["pattern"] == "random"}
    return [[cell(lookup.get((ratio, depth)), direction, metric, divisor)
             for depth in depths] for ratio in ratios]


def color_scale(manifest, bs, direction, metric, divisor):
    _, ratios, depths, _ = dimensions(manifest)
    values = [cell(p, direction, metric, divisor)[0] for p in manifest.get("points", [])
              if p["bs"] == bs and p["pattern"] == "random" and p["qd"] in depths and p["read_pct"] in ratios]
    values = [v for v in values if v is not None]
    return (min(values), max(values)) if values else (0, 1)


def color(value, limits, latency=False):
    if value is None:
        return "#e5e7eb"
    low, high = limits
    fraction = (value - low) / (high - low) if high > low else 0.5
    fraction = max(0, min(1, fraction))
    start, end = ((255, 245, 235), (217, 72, 1)) if latency else ((239, 243, 255), (49, 130, 189))
    rgb = [round(a + (b - a) * fraction) for a, b in zip(start, end)]
    return "#" + "".join(f"{v:02x}" for v in rgb)


def matrix_html(rows, ratios, depths, limits, latency):
    output = ['<table class="heatmap"><thead><tr><th>Read% / Write%</th>']
    output.extend(f"<th>QD {esc(qd)}</th>" for qd in depths)
    output.append("</tr></thead><tbody>")
    for ratio, row in zip(ratios, rows):
        output.append(f"<tr><th>{esc(ratio)} / {esc(100 - ratio)}</th>")
        output.extend(f'<td style="background:{color(v, limits, latency)}">{esc(label)}</td>'
                      for v, label in row)
        output.append("</tr>")
    output.append("</tbody></table>")
    return "".join(output)


def matrix_text(title, rows, ratios, depths):
    width = max(12, max((len(label) + 1 for row in rows for _, label in row), default=12))
    lines = [title, f"{'Read%/Write%':>12}" + "".join(f"{'QD ' + str(qd):>{width}}" for qd in depths)]
    for ratio, row in zip(ratios, rows):
        lines.append(f"{str(ratio) + '/' + str(100-ratio):>12}" + "".join(f"{label:>{width}}" for _, label in row))
    return "\n".join(lines)


def csv_rows(manifest, raw=False):
    points = manifest.get("points", [])
    if raw:
        points = [{**point, **run, "id": point["id"], "run_index": run.get("index"),
                   "metrics": run.get("metrics", {}), "error": run.get("error"),
                   "quality": run.get("quality", [])}
                  for point in points for run in point.get("runs", [])]
    for point in points:
        metrics = point.get("metrics", {}) or {}
        for direction in ("read", "write"):
            data = metrics.get(direction, {}) or {}
            enabled = active(point, direction)
            row = {key: point.get(key) for key in
                   ("id", "profile", "pattern", "bs", "qd", "read_pct", "status", "elapsed_s", "host_write_bytes", "relative_dir", "error")}
            row.update(direction=direction, active=enabled,
                       kind=point.get("kind", "matrix"),
                       host_write_accounting=point.get("host_write_accounting"),
                       measured_write_bytes=point.get("measured_write_bytes"),
                       run_index=point.get("run_index"), seed=point.get("seed"),
                       requested_run_count=point.get("run_count"),
                       run_count=metrics.get("run_count", 1 if raw and point.get("status") == "complete" else None),
                       quality=json.dumps(point.get("quality", []), sort_keys=True),
                       quality_flags=json.dumps(metrics.get("quality_flags", []), sort_keys=True),
                       tail_quality=json.dumps(data.get("tail_quality", {}), sort_keys=True),
                       percentile_scope=metrics.get("percentile_scope"),
                       repeat_statistics=json.dumps(metrics.get("repeat_statistics", {}), sort_keys=True),
                       conditioning_status=point.get("conditioning_status"),
                       recovery_status=point.get("recovery_status"),
                       clat_max_approximate=data.get("clat_max_approximate"),
                       maximum_observed_clat_ns=data.get("maximum_observed_clat_ns"),
                       sample_count=data.get("sample_count", data.get("io_count")),
                       baseline=json.dumps(point.get("baseline"), sort_keys=True),
                       total_iops=metrics.get("total_iops"), total_bw_bytes=metrics.get("total_bw_bytes"),
                       achieved_read_pct=metrics.get("achieved_read_pct"),
                       achieved_read_byte_pct=metrics.get("achieved_read_byte_pct"),
                       cpu_user=metrics.get("cpu_user"), cpu_system=metrics.get("cpu_system"),
                       depth=json.dumps(metrics.get("depth", {}), sort_keys=True),
                       depth_scope=metrics.get("depth_scope"),
                       stability=json.dumps(point.get("stability", {}), sort_keys=True))
            for key in ("iops", "bw_bytes", "io_count", "clat_mean_ns", "clat_max_ns", "lat_mean_ns"):
                row[key] = data.get(key) if enabled else None
            for key in ("50", "90", "95", "99", "99.9"):
                row[f"clat_p{key}_ns"] = percentile(data, key) if enabled else None
                row[f"pooled_clat_p{key}_ns"] = percentile({"percentiles_ns": data.get("pooled_percentiles_ns", {})}, key) if enabled else None
            stats = metrics.get("repeat_statistics", {}).get(direction, {})
            for name in ("iops", "bw_bytes", "p99"):
                spread = stats.get("percentiles_ns", {}).get("99", {}) if name == "p99" else stats.get(name, {})
                for statistic in ("min", "median", "max", "stdev"):
                    row[f"{name}_{statistic}"] = spread.get(statistic) if enabled else None
            yield row


def detailed_html(manifest):
    output = ['<div class="scroll"><table><thead><tr>']
    repeated = manifest.get("schema_version", 1) >= 2
    headers = ["Profile / point", "Status", "Direction", "Median kIOPS" if repeated else "kIOPS", "MiB/s", "Mean us", "p50 us", "p90 us", "p95 us", "Median run p99 us" if repeated else "p99 us", "p99.9 us", "Median run max us" if repeated else "Max us", "IO count", "Achieved read %", "Host writes GiB", "Successful runs", "IOPS min / max / SD", "Max approximate?", "Quality"]
    output.extend(f"<th>{esc(h)}</th>" for h in headers)
    output.append("</tr></thead><tbody>")
    for row in csv_rows(manifest):
        label = f"{row['profile']} / {row['kind']} {row['pattern']} {row['bs']}B QD{row['qd']} {row['read_pct']}/{100-row['read_pct']}"
        values = [label, row["status"], row["direction"]]
        for key, divisor in (("iops", 1000), ("bw_bytes", 2**20), ("clat_mean_ns", 1000),
                             ("clat_p50_ns", 1000), ("clat_p90_ns", 1000), ("clat_p95_ns", 1000), ("clat_p99_ns", 1000),
                             ("clat_p99.9_ns", 1000), ("clat_max_ns", 1000), ("io_count", 1),
                             ("achieved_read_pct", 1), ("host_write_bytes", 2**30)):
            n = number(row.get(key))
            values.append(fmt(n / divisor if n is not None else None))
        values.extend([f"{row.get('run_count') or 'N/A'} / {row.get('requested_run_count') or 'N/A'}",
                       " / ".join(fmt(row.get(f"iops_{key}")) for key in ("min", "max", "stdev")),
                       row.get("clat_max_approximate", "N/A"), row.get("quality", "[]") + " " + row.get("quality_flags", "[]")])
        output.append("<tr>" + "".join(f"<td>{esc(v)}</td>" for v in values) + "</tr>")
    output.append("</tbody></table></div>")
    return "".join(output)


def image_tag(figure, title):
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=105, bbox_inches="tight")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f'<img loading="lazy" alt="{esc(title)}" src="data:image/png;base64,{encoded}">'


def elapsed_seconds(start, end):
    try:
        return max(0, (dt.datetime.fromisoformat(end) - dt.datetime.fromisoformat(start)).total_seconds())
    except (TypeError, ValueError):
        return None


class TimeSeries(list):
    """Samples plus downsampling and worker-coverage indicators."""

    def __init__(self, values=(), downsampled=False, incomplete_coverage=False):
        super().__init__(values)
        self.downsampled = downsampled
        self.incomplete_coverage = incomplete_coverage


class StreamingEnvelope:
    """Bounded-memory min/mean/max buckets with progressively doubled widths.

    At most 2,048 complete buckets and one partial bucket are retained. Each
    yields up to three points, preserving extrema even in multiweek logs.
    """

    MAX_BUCKETS = 2048

    def __init__(self):
        self.buckets = []
        self.partial = None
        self.width = 1
        self.first = None
        self.last = None

    @staticmethod
    def merge(left, right):
        if left is None:
            return right
        return (left[0] + right[0], left[1] + right[1], left[2] + right[2],
                min(left[3], right[3], key=lambda pair: pair[1]),
                max(left[4], right[4], key=lambda pair: pair[1]))

    def add(self, time_s, value):
        pair = (time_s, value)
        if self.first is None:
            self.first = pair
        self.last = pair
        self.partial = self.merge(self.partial, (1, time_s, value, pair, pair))
        if self.partial[0] < self.width:
            return
        self.buckets.append(self.partial)
        self.partial = None
        if len(self.buckets) >= self.MAX_BUCKETS:
            self.buckets = [self.merge(self.buckets[i], self.buckets[i + 1])
                            for i in range(0, len(self.buckets), 2)]
            self.width *= 2

    def samples(self):
        output = []
        for count, sum_time, sum_value, minimum, maximum in self.buckets + ([self.partial] if self.partial else []):
            output.extend({minimum, (sum_time / count, sum_value / count), maximum})
        if self.first is not None:
            output.extend((self.first, self.last))
        output = list(set(output))
        output.sort()
        return TimeSeries(output, downsampled=self.width > 1)


def fio_series(directory, point, kind):
    """Sum known worker intervals; retain historical logs as point samples.

    fio's ``log_avg_msec`` writes interval-end timestamps. Its ramp transition
    resets the per-job epoch (fio time.c/libfio.c), so our schema-3 logs start
    at zero in the post-ramp measurement clock. Legacy manifests do not state
    averaging/origin and cannot safely be interpreted as intervals.
    """
    result = {"read": TimeSeries(), "write": TimeSeries()}
    if directory is None or not point.get("relative_dir"):
        return result
    root = Path(directory).resolve()
    location = (root / point["relative_dir"]).resolve()
    try:
        location.relative_to(root)
    except ValueError:
        return result
    def samples(path, selected_direction):
        with path.open(encoding="utf-8") as handle:
            for row in csv.reader(handle):
                if len(row) < 3:
                    continue
                time_ms, value, direction = map(number, row[:3])
                if (time_ms is None or time_ms < 0 or value is None or value < 0
                        or direction != selected_direction):
                    continue
                yield time_ms, value * (1024 if kind == "bw" else 1)

    paths = sorted(location.glob(f"series_{kind}*.log"))
    metadata = point.get("log_intervals") or {}
    averaged = metadata.get("mode") == "averaged_contiguous"
    width = number(metadata.get("average_ms"))
    origin = number(metadata.get("origin_ms"))
    averaged = averaged and width is not None and width > 0 and origin is not None and origin >= 0
    expected = len(point.get("worker_layout") or []) if averaged else len(paths)

    def coalesced(path, code):
        last, total = None, 0
        for timestamp, value in samples(path, code):
            if last is not None and timestamp < last:
                continue
            if last is not None and timestamp != last:
                yield last, total
                total = 0
            last = timestamp
            total += value
        if last is not None:
            yield last, total

    for direction, code in (("read", 0), ("write", 1)):
        if averaged and direction not in metadata.get("directions", ("read", "write")):
            continue
        if not averaged:
            # No interval configuration or epoch is known for schema 1/2.
            # Exact timestamps are point observations, including time zero.
            envelope = StreamingEnvelope()
            group_time, group_value, contributors = None, 0, 0
            incomplete = False
            streams = [coalesced(path, code) for path in paths]
            for timestamp, value in heapq.merge(*streams):
                if group_time is not None and timestamp != group_time:
                    envelope.add(group_time / 1000, group_value)
                    incomplete |= contributors < len(paths)
                    group_value, contributors = 0, 0
                group_time = timestamp
                group_value += value
                contributors += 1
            if group_time is not None:
                envelope.add(group_time / 1000, group_value)
                incomplete |= contributors < len(paths)
            result[direction] = envelope.samples()
            result[direction].incomplete_coverage = incomplete
            continue

        def intervals(path):
            previous = origin
            for end, rate in coalesced(path, code):
                if end > previous:
                    yield previous, end, rate
                    previous = end

        # A missing worker log is unknown, not a worker with a zero rate.
        if not paths or (expected and len(paths) != expected):
            result[direction].incomplete_coverage = True
            continue
        workers = [iter(intervals(path)) for path in paths]
        current = [next(worker, None) for worker in workers]
        if any(interval is None for interval in current):
            result[direction].incomplete_coverage = True
            continue
        envelope = StreamingEnvelope()
        cursor = bin_start = origin
        weighted = 0
        while all(interval is not None for interval in current):
            segment_end = min(interval[1] for interval in current)
            rate = sum(interval[2] for interval in current)
            while cursor < segment_end:
                end = min(segment_end, bin_start + width)
                weighted += rate * (end - cursor)
                cursor = end
                if cursor == bin_start + width:
                    envelope.add(cursor / 1000, weighted / width)
                    bin_start, weighted = cursor, 0
            for index, interval in enumerate(current):
                if interval[1] == segment_end:
                    current[index] = next(workers[index], None)
        if cursor > bin_start:
            envelope.add(cursor / 1000, weighted / (cursor - bin_start))
        result[direction] = envelope.samples()
        result[direction].incomplete_coverage = any(interval is not None for interval in current)
    return result


def representative_traces(manifest):
    """Select at most one successful repeat per schema-2 point for display."""
    if manifest.get("schema_version", 1) < 2:
        return manifest.get("points", [])
    traces = []
    for point in manifest.get("points", []):
        runs = [run for run in point.get("runs", []) if run.get("status") == "complete" and run.get("metrics")]
        if not runs:
            continue
        target = number((point.get("metrics") or {}).get("total_iops"))
        rates = [number(run["metrics"].get("total_iops")) for run in runs]
        rates = [rate for rate in rates if rate is not None]
        if target is None:
            target = statistics.median(rates) if rates else 0
        def distance(run):
            rate = number(run["metrics"].get("total_iops"))
            return abs(rate - target) if rate is not None else float("inf")
        run = min(runs, key=distance)
        traces.append({**point, **run, "id": f"{point['id']} / representative repeat {run.get('index', '?')}",
                       "history": run.get("history", [])})
    return traces


COMPARISON_METRICS = (
    ("total_iops", "Total IOPS", 1),
    ("total_bw_bytes", "Total MiB/s", 2**20),
    ("read_iops", "Read IOPS", 1),
    ("write_iops", "Write IOPS", 1),
    ("read_p99_ns", "Read run p99 clat (us)", 1000),
    ("write_p99_ns", "Write run p99 clat (us)", 1000),
)

QD1_LATENCY_FIELDS = (
    ("clat_mean_ns", "Mean clat"),
    ("50", "p50 clat"),
    ("90", "p90 clat"),
    ("95", "p95 clat"),
    ("99", "p99 clat"),
    ("99.9", "p99.9 clat"),
)


def comparison_value(metrics, key):
    """One observation estimate; p99 is the run percentile, never a pooled bin."""
    metrics = metrics or {}
    if key in ("total_iops", "total_bw_bytes"):
        return number(metrics.get(key))
    direction, field = key.split("_", 1)
    data = metrics.get(direction, {}) or {}
    return percentile(data, "99") if field == "p99_ns" else number(data.get(field))


def comparison_eligible(point, preparation, performance_run):
    """One complete observation from a qualified independent preparation."""
    return (performance_run and preparation is not None and preparation.get("qualified") is True
            and preparation.get("trial") == point.get("trial")
            and preparation.get("profile") == point.get("profile")
            and point.get("status") == "complete" and bool(point.get("metrics")))


def qd1_latency_summary(manifest, direction="read"):
    """Compact QD1 latency ranges, with at most one observation per trial."""
    if direction not in ("read", "write"):
        raise ValueError("QD1 latency direction must be read or write")
    anchor = "rr4k_qd1" if direction == "read" else "wr4k_qd1"
    read_pct = 100 if direction == "read" else 0
    preparations = {item.get("id"): item for item in manifest.get("preparation", [])}
    performance_run = manifest.get("config", {}).get("run_type") != "smoke"
    profiles = manifest.get("config", {}).get("profiles") or list(dict.fromkeys(
        item.get("profile") for item in manifest.get("points", []) + manifest.get("references", [])))
    output = {}
    for kind, points in (("primary", manifest.get("points", [])),
                         ("reference", manifest.get("references", []))):
        rows = []
        for profile in profiles:
            by_trial = {}
            for point in points:
                if (point.get("profile") != profile or point.get("anchor") != anchor
                        or point.get("qd") != 1 or point.get("read_pct") != read_pct):
                    continue
                prep = preparations.get(point.get("preparation_id"))
                if comparison_eligible(point, prep, performance_run):
                    by_trial.setdefault(point.get("trial"), point)
            selected = list(by_trial.values())
            row = dict(profile=profile, qualified_trials=len(selected),
                       local_repeats=sum(completed_runs(point) for point in selected))
            for key, _ in QD1_LATENCY_FIELDS:
                values = []
                for point in selected:
                    data = (point.get("metrics") or {}).get(direction, {}) or {}
                    value = number(data.get(key)) if key == "clat_mean_ns" else percentile(data, key)
                    if value is not None:
                        values.append(value / 1000)
                row[key] = (f"{fmt(statistics.median(values))} [{fmt(min(values))}, {fmt(max(values))}] "
                            f"(n={len(values)})" if values else "N/A")
            maxima = []
            exactness = []
            for point in selected:
                runs = [run for run in point.get("runs", [])
                        if run.get("status") == "complete" and run.get("metrics")]
                if runs:
                    sources = [((run.get("metrics") or {}).get(direction, {}), True) for run in runs]
                else:
                    sources = [((point.get("metrics") or {}).get(direction, {}), False)]
                for data, is_run in sources:
                    value = number(data.get("clat_max_ns" if is_run else "maximum_observed_clat_ns"))
                    if value is not None:
                        maxima.append(value / 1000)
                        exactness.append(data.get("clat_max_approximate"))
            precision = ("approximate" if True in exactness else
                         "exactness unknown" if None in exactness else "exact")
            row["worst_observed_clat"] = (f"{fmt(max(maxima))} ({precision}; "
                                            f"{len(maxima)} recorded maxima)" if maxima else "N/A")
            rows.append(row)
        output[kind] = rows
    return output


def comparison_latency_details(observation_rows):
    """Readable microsecond values for each active direction of every observation."""
    for observation in observation_rows:
        for direction in ("read", "write"):
            if observation.get("read_pct") == (0 if direction == "read" else 100):
                continue
            row = {key: observation.get(key) for key in
                   ("kind", "trial", "profile", "anchor", "id", "status", "preparation_qualified")}
            row["direction"] = direction
            for source, destination in (("clat_mean_ns", "clat_mean_us"),
                                        ("lat_mean_ns", "lat_mean_us"),
                                        ("clat_max_ns", "clat_max_us"),
                                        ("maximum_observed_clat_ns", "worst_observed_clat_us"),
                                        ("pooled_clat_p99_ns", "pooled_clat_p99_us")):
                value = number(observation.get(f"{direction}_{source}"))
                row[destination] = fmt(value / 1000 if value is not None else None)
            for p in ("50", "90", "95", "99", "99.9"):
                value = number(observation.get(f"{direction}_clat_p{p}_ns"))
                row[f"clat_p{p}_us"] = fmt(value / 1000 if value is not None else None)
            row["max_approximate"] = observation.get(f"{direction}_clat_max_approximate")
            yield row


def comparison_summary(manifest):
    """Summarize schema-3 observations, using one value per preparation/trial.

    Incomplete or unqualified observations stay in within_trial but do not
    contribute to cross-trial or paired estimates. References are separate
    from primary anchors. Ratios pair profiles inside a trial before computing
    median/range across trials.
    """
    preparations = {record.get("id"): record for record in manifest.get("preparation", [])}
    observations = [("primary", point) for point in manifest.get("points", [])]
    observations += [("reference", point) for point in manifest.get("references", [])]
    within = []
    eligible = {}
    all_preparation_ids = {}
    all_groups = set()
    performance_run = manifest.get("config", {}).get("run_type") != "smoke"
    for kind, point in observations:
        prep = preparations.get(point.get("preparation_id"))
        trial = point.get("trial")
        anchor = point.get("anchor")
        profile = point.get("profile")
        group = (kind, anchor, profile)
        if prep is not None:
            all_preparation_ids.setdefault(group, set()).add(prep.get("id"))
        good = comparison_eligible(point, prep, performance_run)
        for key, label, divisor in COMPARISON_METRICS:
            all_groups.add((kind, anchor, profile, key))
            value = comparison_value(point.get("metrics"), key)
            row = dict(kind=kind, trial=trial, anchor=anchor, profile=profile,
                       observation_id=point.get("id"), preparation_id=point.get("preparation_id"),
                       preparation_qualified=prep.get("qualified") if prep else None,
                       status=point.get("status", "pending"), metric=key, metric_label=label,
                       value=value, display_value=value / divisor if value is not None else None,
                       included=bool(good and value is not None),
                       local_run_count=completed_runs(point),
                       prior_write_bytes=point.get("prior_write_bytes"),
                       prior_write_accounting=point.get("prior_write_accounting"))
            within.append(row)
            if row["included"]:
                eligible.setdefault((kind, anchor, profile, key), []).append(row)
    cross = []
    for kind, anchor, profile, key in sorted(all_groups, key=str):
        rows = eligible.get((kind, anchor, profile, key), [])
        # A duplicate local observation cannot manufacture an independent trial.
        by_trial = {}
        for row in rows:
            by_trial.setdefault(row["trial"], row)
        values = [row["value"] for row in by_trial.values()]
        metric_label, divisor = next((label, scale) for name, label, scale in COMPARISON_METRICS if name == key)
        cross.append(dict(kind=kind, anchor=anchor, profile=profile, metric=key,
                          metric_label=metric_label, qualified_trials=len(by_trial),
                          independent_preparations=len({row["preparation_id"] for row in by_trial.values()}),
                          available_preparations=len(all_preparation_ids.get((kind, anchor, profile), ())),
                          local_repeats=sum(row["local_run_count"] for row in by_trial.values()),
                          minimum=min(values) if values else None,
                          median=statistics.median(values) if values else None,
                          maximum=max(values) if values else None,
                          display_minimum=min(values) / divisor if values else None,
                          display_median=statistics.median(values) / divisor if values else None,
                          display_maximum=max(values) / divisor if values else None,
                          trials=json.dumps(sorted(by_trial, key=str))))
    paired = []
    for kind, anchor, profile, key in sorted(all_groups, key=str):
        if profile == "fresh":
            continue
        baseline = {row["trial"]: row for row in eligible.get((kind, anchor, "fresh", key), [])}
        candidate = {row["trial"]: row for row in eligible.get((kind, anchor, profile, key), [])}
        ratios = []
        for trial in sorted(candidate.keys() & baseline.keys(), key=str):
            first, second = baseline[trial], candidate[trial]
            if first["value"] <= 0:
                continue
            ratio = second["value"] / first["value"]
            ratios.append(dict(kind=kind, anchor=anchor, profile=profile, baseline_profile="fresh",
                               trial=trial, metric=key, ratio=ratio,
                               baseline_observation_id=first["observation_id"],
                               observation_id=second["observation_id"]))
        values = [row["ratio"] for row in ratios]
        paired.append(dict(kind=kind, anchor=anchor, profile=profile, baseline_profile="fresh",
                           metric=key, paired_trials=len(ratios), minimum=min(values) if values else None,
                           median=statistics.median(values) if values else None,
                           maximum=max(values) if values else None, trial_ratios=ratios))
    drift = []
    primary = {(p.get("trial"), p.get("profile"), p.get("anchor")): p for p in manifest.get("points", [])}
    for ref in manifest.get("references", []):
        key = (ref.get("trial"), ref.get("profile"), ref.get("anchor"))
        start = primary.get(key)
        prep = preparations.get(ref.get("preparation_id"))
        valid = (performance_run and start is not None and prep is not None and prep.get("qualified") is True
                 and start.get("preparation_id") == ref.get("preparation_id")
                 and start.get("status") == "complete" and ref.get("status") == "complete")
        for metric in ("read_iops", "read_p99_ns"):
            before = comparison_value(start.get("metrics"), metric) if start else None
            after = comparison_value(ref.get("metrics"), metric)
            drift.append(dict(trial=ref.get("trial"), profile=ref.get("profile"), anchor=ref.get("anchor"),
                              preparation_id=ref.get("preparation_id"), metric=metric,
                              early=before, late=after, change_pct=(100 * (after / before - 1)
                              if valid and before and after is not None else None),
                              qualified=valid, early_observation_id=start.get("id") if start else None,
                              late_observation_id=ref.get("id")))
    return dict(within_trial=within, cross_trial=cross, paired_ratios=paired, drift=drift)


def plot_report(manifest, directory=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    profiles, ratios, depths, sizes = dimensions(manifest)
    result = {}
    for profile in profiles:
        parts = []
        for bs in sizes:
            figure, axes = plt.subplots(2, 2, figsize=(15, max(7, len(ratios) * .75)))
            for ax, (direction, metric, title, divisor) in zip(axes.flat, metric_definitions(manifest)):
                rows = matrix(manifest, profile, bs, direction, metric, divisor)
                values = np.array([[np.nan if v is None else v for v, _ in row] for row in rows])
                low, high = color_scale(manifest, bs, direction, metric, divisor)
                if low == high:
                    high = low + max(abs(low) * .01, 1)
                cmap = plt.get_cmap("Oranges" if metric == "p99" else "Blues").copy()
                cmap.set_bad("#e5e7eb")
                picture = ax.imshow(np.ma.masked_invalid(values), aspect="auto", cmap=cmap, vmin=low, vmax=high)
                ax.set_xticks(range(len(depths)), depths)
                ax.set_yticks(range(len(ratios)), [f"{r}/{100-r}" for r in ratios])
                ax.set(xlabel="Total queue depth", ylabel="Read% / Write%", title=title)
                for y, row in enumerate(rows):
                    for x, (value, label) in enumerate(row):
                        ax.text(x, y, label, ha="center", va="center", fontsize=8,
                                color="white" if value is not None and value > low + .65 * (high-low) else "black")
                figure.colorbar(picture, ax=ax, shrink=.7)
            title = f"{profile}: random {bs // 1024} KiB"
            figure.suptitle(title)
            figure.tight_layout()
            parts.append(image_tag(figure, title))
            plt.close(figure)

            figure, axes = plt.subplots(2, 2, figsize=(14, 8))
            for ax, (direction, metric, title, divisor) in zip(axes.flat, metric_definitions(manifest)):
                for ratio, row in zip(ratios, matrix(manifest, profile, bs, direction, metric, divisor)):
                    if any(v is not None for v, _ in row):
                        ax.plot(depths, [float("nan") if v is None else v for v, _ in row], marker=".", label=f"{ratio}/{100-ratio}")
                ax.set(xlabel="Total queue depth", ylabel=title)
                ax.set_xscale("log", base=2)
                ax.set_xticks(depths, depths)
                ax.grid(alpha=.2)
                if ax.lines:
                    ax.legend(title="Read/write %", fontsize=7)
            figure.suptitle(f"{profile}: {bs // 1024} KiB versus QD")
            figure.tight_layout()
            parts.append(image_tag(figure, f"{profile} {bs} bytes versus QD"))
            plt.close(figure)
        result[profile] = "".join(parts)

    histories = []
    for point in representative_traces(manifest):
        history = point.get("history") or []
        logs = {"iops": fio_series(directory, point, "iops"),
                "bw_bytes": fio_series(directory, point, "bw")}
        if not history and not any(v for series in logs.values() for v in series.values()):
            continue
        figure, axes = plt.subplots(1, 3, figsize=(15, 3.5))
        elapsed = [h.get("elapsed_s", i) for i, h in enumerate(history)]
        for direction in ("read", "write"):
            if not active(point, direction):
                continue
            for ax, key, scale, ylabel in zip(axes, ("iops", "bw_bytes", "clat_mean_ns"),
                                             (1000, 2**20, 1000), ("kIOPS", "MiB/s", "Mean completion latency (us)")):
                values = [number(h.get(direction, {}).get(key)) for h in history]
                logged = logs.get(key, {}).get(direction)
                if logged:
                    label = direction + (" (min/mean/max bins)" if logged.downsampled else "")
                    if logged.incomplete_coverage:
                        label += " (incomplete worker coverage)"
                    ax.plot([t for t, _ in logged], [v / scale for _, v in logged], label=label)
                else:
                    ax.plot(elapsed, [float("nan") if v is None else v / scale for v in values], label=direction)
                ax.set(xlabel="Elapsed seconds", ylabel=ylabel)
                ax.grid(alpha=.2)
                ax.legend()
        title = f"{point['profile']} / {point['id']} ({point.get('status', 'pending')})"
        downsampled = any(values.downsampled for series in logs.values() for values in series.values())
        incomplete = any(values.incomplete_coverage for series in logs.values() for values in series.values())
        figure.suptitle(title + ("\nThroughput downsampled; bin extrema preserved; raw logs unchanged" if downsampled else "")
                        + ("\nIncomplete worker log coverage" if incomplete else ""))
        figure.tight_layout()
        histories.append(f"<details><summary>{esc(title)}</summary>" + image_tag(figure, title) + "</details>")
        plt.close(figure)
    result["histories"] = "".join(histories)
    return result


def _comparison_table(rows, columns):
    output = ['<div class="scroll"><table><thead><tr>']
    output.extend(f"<th>{esc(label)}</th>" for _, label in columns)
    output.append("</tr></thead><tbody>")
    for row in rows:
        cells = []
        for key, _ in columns:
            value = row.get(key)
            label = fmt(value) if isinstance(value, float) else str(value) if value is not None else "N/A"
            body = esc(label)
            if row.get("_prep_anchor") and key == "id":
                body = f'<span id="{esc(row["_prep_anchor"])}">{body}</span>'
            elif row.get("_prep_anchor") and key == "preparation_id":
                body = f'<a href="#{esc(row["_prep_anchor"])}">{body}</a>'
            cells.append(f"<td>{body}</td>")
        output.append("<tr>" + "".join(cells) + "</tr>")
    output.append("</tbody></table></div>")
    return "".join(output)


def _write_rows(directory, filename, rows, fields):
    with (directory / filename).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def comparison_plots(manifest, directory, limit=21):
    """Embed at most one completed run per primary anchor/profile pair.

    Worker logs are aligned and summed by ``fio_series`` before plotting.
    Raw logs remain in their run directories, and envelope extrema survive
    downsampling for large files.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    preparation = {record.get("id"): record for record in manifest.get("preparation", [])}
    chosen = {}
    for point in manifest.get("points", []):
        if point.get("status") != "complete":
            continue
        runs = [run for run in point.get("runs", []) if run.get("status") == "complete" and run.get("relative_dir")]
        if not runs:
            continue
        key = point.get("profile"), point.get("anchor")
        qualified = preparation.get(point.get("preparation_id"), {}).get("qualified") is True
        if key not in chosen or (qualified and not chosen[key][0]):
            chosen[key] = qualified, point, runs[0]
    output = []
    for _, point, run in list(chosen.values())[:limit]:
        series = {kind: fio_series(directory, run, kind) for kind in ("iops", "bw")}
        plot_label = f"trial {point.get('trial')} {point.get('profile')} {point.get('anchor')} / run {run.get('index', 1)}"
        incomplete = any(values.incomplete_coverage for directions in series.values()
                         for values in directions.values())
        if not any(values for directions in series.values() for values in directions.values()):
            if incomplete:
                output.append(f"<p>{esc(plot_label)}: incomplete worker log coverage; aggregate trace unavailable.</p>")
            continue
        figure, axes = plt.subplots(1, 2, figsize=(12, 3.5))
        for ax, kind, scale, ylabel in ((axes[0], "iops", 1000, "kIOPS"),
                                        (axes[1], "bw", 1024**2, "MiB/s")):
            for direction in ("read", "write"):
                values = series[kind][direction]
                if values:
                    series_label = direction + (" (envelope)" if values.downsampled else "")
                    if values.incomplete_coverage:
                        series_label += " (incomplete worker coverage)"
                    ax.plot([time for time, _ in values], [value / scale for _, value in values], label=series_label)
            ax.set(xlabel="Elapsed seconds", ylabel=ylabel)
            ax.grid(alpha=.2)
            if ax.lines:
                ax.legend()
        figure.suptitle(plot_label)
        figure.tight_layout()
        note = "<p>Incomplete worker log coverage; only the shared observed interval is plotted.</p>" if incomplete else ""
        output.append(f"<details><summary>{esc(plot_label)}</summary>{note}{image_tag(figure, plot_label)}</details>")
        plt.close(figure)
    return "".join(output)


def render_comparison_report(directory, manifest, plots=True):
    """Render the schema-3 compact comparison and bounded throughput plots."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    summary = comparison_summary(manifest)
    preps = manifest.get("preparation", [])
    points = manifest.get("points", [])
    refs = manifest.get("references", [])
    order = {entry.get("id"): index for index, entry in enumerate(manifest.get("schedule", []))
             if isinstance(entry, dict) and entry.get("id") is not None}
    observations = sorted(points + refs, key=lambda point: order.get(point.get("id"), len(order)))
    config = manifest.get("config", {})
    qd1_rows = qd1_latency_summary(manifest)
    write_qd1_rows = qd1_latency_summary(manifest, "write")["primary"]
    write_qd1_scheduled = any(point.get("anchor") == "wr4k_qd1" for point in points)
    write_qd1_qualified = any(row["qualified_trials"] for row in write_qd1_rows)
    title = str(config.get("prefix") or "nvme") + " — NVMe compact comparison"
    qualified_preps = sum(prep.get("qualified") is True for prep in preps)
    total_host_writes = sum(number(record.get("host_write_bytes")) or 0 for record in preps + observations)
    write_lower_bound = any(record.get("host_write_accounting") == "measured_only_lower_bound"
                            or any(run.get("host_write_accounting") == "measured_only_lower_bound"
                                   for run in record.get("runs", [])) for record in preps + observations)
    elapsed = elapsed_seconds(manifest.get("started_at"), manifest.get("finished_at") or manifest.get("updated_at"))
    intro = (f"{title}: {manifest.get('status', 'unknown')}; {qualified_preps}/{len(preps)} "
             f"independent preparations {'met preparation criteria' if config.get('run_type') == 'smoke' else 'qualified'}; "
             f"{sum(p.get('status') == 'complete' for p in points)}/{len(points)} "
             f"primary observations complete; {sum(p.get('status') == 'complete' for p in refs)}/{len(refs)} final references complete. "
             f"Elapsed {fmt(elapsed)} s; recorded host writes {'at least ' if write_lower_bound else ''}{fmt(total_host_writes / 2**40)} TiB.")
    semantics = ("Each trial has a separately discarded and initialized preparation for each profile. "
                 "Within-trial rows are local observations; cross-trial medians and ranges use one qualified "
                 "observation per trial. Local repeats and conditioning windows are not independent trials. "
                 "Profile ratios pair observations from the same trial before summarizing ratios. "
                 "Run p99 values are separate from combined histogram p99 values. No equivalence is inferred from overlapping ranges. "
                 "Mixed and write anchors form an ordered sequence, so preceding writes and excluded ramp writes can change device state.")
    if config.get("run_type") == "smoke":
        semantics += (" Smoke measurements are exploratory workflow observations. "
                      "They are excluded from qualified cross-trial summaries, drift, and paired ratios.")
    prep_rows = []
    prep_anchors = {prep.get("id"): f"prep-{index}" for index, prep in enumerate(preps)}
    for prep in preps:
        row = {key: prep.get(key) for key in ("id", "trial", "profile", "status", "qualified", "relative_dir",
                                              "conditioning_status", "recovery_status", "required_write_bytes",
                                              "exposure_write_bytes", "headroom_s", "host_write_bytes",
                                              "host_write_accounting", "error")}
        row["conditioning"] = json.dumps(prep.get("conditioning", {}), sort_keys=True)
        row["quality"] = json.dumps(prep.get("quality", []), sort_keys=True)
        row["_prep_anchor"] = prep_anchors.get(prep.get("id"))
        prep_rows.append(row)
    observation_rows = []
    raw_rows = []
    for point in observations:
            kind = "reference" if any(point is ref for ref in refs) else "primary"
            prep = next((record for record in preps if record.get("id") == point.get("preparation_id")), None)
            common = {key: point.get(key) for key in ("id", "trial", "profile", "anchor", "stage", "preparation_id",
                                                      "status", "relative_dir", "pattern", "bs", "qd", "read_pct",
                                                      "prior_write_bytes", "prior_write_accounting", "host_write_bytes",
                                                      "host_write_accounting", "measured_write_bytes", "ramp_write_bytes",
                                                      "ramp_write_accounting", "error")}
            common.update(kind=kind, preparation_qualified=prep.get("qualified") if prep else None,
                          quality=json.dumps(point.get("quality", []), sort_keys=True),
                          requested_run_count=point.get("run_count"), completed_run_count=completed_runs(point))
            common["_prep_anchor"] = prep_anchors.get(point.get("preparation_id"))
            ramps = [run.get("ramp_write_bytes") for run in point.get("runs", [])]
            if common["ramp_write_bytes"] is None and ramps and all(number(value) is not None for value in ramps):
                common["ramp_write_bytes"] = sum(ramps)
            for run_index, item in [(None, point)] + [(index, run) for index, run in enumerate(point.get("runs", []), 1)]:
                row = common.copy()
                if run_index is not None:
                    row.update(status=item.get("status"), relative_dir=item.get("relative_dir"),
                               host_write_bytes=item.get("host_write_bytes"),
                               host_write_accounting=item.get("host_write_accounting"),
                               measured_write_bytes=item.get("measured_write_bytes"),
                               ramp_write_bytes=item.get("ramp_write_bytes"),
                               ramp_write_accounting=item.get("ramp_write_accounting"),
                               error=item.get("error"), seed=item.get("seed"),
                               quality=json.dumps(item.get("quality", []), sort_keys=True))
                row["run_index"] = item.get("index", run_index) if run_index is not None else None
                metrics = item.get("metrics") or {}
                row["percentile_scope"] = metrics.get("percentile_scope", "single_run_percentile")
                row["depth_scope"] = metrics.get("depth_scope")
                row["worker_layout"] = json.dumps(item.get("worker_layout", []), sort_keys=True)
                row["workers"] = json.dumps({name: {key: worker.get(key) for key in
                                                       ("cpu_user", "cpu_system", "depth", "depth_scope")}
                                             for name, worker in metrics.get("workers", {}).items()}, sort_keys=True)
                row["requested_aggregate_qd"] = item.get("requested_aggregate_qd", point.get("qd"))
                row["depth"] = json.dumps(metrics.get("depth", {}), sort_keys=True)
                row["cpu_user"] = metrics.get("cpu_user")
                row["cpu_system"] = metrics.get("cpu_system")
                row["total_iops"] = comparison_value(metrics, "total_iops")
                row["total_bw_bytes"] = comparison_value(metrics, "total_bw_bytes")
                for direction in ("read", "write"):
                    data = metrics.get(direction, {}) or {}
                    for field in ("iops", "bw_bytes", "io_count", "sample_count", "clat_mean_ns",
                                  "clat_max_ns", "lat_mean_ns", "clat_max_approximate"):
                        row[f"{direction}_{field}"] = data.get(field)
                    row[f"{direction}_maximum_observed_clat_ns"] = data.get(
                        "maximum_observed_clat_ns") if run_index is None else data.get("clat_max_ns")
                    for p in ("50", "90", "95", "99", "99.9"):
                        row[f"{direction}_clat_p{p}_ns"] = percentile(data, p)
                        row[f"{direction}_pooled_clat_p{p}_ns"] = percentile(
                            {"percentiles_ns": data.get("pooled_percentiles_ns", {})}, p)
                    row[f"{direction}_run_p99_ns"] = percentile(data, "99")
                    row[f"{direction}_combined_histogram_p99_ns"] = percentile({"percentiles_ns": data.get("pooled_percentiles_ns", {})}, "99")
                    row[f"{direction}_tail_quality"] = json.dumps(data.get("tail_quality", {}), sort_keys=True)
                (observation_rows if run_index is None else raw_rows).append(row)
    latency_rows = list(comparison_latency_details(observation_rows))
    cross_rows = summary["cross_trial"]
    paired_rows = [{**{key: value for key, value in row.items() if key != "trial_ratios"},
                    "trial_ratios": json.dumps(row["trial_ratios"], sort_keys=True)} for row in summary["paired_ratios"]]
    fields = [key for key in observation_rows[0] if not key.startswith("_")] if observation_rows else list(raw_rows[0]) if raw_rows else ["id", "trial", "profile", "anchor", "preparation_id", "status"]
    raw_fields = list(dict.fromkeys(fields + [key for row in raw_rows for key in row if not key.startswith("_")]))
    _write_rows(directory, "results.csv", observation_rows, fields)
    _write_rows(directory, "raw-runs.csv", raw_rows, raw_fields)
    _write_rows(directory, "preparations.csv", prep_rows, [key for key in prep_rows[0] if not key.startswith("_")] if prep_rows else ["id", "trial", "profile", "qualified"])
    _write_rows(directory, "within-trial.csv", summary["within_trial"], list(summary["within_trial"][0]) if summary["within_trial"] else ["trial", "anchor", "profile", "metric", "value", "included"])
    _write_rows(directory, "cross-trial.csv", cross_rows, list(cross_rows[0]) if cross_rows else ["kind", "anchor", "profile", "metric", "qualified_trials", "median"])
    _write_rows(directory, "paired-ratios.csv", paired_rows, list(paired_rows[0]) if paired_rows else ["kind", "anchor", "profile", "metric", "paired_trials", "median"])
    _write_rows(directory, "drift.csv", summary["drift"], list(summary["drift"][0]) if summary["drift"] else ["trial", "profile", "anchor", "metric", "change_pct"])
    workload_key = ("Workloads: sr1m_qd64 = 1 MiB sequential read; rr4k_qd1/rr4k_qd64 = "
                    "4 KiB random read; rr8k_qd64 = 8 KiB random read; rw4k_qd32 = 4 KiB "
                    "random mixed I/O, 70% reads and 30% writes; wr4k_qd64/wr4k_qd1 = "
                    "4 KiB random write. QD is aggregate across workers. Each run's total IOPS is read plus write; "
                    "medians of total and directional IOPS across repeats need not add up.")
    latency_explanation = ("QD1 completion latency in microseconds. Each percentile is the median of "
                           "local run percentiles for one observation; a cell shows the median "
                           "[minimum, maximum] across qualified independent trials, with n in the cell. "
                           "Pooled histogram percentiles are separate. Worst observed clat is the largest "
                           "run maximum across those trials; exact/approximate reflects its source data. "
                           "Exploratory and smoke observations are excluded.")
    write_qd1_note = ("In the revised protocol, write QD1 uses one 4 KiB random-write worker after the "
                      "write-QD64 anchor and before final read references; it reflects that ordered "
                      "device state. " + ("Qualified trial measurements are shown below."
                      if write_qd1_qualified else
                      "No qualified write-QD1 observations are available yet."
                      if write_qd1_scheduled else
                      "Write QD1 was not measured in this run; write-QD64 is not a substitute."))
    text_parts = [intro, semantics, workload_key, latency_explanation]
    for kind, heading in (("primary", "Primary QD1 read latency"),
                          ("reference", "Final-reference QD1 read latency")):
        text_parts.append(heading + ":")
        for row in qd1_rows[kind]:
            cells = "; ".join(f"{label}={row[key]} us" for key, label in QD1_LATENCY_FIELDS)
            text_parts.append(f"{row['profile']}: {row['qualified_trials']} qualified independent trials; "
                              f"{row['local_repeats']} local repeats; {cells}; "
                              f"worst observed clat={row['worst_observed_clat']} us")
    text_parts.append("Primary QD1 write latency: " + write_qd1_note)
    for row in write_qd1_rows:
        cells = "; ".join(f"{label}={row[key]} us" for key, label in QD1_LATENCY_FIELDS)
        text_parts.append(f"{row['profile']}: {row['qualified_trials']} qualified independent trials; "
                          f"{row['local_repeats']} local repeats; {cells}; "
                          f"worst observed clat={row['worst_observed_clat']} us")
    text_parts.append("Preparation qualification and exposure:")
    text_parts.append("Scheduled order: " + " -> ".join(str(entry.get("id", entry.get("kind", "?"))) for entry in manifest.get("schedule", []) if isinstance(entry, dict)))
    for row in prep_rows:
        text_parts.append(f"trial {row['trial']} {row['profile']} {row['id']}: status={row['status']}; qualified={row['qualified']}; "
                          f"exposure={fmt(row['exposure_write_bytes'], 0)}/{fmt(row['required_write_bytes'], 0)} bytes; "
                          f"headroom={fmt(row['headroom_s'])} s; conditioning={row['conditioning_status']}; "
                          f"recovery={row['recovery_status']}; quality={row['quality']}")
    text_parts.append("Within-trial observations and preceding writes:")
    for point in observations:
        text_parts.append(f"trial {point.get('trial')} {point.get('profile')} {point.get('anchor')} {point.get('id')}: "
                          f"status={point.get('status')}; preparation={point.get('preparation_id')}; "
                          f"prior writes={fmt(point.get('prior_write_bytes'), 0)} bytes ({point.get('prior_write_accounting')}); "
                          f"runs={completed_runs(point)}; read run p99={fmt(comparison_value(point.get('metrics'), 'read_p99_ns'))} ns; "
                          f"read combined histogram p99={fmt(percentile({'percentiles_ns': (point.get('metrics') or {}).get('read', {}).get('pooled_percentiles_ns', {})}, '99'))} ns")
    text_parts.append("Directional latency detail (microseconds; observation values are medians of local runs):")
    for row in latency_rows:
        values = "; ".join(f"{name}={row[key]}" for key, name in
                           (("clat_mean_us", "clat mean"), ("clat_p50_us", "p50"),
                            ("clat_p90_us", "p90"), ("clat_p95_us", "p95"),
                            ("clat_p99_us", "p99"), ("clat_p99.9_us", "p99.9"),
                            ("clat_max_us", "run max"),
                            ("worst_observed_clat_us", "worst observed clat"),
                            ("lat_mean_us", "total latency mean")))
        text_parts.append(f"trial {row['trial']} {row['profile']} {row['anchor']} {row['kind']} "
                          f"{row['direction']} {row['id']}: {values}; "
                          f"max approximate={row['max_approximate']}")
    text_parts.append("Cross-trial qualified results (minimum / median / maximum; independent trial counts):")
    for row in cross_rows:
        text_parts.append(f"{row['kind']} {row['anchor']} {row['profile']} {row['metric_label']}: "
                          f"{fmt(row['display_minimum'])} / {fmt(row['display_median'])} / {fmt(row['display_maximum'])}; "
                          f"{row['qualified_trials']} trials, {row['independent_preparations']} independent preparations, "
                          f"{row['local_repeats']} local repeats")
    text_parts.append("Paired profile/fresh ratios (minimum / median / maximum):")
    for row in paired_rows:
        text_parts.append(f"{row['kind']} {row['anchor']} {row['profile']}/fresh {row['metric']}: "
                          f"{fmt(row['minimum'])} / {fmt(row['median'])} / {fmt(row['maximum'])}; "
                          f"{row['paired_trials']} paired trials; {row['trial_ratios']}")
    text_parts.append("Early-to-final reference drift:")
    for row in summary["drift"]:
        text_parts.append(f"trial {row['trial']} {row['profile']} {row['anchor']} {row['metric']}: {fmt(row['change_pct'])}%")
    if manifest.get("error"):
        text_parts.append("Run stopped: " + str(manifest["error"]))
    css = "body{font:15px system-ui,sans-serif;margin:2rem;color:#172033;background:#fafbfc}table{border-collapse:collapse;margin:1rem 0;background:white}th,td{padding:.45rem .65rem;border:1px solid #ccd3dd;text-align:right;white-space:nowrap}th{background:#f1f4f8}.scroll{overflow:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere}details{margin:1rem 0}summary{cursor:pointer}.warning{background:#fff2cc;padding:1rem}"
    output = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
              f"<title>{esc(title)}</title><style>{css}</style></head><body><h1>{esc(intro)}</h1><p>{esc(semantics)}</p>"]
    if manifest.get("error"):
        output.append(f'<p class="warning">{esc(manifest["error"])}</p>')
    for warning in manifest.get("warnings", []):
        output.append(f'<p class="warning">{esc(warning)}</p>')
    output.append(f"<p>{esc(workload_key)}</p>")
    output.append("<h2>4 KiB random-read QD1 latency</h2>" + f"<p>{esc(latency_explanation)}</p>")
    qd1_columns = ([('profile', 'Profile'), ('qualified_trials', 'Qualified trials'),
                    ('local_repeats', 'Local repeats')]
                   + [(key, label + ' µs median [min, max]') for key, label in QD1_LATENCY_FIELDS]
                   + [('worst_observed_clat', 'Worst observed clat µs')])
    for kind, heading in (("primary", "Primary anchor"), ("reference", "Final reference")):
        output.append(f"<h3>{heading}</h3>")
        output.append(_comparison_table(qd1_rows[kind], qd1_columns))
    output.append("<h2>4 KiB random-write QD1 latency</h2>" + f"<p>{esc(write_qd1_note)}</p>")
    output.append(_comparison_table(write_qd1_rows, qd1_columns))
    output.append("<h2>Preparation qualification and exposure</h2>")
    output.append(_comparison_table(prep_rows, [("trial", "Trial"), ("profile", "Profile"), ("id", "Preparation ID"),
                                                 ("status", "Status"), ("qualified", "Qualified"),
                                                 ("required_write_bytes", "Required write bytes"),
                                                 ("exposure_write_bytes", "Exposure write bytes"),
                                                 ("headroom_s", "Cap headroom s"),
                                                 ("conditioning_status", "Conditioning"), ("recovery_status", "Recovery"),
                                                 ("conditioning", "Stability assessment"),
                                                 ("host_write_bytes", "Host write bytes"), ("quality", "Quality")]))
    output.append("<h2>Within-trial observations</h2><p>Each value is one local observation linked to its preparation; excluded values remain visible.</p>")
    output.append(_comparison_table(summary["within_trial"], [("kind", "Kind"), ("trial", "Trial"),
                                                               ("anchor", "Anchor"), ("profile", "Profile"),
                                                               ("preparation_id", "Preparation ID"),
                                                               ("metric_label", "Metric"), ("display_value", "Value"),
                                                               ("included", "Included"),
                                                               ("local_run_count", "Local repeats"),
                                                               ("prior_write_bytes", "Prior write bytes")]))
    output.append("<h2>Cross-trial summary</h2><p>One qualified value per independent preparation and trial; minimum, median, and maximum are descriptive ranges.</p>")
    output.append(_comparison_table(cross_rows, [("kind", "Kind"), ("anchor", "Anchor"), ("profile", "Profile"),
                                                 ("metric_label", "Metric"), ("qualified_trials", "Qualified trials"),
                                                 ("independent_preparations", "Independent preparations"),
                                                 ("local_repeats", "Local repeats"),
                                                 ("display_minimum", "Minimum"), ("display_median", "Median"),
                                                 ("display_maximum", "Maximum")]))
    output.append("<h2>Paired profile ratios</h2><p>Each ratio uses the same trial and anchor, profile divided by fresh.</p>")
    output.append(_comparison_table(paired_rows, [("kind", "Kind"), ("anchor", "Anchor"), ("profile", "Profile"),
                                                  ("metric", "Metric"), ("paired_trials", "Pairs"),
                                                  ("minimum", "Minimum"), ("median", "Median"),
                                                  ("maximum", "Maximum")]))
    paired_trials = [trial for row in summary["paired_ratios"] for trial in row["trial_ratios"]]
    output.append("<h3>Individual trial pairs</h3>")
    output.append(_comparison_table(paired_trials, [("trial", "Trial"), ("kind", "Kind"),
                                                     ("anchor", "Anchor"), ("profile", "Profile"),
                                                     ("metric", "Metric"), ("ratio", "Profile / fresh"),
                                                     ("baseline_observation_id", "Fresh observation"),
                                                     ("observation_id", "Profile observation")]))
    output.append("<h2>Ordered observations and preceding writes</h2>")
    output.append(_comparison_table(observation_rows, [("trial", "Trial"), ("profile", "Profile"),
                                                       ("anchor", "Anchor"), ("id", "Observation ID"),
                                                       ("preparation_id", "Preparation ID"), ("status", "Status"),
                                                       ("preparation_qualified", "Prep qualified"),
                                                       ("prior_write_bytes", "Prior write bytes incl ramps"),
                                                       ("prior_write_accounting", "Prior write accounting"),
                                                       ("ramp_write_bytes", "Ramp write bytes"),
                                                       ("total_iops", "Total IOPS"),
                                                       ("requested_aggregate_qd", "Aggregate requested QD"),
                                                       ("cpu_user", "CPU user %"),
                                                       ("cpu_system", "CPU system %"),
                                                       ("read_run_p99_ns", "Read run p99 ns"),
                                                       ("read_combined_histogram_p99_ns", "Read combined histogram p99 ns")]))
    output.append("<details><summary>Directional latency for every observation</summary>"
                  "<p>Microseconds. Observation percentiles are medians of local run percentiles; "
                  "pooled histogram p99 is shown separately. Run maximum is the median of local run "
                  "maxima; worst observed clat is the largest local maximum.</p>")
    output.append(_comparison_table(latency_rows, [("kind", "Kind"), ("trial", "Trial"),
                                                       ("profile", "Profile"), ("anchor", "Anchor"),
                                                       ("id", "Observation ID"), ("status", "Status"),
                                                       ("preparation_qualified", "Prep qualified"),
                                                       ("direction", "Direction"),
                                                       ("clat_mean_us", "Mean clat µs"),
                                                       ("clat_p50_us", "p50 µs"),
                                                       ("clat_p90_us", "p90 µs"),
                                                       ("clat_p95_us", "p95 µs"),
                                                       ("clat_p99_us", "p99 µs"),
                                                       ("clat_p99.9_us", "p99.9 µs"),
                                                       ("clat_max_us", "Run max µs"),
                                                       ("worst_observed_clat_us", "Worst observed clat µs"),
                                                       ("max_approximate", "Max approximate?"),
                                                       ("lat_mean_us", "Total latency mean µs"),
                                                       ("pooled_clat_p99_us", "Pooled histogram p99 µs")]))
    output.append("</details>")
    output.append("<h2>Early-to-final read reference drift</h2>")
    output.append(_comparison_table(summary["drift"], [("trial", "Trial"), ("profile", "Profile"), ("anchor", "Anchor"),
                                                      ("metric", "Metric"), ("early", "Early"), ("late", "Final"),
                                                      ("change_pct", "Change %"), ("qualified", "Qualified")]))
    output.append("<h2>Representative throughput over time</h2><p>At most one completed run per primary anchor and profile is shown, with a maximum of 21 plots. Worker intervals are aligned and summed before downsampling. Full logs remain in each run directory.</p>")
    if plots:
        try:
            output.append(comparison_plots(manifest, directory) or "<p>No completed run logs are available for plotting.</p>")
        except (ImportError, ValueError, OSError, RuntimeError) as exc:
            warning = f"PNG plots unavailable: {exc}. HTML/CSV/text remain available."
            output.append(f'<p class="warning">{esc(warning)}</p>')
            text_parts.append(warning)
    else:
        output.append("<p>Plots can be generated offline with aggregate.py after the run.</p>")
    output.append("<h2>Schedule and raw records</h2>")
    for label, value in (("Scheduled order", manifest.get("schedule", [])), ("Preparation records", preps),
                         ("Observations", observations), ("Configuration", config),
                         ("Device and environment", manifest.get("device", {})),
                         ("Budget", manifest.get("budget", {})), ("Telemetry", manifest.get("telemetry", []))):
        output.append(f"<details><summary>{esc(label)}</summary><pre>{display_json(value)}</pre></details>")
    output.append("</body></html>")
    (directory / "report.html").write_text("\n".join(output), encoding="utf-8")
    (directory / "result.txt").write_text("\n\n".join(text_parts) + "\n", encoding="utf-8")
    return {name: str(directory / name) for name in ("report.html", "result.txt", "results.csv", "raw-runs.csv",
                                                   "preparations.csv", "within-trial.csv", "cross-trial.csv",
                                                   "paired-ratios.csv", "drift.csv")}


def render_report(directory, manifest, plots=True):
    """Write offline HTML, summary/raw CSVs and text; return artifact paths."""
    if manifest.get("schema_version") == 3 and manifest.get("config", {}).get("protocol") == "comparison":
        return render_comparison_report(directory, manifest, plots=plots)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    profiles, ratios, depths, sizes = dimensions(manifest)
    statuses = Counter(p.get("status", "pending") for p in manifest.get("points", []))
    title = str(manifest.get("config", {}).get("prefix") or "nvme") + " — NVMe benchmark"
    summary = f"{title}: {manifest.get('status', 'unknown')} — {len(manifest.get('points', []))} points; " + "; ".join(f"{n} {s}" for s, n in sorted(statuses.items()))
    duration = elapsed_seconds(manifest.get("started_at"), manifest.get("finished_at") or manifest.get("updated_at"))
    preparation = manifest.get("preparation", [])
    repeated = manifest.get("schema_version", 1) >= 2
    prep_bytes = sum(number(p.get("host_write_bytes")) or 0 for p in preparation)
    workload_bytes = sum(number(p.get("host_write_bytes")) or 0 for p in manifest.get("points", []))
    reference_bytes = sum(number(p.get("host_write_bytes")) or 0 for p in manifest.get("references", []))
    records = preparation + manifest.get("points", []) + manifest.get("references", [])
    lower_bound = any(record.get("host_write_accounting") == "measured_only_lower_bound"
                      or any(run.get("host_write_accounting") == "measured_only_lower_bound" for run in record.get("runs", []))
                      for record in records)
    qualification = "at least " if lower_bound else ""
    totals = f"Elapsed: {fmt(duration)} seconds. Host writes: {qualification}{fmt((prep_bytes + workload_bytes + reference_bytes) / 2**40)} TiB total ({fmt(prep_bytes / 2**40)} TiB preparation + {fmt(workload_bytes / 2**40)} TiB workloads + {fmt(reference_bytes / 2**40)} TiB references)."
    if lower_bound:
        totals += " Lower bound: target kernel counters were unavailable for some work; excluded warmup writes may be missing."
    semantics = ("Throughput/latency cells are medians across successful repeats; p99 is the median of per-run p99 values, not a pooled percentile. Counts sum successful repeats; achieved ratios use pooled counts. Preparation occurs once per profile and device state evolves during measurements. Warmup is excluded. This is practical characterization, not steady-state certification."
                 if repeated else "Legacy schema 1 report: metrics retain their recorded measurement-window semantics; no repeat aggregation is inferred.")
    legend = "Numeric cells with * have quality qualifications or incomplete repeats; see detailed results. Inactive directions are N/A. Unavailable measurements are gray. Conditioning status is separate from measurement status."
    text_parts = [summary, totals, semantics, "Completion latency is reported in microseconds. Ratios are read/write operation percentages.", legend]
    plot_parts = {}
    plot_warning = None
    if plots:
        try:
            plot_parts = plot_report(manifest, directory)
        except (ImportError, ValueError, OSError, RuntimeError) as exc:
            plot_warning = f"PNG plots unavailable: {exc}. HTML/CSV/text remain available; check plotting dependencies and rerun aggregate.py to retry."
            text_parts.append(plot_warning)
    css = "body{font:15px system-ui,sans-serif;margin:2rem;color:#172033;background:#fafbfc}h1{font-size:1.7rem}h2{margin-top:2rem}table{border-collapse:collapse;margin:1rem 0;background:white}th,td{padding:.45rem .65rem;border:1px solid #ccd3dd;text-align:right;white-space:nowrap}th{background:#f1f4f8}pre{white-space:pre-wrap;overflow-wrap:anywhere}img{max-width:100%;height:auto}.scroll{overflow:auto}.warning{background:#fff2cc;padding:1rem}.matrices{display:flex;gap:1rem;flex-wrap:wrap}details{margin:1rem 0}summary{cursor:pointer}"
    output = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
              f"<title>{esc(title)}</title><style>{css}</style></head><body><h1>{esc(summary)}</h1><p>{esc(totals)}</p>",
              "<p>Ratios are read/write operation percentages; QD is the shared total queue budget. Latencies are completion latencies, not application durability measurements. Gray cells are unavailable or invalid; N/A denotes an inactive direction.</p>",
              "<p>Heatmap limits are shared across profiles for each block size and metric. Darker blue means higher throughput; darker orange means higher latency.</p>",
              f"<p>{esc(semantics)}</p><p>{esc(legend)}</p>",
              f"<p>Started: {esc(manifest.get('started_at', 'unknown'))}; updated: {esc(manifest.get('updated_at', 'unknown'))}</p>"]
    if plot_warning:
        output.append(f'<p class="warning">{esc(plot_warning)}</p>')
    if manifest.get("error"):
        error = "Run stopped: " + str(manifest["error"])
        text_parts.append(error)
        output.append(f'<p class="warning">{esc(error)}</p>')
    for warning in manifest.get("warnings", []):
        output.append(f'<p class="warning">{esc(warning)}</p>')
    for name, value in (("Device and environment", manifest.get("device", {})), ("Configuration", manifest.get("config", {})),
                        ("Whole-run time budget and estimate", manifest.get("budget", {})),
                        ("Read-only engine capability check", manifest.get("capability_check", {}))):
        output.append(f"<details><summary>{esc(name)}</summary><pre>{esc(json.dumps(value, indent=2, sort_keys=True))}</pre></details>")
    if manifest.get("budget"):
        text_parts.append("Whole-run budget: " + json.dumps(manifest["budget"], sort_keys=True))
    output.append('<h2>Preparation (once per profile in schema 2)</h2><div class="scroll"><table><thead><tr><th>Profile / record</th><th>Preparation status</th><th>Conditioning status</th><th>Recovery status</th><th>Active GiB</th><th>Host writes GiB</th><th>Elapsed seconds</th><th>Quality / error</th></tr></thead><tbody>')
    for record in preparation:
        seconds = elapsed_seconds(record.get("started_at"), record.get("finished_at") or manifest.get("updated_at"))
        label = f"{record.get('profile', '')} / {record.get('relative_dir', '')}"
        size = number(record.get("size_bytes"))
        written = number(record.get("host_write_bytes"))
        details = "; ".join(record.get("quality", []) + ([record["error"]] if record.get("error") else []))
        values = [label, record.get("status", "unknown"), record.get("conditioning_status", "not recorded"), record.get("recovery_status", "N/A"), fmt(size / 2**30 if size is not None else None),
                  fmt(written / 2**30 if written is not None else None), fmt(seconds), details]
        output.append("<tr>" + "".join(f"<td>{esc(v)}</td>" for v in values) + "</tr>")
        text_parts.append(f"Preparation {label}: {record.get('status', 'unknown')}; conditioning={record.get('conditioning_status', 'not recorded')}; recovery={record.get('recovery_status', 'N/A')}; {fmt(written)} host bytes; {fmt(seconds)} seconds; {details}")
    output.append("</tbody></table></div>")
    output.append(f"<details><summary>Raw preparation records (histograms retained in files)</summary><pre>{display_json(preparation)}</pre></details>")
    for profile in profiles:
        output.append(f"<h2>{esc(profile)}</h2>")
        if repeated:
            notes = {"fresh": "Discard and initialize the measured range once, without extra aging. Subsequent writes evolve the device state.",
                     "medium": "Initialize the configured occupancy once, apply a bounded light random overwrite (at most 5% of the range), then recover.",
                     "sustained": "Initialize the configured occupancy once and apply bounded high-QD conditioning; time-cap and observed-stability status are recorded separately from successful measurements."}
            output.append(f"<p>{esc(notes.get(profile, 'See recorded preparation policy.'))}</p>")
        for bs in sizes:
            output.append(f'<h3>Random {esc(bs // 1024)} KiB</h3><div class="matrices">')
            for direction, metric, title, divisor in metric_definitions(manifest):
                rows = matrix(manifest, profile, bs, direction, metric, divisor)
                limits = color_scale(manifest, bs, direction, metric, divisor)
                output.append(f"<section><h4>{esc(title)}</h4>" + matrix_html(rows, ratios, depths, limits, metric == "p99") + "</section>")
                text_parts.append(matrix_text(f"{profile} / random {bs // 1024} KiB / {title}", rows, ratios, depths))
            output.append("</div>")
        output.append(plot_parts.get(profile, ""))
    text_parts.append("All points (throughput includes both active directions):")
    for point in manifest.get("points", []):
        metrics = point.get("metrics", {}) or {}
        text_parts.append(f"{point['profile']} / {point['id']}: {point.get('status', 'pending')}, successful runs={completed_runs(point)}, total IOPS={fmt(metrics.get('total_iops'))}, "
                          f"elapsed={fmt(point.get('elapsed_s'))}s, host writes={fmt((number(point.get('host_write_bytes')) or 0) / 2**30)} GiB"
                          + (f"; quality={point.get('quality', [])}; flags={metrics.get('quality_flags', [])}" if qualified(point) else "")
                          + (f"; {point['error']}" if point.get("error") else ""))
    output.extend(["<h2>Detailed results</h2>", detailed_html(manifest), "<h2>Performance over time</h2>",
                   "<p>Schema 2 plots show one representative successful repeat per point: closest total IOPS to the point median. Every repeat remains in raw-runs.csv, manifest.json, and the recorded run directories. Histogram payloads remain in raw JSON, not embedded HTML.</p>",
                   plot_parts.get("histories", "<p>Time-series plots are generated at the end of the run. Raw history is retained in manifest.json.</p>")])
    output.append("<p>Median per-run percentiles and pooled percentiles are distinct statistics. CSV preserves both, alongside repeat spread and sample counts. Tail-sample flags are advisory, not confidence guarantees. Approximate maxima are labeled explicitly.</p>")
    if manifest.get("references"):
        output.extend(["<h2>Early/late read references and drift</h2>",
                       "<p>These separate 4 KiB QD1 read references track state drift during each profile; they are not matrix repeats.</p>",
                       detailed_html({**manifest, "points": manifest["references"]}),
                       f"<details><summary>Reference and drift records</summary><pre>{display_json(manifest['references'])}</pre></details>"])
        schema3 = manifest.get("schema_version") == 3
        if schema3:
            groups = {}
            for ref in manifest["references"]:
                if ref.get("stage") in ("early", "late"):
                    key = (ref.get("trial"), ref.get("preparation_id"), ref.get("profile"))
                    groups.setdefault(key, {})[ref["stage"]] = ref
            drift_rows = [(profile, trial, prep_id, refs)
                          for (trial, prep_id, profile), refs in sorted(groups.items(), key=str)]
        else:
            drift_rows = [(profile, None, None,
                           {stage: next((ref for ref in manifest["references"]
                                         if ref.get("profile") == profile and ref.get("id") == f"reference_{stage}"), {})
                            for stage in ("early", "late")}) for profile in profiles]
        heading = ("<th>Trial</th><th>Preparation ID</th>" if schema3 else "")
        output.append(f'<table><thead><tr><th>Profile</th>{heading}<th>Early read kIOPS</th><th>Late read kIOPS</th><th>Read IOPS change %</th></tr></thead><tbody>')
        for profile, trial, prep_id, refs in drift_rows:
            early = number(refs.get('early', {}).get('metrics', {}).get('read', {}).get('iops'))
            late = number(refs.get('late', {}).get('metrics', {}).get('read', {}).get('iops'))
            drift = (late / early - 1) * 100 if early and late is not None else None
            values = [profile] + ([trial, prep_id] if schema3 else []) + [
                fmt(early / 1000 if early is not None else None),
                fmt(late / 1000 if late is not None else None), fmt(drift)]
            output.append('<tr>' + ''.join(f'<td>{esc(v)}</td>' for v in values) + '</tr>')
            identity = f'trial {trial} {prep_id} ' if schema3 else ''
            text_parts.append(f'{identity}{profile} reference read IOPS drift: {fmt(drift)}% (single samples, not a stability certification)')
        output.append('</tbody></table>')
    for point in manifest.get("points", []):
        details = {key: point.get(key) for key in ("baseline", "elapsed_s", "host_write_bytes", "stability", "conditioning_status", "quality", "error", "relative_dir")}
        details["metrics"] = point.get("metrics", {})
        details["runs"] = point.get("runs", [])
        output.append(f"<details><summary>{esc(point['id'])}: preparation, repeat summaries and raw paths</summary><pre>{display_json(details)}</pre></details>")
    output.append(f"<details><summary>Telemetry</summary><pre>{esc(json.dumps(manifest.get('telemetry', []), indent=2, sort_keys=True))}</pre></details></body></html>")
    (directory / "report.html").write_text("\n".join(output), encoding="utf-8")
    (directory / "result.txt").write_text("\n\n".join(text_parts) + "\n", encoding="utf-8")
    for filename, raw in (("results.csv", False), ("raw-runs.csv", True)):
        rows = list(csv_rows(manifest, raw=raw))
        with (directory / filename).open("w", newline="", encoding="utf-8") as handle:
            if rows:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
    return {name: str(directory / name) for name in ("report.html", "results.csv", "raw-runs.csv", "result.txt")}
