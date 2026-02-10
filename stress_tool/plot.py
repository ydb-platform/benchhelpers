#!/usr/bin/env python3

import argparse
import json
import math
import os
import re
from typing import Any, Dict, List, Tuple


def _parse_float(s: Any) -> float:
    if s is None:
        return float("nan")
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip()
    if s == "":
        return float("nan")
    return float(s)


_BW_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+/s)\s*$")
_LAT_US_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*us\s*$")


def _bandwidth_to_mbs(s: Any) -> float:
    """
    Convert strings like '205.3 MB/s' to MB/s float.
    Supports: B/s, KB/s, MB/s, GB/s, KiB/s, MiB/s, GiB/s.
    """
    if s is None:
        return float("nan")
    if isinstance(s, (int, float)):
        # Assume already MB/s.
        return float(s)
    raw = str(s).strip()
    if raw == "":
        return float("nan")

    m = _BW_RE.match(raw)
    if not m:
        raise ValueError(f"Unrecognized bandwidth value: {raw!r}")
    val = float(m.group(1))
    unit = m.group(2)

    # Convert to bytes/sec first, then MB/s (10^6).
    if unit == "B/s":
        bps = val
    elif unit == "KB/s":
        bps = val * 1_000
    elif unit == "MB/s":
        bps = val * 1_000_000
    elif unit == "GB/s":
        bps = val * 1_000_000_000
    elif unit == "KiB/s":
        bps = val * 1024
    elif unit == "MiB/s":
        bps = val * 1024**2
    elif unit == "GiB/s":
        bps = val * 1024**3
    else:
        raise ValueError(f"Unsupported bandwidth unit: {unit!r} (value={raw!r})")

    return bps / 1_000_000.0


def _latency_to_us(s: Any) -> float:
    """
    Convert strings like '39 us' to float microseconds.
    """
    if s is None:
        return float("nan")
    if isinstance(s, (int, float)):
        return float(s)
    raw = str(s).strip()
    if raw == "":
        return float("nan")
    m = _LAT_US_RE.match(raw)
    if not m:
        raise ValueError(f"Unrecognized latency value: {raw!r}")
    return float(m.group(1))


def _iops_to_kiops(s: Any) -> float:
    """
    Convert IOPS value to kIOPS (thousands of IOPS).
    Input is typically a string number like "104301".
    """
    return _parse_float(s) / 1000.0


def _group_name(group: Dict[str, Any]) -> str:
    label = str(group.get("Label", "")).strip()
    log_mode = str(group.get("LogMode", "")).strip()
    name = " ".join([p for p in [label, log_mode] if p])
    return name if name else "result"


def _extract_series(
    group: Dict[str, Any],
    metric_key: str,
    value_parser,
) -> Tuple[List[int], List[float], List[float], List[float]]:
    inflights: List[int] = []
    mins: List[float] = []
    meds: List[float] = []
    maxs: List[float] = []

    items = group.get("InFlights", [])
    if not isinstance(items, list):
        raise ValueError("Expected group['InFlights'] to be a list")

    for it in items:
        if not isinstance(it, dict):
            continue
        x = it.get("InFlight")
        stats = it.get(metric_key, {})
        if x is None or not isinstance(stats, dict):
            continue

        inflights.append(int(x))
        mins.append(value_parser(stats.get("min")))
        meds.append(value_parser(stats.get("median")))
        maxs.append(value_parser(stats.get("max")))

    order = sorted(range(len(inflights)), key=lambda i: inflights[i])
    inflights = [inflights[i] for i in order]
    mins = [mins[i] for i in order]
    meds = [meds[i] for i in order]
    maxs = [maxs[i] for i in order]

    return inflights, mins, meds, maxs


