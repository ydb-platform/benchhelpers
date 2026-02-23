#!/usr/bin/env python3

import argparse
import csv
import json
import os
import re
import sys
from typing import Dict, List, Optional


PERCENTILE_KEYS = {
    "LatencyP50_us": "50.000000",
    "LatencyP90_us": "90.000000",
    "LatencyP95_us": "95.000000",
    "LatencyP99_us": "99.000000",
    "LatencyP99_9_us": "99.900000",
}

RESULT_FILE_RE = re.compile(
    r"^(?P<engine>.+)_qd(?P<queue_depth>\d+)_(?P<workload>read|write)\.json$"
)


def load_fio_json(path: str) -> Dict[str, object]:
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()

    # fio may prepend warnings (for example from sqpoll/affinity) before JSON.
    first_obj = raw.find("{")
    last_obj = raw.rfind("}")
    if first_obj == -1 or last_obj == -1 or first_obj > last_obj:
        raise ValueError(f"{path}: JSON object boundaries not found")

    payload = raw[first_obj : last_obj + 1]
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: malformed JSON payload ({exc})") from exc


def human_bytes_per_second(value: float) -> str:
    units = ["B/s", "KiB/s", "MiB/s", "GiB/s", "TiB/s"]
    scaled = float(value)
    for unit in units:
        if scaled < 1024.0 or unit == units[-1]:
            return f"{scaled:.2f} {unit}"
        scaled /= 1024.0
    return f"{value:.2f} B/s"


def parse_one_result(path: str, engine: str, queue_depth: int, workload: str) -> Dict[str, object]:
    payload = load_fio_json(path)

    jobs = payload.get("jobs", [])
    if not jobs:
        raise ValueError(f"{path}: no jobs in fio output")

    total_bw_bytes = 0.0
    total_iops = 0.0
    pct_sum = {k: 0.0 for k in PERCENTILE_KEYS}
    pct_count = {k: 0 for k in PERCENTILE_KEYS}

    for job in jobs:
        data = job.get(workload, {})
        total_bw_bytes += float(data.get("bw_bytes", 0.0))
        total_iops += float(data.get("iops", 0.0))

        clat_percentiles = data.get("clat_ns", {}).get("percentile", {})
        for out_key, fio_key in PERCENTILE_KEYS.items():
            val_ns = clat_percentiles.get(fio_key)
            if val_ns is not None:
                pct_sum[out_key] += float(val_ns) / 1000.0
                pct_count[out_key] += 1

    row = {
        "Engine": engine,
        "QueueDepth": queue_depth,
        "Workload": workload,
        "Speed": human_bytes_per_second(total_bw_bytes),
        "Speed_Bps": int(total_bw_bytes),
        "IOPS": int(total_iops),
    }
    for out_key in PERCENTILE_KEYS:
        row[out_key] = int(pct_sum[out_key] / pct_count[out_key]) if pct_count[out_key] else 0

    return row


def collect_rows(results_dir: str) -> List[Dict[str, object]]:
    rows = []
    for name in sorted(os.listdir(results_dir)):
        match = RESULT_FILE_RE.match(name)
        if not match:
            continue

        engine = match.group("engine")
        queue_depth = int(match.group("queue_depth"))
        workload = match.group("workload")
        path = os.path.join(results_dir, name)
        rows.append(parse_one_result(path, engine, queue_depth, workload))

    rows.sort(key=lambda r: (str(r["Engine"]), int(r["QueueDepth"]), str(r["Workload"])))
    return rows


def print_table(rows: List[Dict[str, object]], fieldnames: List[str]) -> None:
    if not rows:
        print("No JSON result files found.")
        return

    widths = {name: len(name) for name in fieldnames}
    for row in rows:
        for name in fieldnames:
            widths[name] = max(widths[name], len(str(row[name])))

    header = "  ".join(f"{name:<{widths[name]}}" for name in fieldnames)
    sep = "  ".join("-" * widths[name] for name in fieldnames)
    print(header)
    print(sep)
    for row in rows:
        print("  ".join(f"{str(row[name]):<{widths[name]}}" for name in fieldnames))


def print_csv(rows: List[Dict[str, object]], fieldnames: List[str]) -> None:
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row[k] for k in fieldnames})


