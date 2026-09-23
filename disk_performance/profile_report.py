#!/usr/bin/env python3
"""Offline reports for the manifest-based NVMe profile benchmark.

Metrics are supplied by the runner: schema 2 uses medians across successful
repeats, while schema 1 retains its historical measurement-window semantics.
This renderer never combines directional distributions or averages percentiles.
"""

import base64
import csv
import datetime as dt
import html
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
    """Samples plus an explicit indicator for envelope downsampling."""

    def __init__(self, values=(), downsampled=False):
        super().__init__(values)
        self.downsampled = downsampled


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
    """Stream fio logs into bounded min/mean/max envelopes; raw logs stay intact."""
    result = {"read": TimeSeries(), "write": TimeSeries()}
    if directory is None or not point.get("relative_dir"):
        return result
    root = Path(directory).resolve()
    location = (root / point["relative_dir"]).resolve()
    try:
        location.relative_to(root)
    except ValueError:
        return result
    envelopes = {direction: StreamingEnvelope() for direction in result}
    for path in sorted(location.glob(f"series_{kind}*.log")):
        with path.open(encoding="utf-8") as handle:
            for row in csv.reader(handle):
                if len(row) < 3:
                    continue
                time_ms, value, direction = map(number, row[:3])
                if time_ms is None or value is None or direction not in (0, 1):
                    continue
                envelopes["read" if direction == 0 else "write"].add(time_ms / 1000, value * (1024 if kind == "bw" else 1))
    return {direction: envelope.samples() for direction, envelope in envelopes.items()}


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
                    ax.plot([t for t, _ in logged], [v / scale for _, v in logged], label=label)
                else:
                    ax.plot(elapsed, [float("nan") if v is None else v / scale for v in values], label=direction)
                ax.set(xlabel="Elapsed seconds", ylabel=ylabel)
                ax.grid(alpha=.2)
                ax.legend()
        title = f"{point['profile']} / {point['id']} ({point.get('status', 'pending')})"
        downsampled = any(values.downsampled for series in logs.values() for values in series.values())
        figure.suptitle(title + ("\nThroughput downsampled; bin extrema preserved; raw logs unchanged" if downsampled else ""))
        figure.tight_layout()
        histories.append(f"<details><summary>{esc(title)}</summary>" + image_tag(figure, title) + "</details>")
        plt.close(figure)
    result["histories"] = "".join(histories)
    return result


def render_report(directory, manifest, plots=True):
    """Write offline HTML, summary/raw CSVs and text; return artifact paths."""
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
        output.append('<table><thead><tr><th>Profile</th><th>Early read kIOPS</th><th>Late read kIOPS</th><th>Read IOPS change %</th></tr></thead><tbody>')
        for profile in profiles:
            refs = {ref['id']: ref for ref in manifest['references'] if ref['profile'] == profile}
            early = number(refs.get('reference_early', {}).get('metrics', {}).get('read', {}).get('iops'))
            late = number(refs.get('reference_late', {}).get('metrics', {}).get('read', {}).get('iops'))
            drift = (late / early - 1) * 100 if early and late is not None else None
            values = [profile, fmt(early / 1000 if early is not None else None), fmt(late / 1000 if late is not None else None), fmt(drift)]
            output.append('<tr>' + ''.join(f'<td>{esc(v)}</td>' for v in values) + '</tr>')
            text_parts.append(f'{profile} reference read IOPS drift: {fmt(drift)}% (single samples, not a stability certification)')
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