def _plot_min_med_max(
    groups: List[Dict[str, Any]],
    title: str,
    ylabel: str,
    metric_key: str,
    value_parser,
    out_path: str,
) -> None:
    # Optional dependency: matplotlib (headless-safe backend).
    try:
        import matplotlib  # type: ignore
    except ModuleNotFoundError as e:
        raise SystemExit(
            "Missing dependency: matplotlib\n"
            "Install it with: pip3 install matplotlib\n"
            f"Original error: {e}"
        )

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 6))

    n = max(1, len(groups))
    offsets = [0.0] if n == 1 else [0.12 * (i - (n - 1) / 2.0) for i in range(n)]
    all_inflights = set()

    for idx, group in enumerate(groups):
        name = _group_name(group)
        xs, mins, meds, maxs = _extract_series(group, metric_key, value_parser)
        if not xs:
            continue
        all_inflights.update(xs)

        x_plot = [x + offsets[idx] for x in xs]
        yerr_low = [m - lo for m, lo in zip(meds, mins)]
        yerr_high = [hi - m for hi, m in zip(maxs, meds)]

        # Draw thin median line first and capture its color
        (line,) = ax.plot(
            x_plot,
            meds,
            linewidth=1.0,
            alpha=0.6,
            zorder=1,
        )
        color = line.get_color()

        ax.errorbar(
            x_plot,
            meds,
            yerr=[yerr_low, yerr_high],
            fmt="o",
            linestyle="none",
            markersize=5,
            capsize=4,
            elinewidth=1.2,
            color=color,
            label=name,
            zorder=2,
        )

    ax.set_title(title)
    ax.set_xlabel("InFlight")
    ax.set_ylabel(ylabel)
    ax.grid(True, which="both", axis="both", linestyle="--", linewidth=0.6, alpha=0.6)

    # Linear scale: make sure inflight points are visible as ticks, but avoid
    # "weird" large gaps when values are sparse (e.g. 1,2,4) by using a small
    # integer range when it stays readable.
    if all_inflights:
        xs_sorted = sorted(all_inflights)
        xmin, xmax = xs_sorted[0], xs_sorted[-1]
        if (xmax - xmin) <= 12:
            ax.set_xticks(list(range(xmin, xmax + 1)))
        else:
            try:
                from matplotlib.ticker import MaxNLocator

                ax.xaxis.set_major_locator(MaxNLocator(integer=True, nbins=10))
            except Exception:
                ax.set_xticks(xs_sorted)

    if len(groups) > 1:
        ax.legend(loc="best")

    fig.tight_layout()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def _select_median_iops_run(inflight_item: Dict[str, Any]) -> Dict[str, Any] | None:
    """
    Pick the run whose IOPS is closest to the reported median IOPS.
    This is robust to rounding differences (median may be fractional while runs are ints).
    """
    iops_stats = inflight_item.get("IOPS", {})
    if not isinstance(iops_stats, dict):
        return None
    target = _parse_float(iops_stats.get("median"))
    if math.isnan(target):
        return None

    runs = inflight_item.get("Runs", [])
    if not isinstance(runs, list) or not runs:
        return None

    best_run = None
    best_diff = None
    for r in runs:
        if not isinstance(r, dict):
            continue
        diff = abs(_parse_float(r.get("IOPS")) - target)
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best_run = r
    return best_run