def build_series(rows: List[Dict[str, object]]) -> Dict[str, List[Dict[str, object]]]:
    grouped: Dict[str, List[Dict[str, object]]] = {}
    for row in rows:
        workload = str(row["Workload"])
        engine = str(row["Engine"])
        key = engine if workload == "write" else f"{engine}-{workload}"
        grouped.setdefault(key, []).append(row)

    for key in grouped:
        grouped[key] = sorted(grouped[key], key=lambda r: int(r["QueueDepth"]))
    return grouped


def plot_speed_by_inflight(rows: List[Dict[str, object]], output_dir: str, prefix: str) -> Optional[str]:
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError(
            "plotting requires matplotlib (pip install matplotlib)"
        ) from exc

    grouped = build_series(rows)

    fig, ax = plt.subplots(figsize=(10, 6))
    has_points = False
    for series_name, points in sorted(grouped.items()):
        x_vals = [int(r["QueueDepth"]) for r in points]
        y_vals = [float(r["Speed_Bps"]) / (1024.0 * 1024.0) for r in points]
        if x_vals:
            has_points = True
            ax.plot(x_vals, y_vals, marker="o", label=series_name)

    if not has_points:
        plt.close(fig)
        return None

    ax.set_xlabel("Inflight (QueueDepth)")
    ax.set_ylabel("Speed (MiB/s)")
    ax.set_title("Speed vs Inflight")
    ax.grid(True, linestyle="--", alpha=0.4)
    ax.legend()
    fig.tight_layout()

    output_path = os.path.join(output_dir, f"{prefix}.png")
    fig.savefig(output_path, dpi=120)
    plt.close(fig)
    return output_path


def plot_latency_by_inflight(rows: List[Dict[str, object]], output_dir: str, prefix: str) -> Optional[str]:
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError(
            "plotting requires matplotlib (pip install matplotlib)"
        ) from exc

    grouped = build_series(rows)
    percentile_keys = [
        ("LatencyP50_us", "p50"),
        ("LatencyP90_us", "p90"),
        ("LatencyP99_us", "p99"),
    ]

    fig, axes = plt.subplots(3, 1, figsize=(10, 12), sharex=True)
    has_points = False

    for ax, (field_name, title_suffix) in zip(axes, percentile_keys):
        for series_name, points in sorted(grouped.items()):
            x_vals = [int(r["QueueDepth"]) for r in points]
            y_vals = [float(r[field_name]) for r in points]
            if x_vals:
                has_points = True
                ax.plot(x_vals, y_vals, marker="o", label=series_name)

        ax.set_ylabel("Latency (us)")
        ax.set_title(f"{title_suffix} vs Inflight")
        ax.grid(True, linestyle="--", alpha=0.4)

    if not has_points:
        plt.close(fig)
        return None

    axes[0].legend()
    axes[-1].set_xlabel("Inflight (QueueDepth)")
    fig.tight_layout()

    output_path = os.path.join(output_dir, f"{prefix}_latency.png")
    fig.savefig(output_path, dpi=120)
    plt.close(fig)
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate aio_uring fio latency JSON results.")
    parser.add_argument("results_dir", help="Directory with fio JSON output files.")
    parser.add_argument(
        "--format",
        choices=["table", "csv"],
        default="table",
        help="Output format (default: table).",
    )
    parser.add_argument(
        "--plot",
        action="store_true",
        help="Generate speed and latency (p50/p90/p99) plots.",
    )
    parser.add_argument(
        "--prefix",
        default="speed_inflight",
        help="Image filename prefix for --plot (default: speed_inflight).",
    )
    args = parser.parse_args()

    if not os.path.isdir(args.results_dir):
        print(f"results dir does not exist: {args.results_dir}", file=sys.stderr)
        return 1

    try:
        rows = collect_rows(args.results_dir)
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        print(f"failed to parse results: {exc}", file=sys.stderr)
        return 1

    fieldnames = [
        "Engine",
        "QueueDepth",
        "Workload",
        "Speed",
        "IOPS",
        "LatencyP50_us",
        "LatencyP90_us",
        "LatencyP95_us",
        "LatencyP99_us",
        "LatencyP99_9_us",
    ]

    if args.format == "csv":
        print_csv(rows, fieldnames)
    else:
        print_table(rows, fieldnames)

    if args.plot:
        try:
            speed_plot = plot_speed_by_inflight(rows, args.results_dir, args.prefix)
            latency_plot = plot_latency_by_inflight(rows, args.results_dir, args.prefix)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        generated_paths = [p for p in [speed_plot, latency_plot] if p is not None]
        if not generated_paths:
            print("plot: no data to plot", file=sys.stderr)
        else:
            for path in generated_paths:
                print(f"plot: {path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