def _plot_latency_percentiles(
    groups: List[Dict[str, Any]],
    out_path: str,
    percentiles: List[str],
) -> None:
    try:
        import matplotlib  # type: ignore
    except ModuleNotFoundError as e:
        raise SystemExit(
            "Missing dependency: matplotlib\n"
            "Install it with: pip3 install matplotlib\n"
            f"Original error: {e}"
        )

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 6))
    all_inflights = set()

    for group in groups:
        name = _group_name(group)
        items = group.get("InFlights", [])
        if not isinstance(items, list):
            continue

        xs: List[int] = []
        ys_by_p: Dict[str, List[float]] = {p: [] for p in percentiles}

        for it in items:
            if not isinstance(it, dict):
                continue
            x = it.get("InFlight")
            if x is None:
                continue

            run = _select_median_iops_run(it)
            if run is None:
                continue

            xs.append(int(x))
            for p in percentiles:
                ys_by_p[p].append(_latency_to_us(run.get(p)))

        # sort by inflight
        order = sorted(range(len(xs)), key=lambda i: xs[i])
        xs = [xs[i] for i in order]
        for p in percentiles:
            ys_by_p[p] = [ys_by_p[p][i] for i in order]

        for p in percentiles:
            if not xs:
                continue
            ax.plot(xs, ys_by_p[p], marker="o", linewidth=1.5, markersize=4, label=f"{name} {p}")
        all_inflights.update(xs)

    ax.set_title("Latency percentiles vs InFlight (median-IOPS run)")
    ax.set_xlabel("InFlight")
    ax.set_ylabel("Latency (us)")
    ax.grid(True, which="both", axis="both", linestyle="--", linewidth=0.6, alpha=0.6)

    # Same x-tick policy as min/med/max plots (linear scale).
    if all_inflights:
        xs_sorted = sorted(all_inflights)
        xmin, xmax = xs_sorted[0], xs_sorted[-1]
        if (xmax - xmin) <= 12:
            ax.set_xticks(list(range(xmin, xmax + 1)))
        else:
            try:
                from matplotlib.ticker import MaxNLocator

                ax.xaxis.set_major_locator(MaxNLocator(integer=True, nbins=10))
            except Exception:
                ax.set_xticks(xs_sorted)

    ax.legend(loc="best", ncols=2)
    fig.tight_layout()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Plot YDB stress tool results (new InFlights format)."
    )
    p.add_argument(
        "input_json",
        nargs="+",
        help="Path(s) to resulting JSON file(s) (array of groups).",
    )
    p.add_argument(
        "prefix",
        help="Prefix for output files (e.g. /tmp/pdisk_write).",
    )
    p.add_argument(
        "--label",
        action="append",
        default=[],
        help="Optional label for each input file (repeat per file).",
    )
    args = p.parse_args()

    if len(args.label) > len(args.input_json):
        raise SystemExit(
            "--label provided more times than input files "
            f"({len(args.label)} > {len(args.input_json)})"
        )

    groups: List[Dict[str, Any]] = []
    test_types: List[str] = []

    for idx, path in enumerate(args.input_json):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, list):
            raise SystemExit(
                f"Input JSON must be an array (list) of result groups: {path}"
            )

        file_label = args.label[idx] if idx < len(args.label) else ""
        for g in data:
            if not isinstance(g, dict):
                continue
            if file_label:
                g = dict(g)
                g["Label"] = file_label
            groups.append(g)

        test_type = str(data[0].get("TestType", "")).strip() if data else ""
        if test_type:
            test_types.append(test_type)

    if not groups:
        raise SystemExit("No result groups found in input JSON.")

    out_speed = f"{args.prefix}_speed.png"
    out_iops = f"{args.prefix}_iops.png"
    out_latency = f"{args.prefix}_latency.png"

    title_suffix = ""
    if test_types:
        unique_types = sorted(set(test_types))
        if len(unique_types) == 1:
            title_suffix = f" ({unique_types[0]})"
        else:
            title_suffix = " (mixed)"

    _plot_min_med_max(
        groups=groups,
        title=f"Throughput vs InFlight{title_suffix}",
        ylabel="Speed (MB/s)",
        metric_key="Speed",
        value_parser=_bandwidth_to_mbs,
        out_path=out_speed,
    )

    _plot_min_med_max(
        groups=groups,
        title=f"IOPS vs InFlight{title_suffix}",
        ylabel="IOPS (kIOPS)",
        metric_key="IOPS",
        value_parser=_iops_to_kiops,
        out_path=out_iops,
    )

    print(out_speed)
    print(out_iops)
    _plot_latency_percentiles(
        groups=groups,
        out_path=out_latency,
        percentiles=["p50.00", "p90.00", "p95.00", "p99.00"],
    )
    print(out_latency)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

