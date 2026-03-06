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
_LAT_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(us|µs|ms|ns|s)\s*$", flags=re.IGNORECASE)


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
    Convert latency values (us/ms/ns/s) to float microseconds.
    """
    if s is None:
        return float("nan")
    if isinstance(s, (int, float)):
        return float(s)
    raw = str(s).strip()
    if raw == "":
        return float("nan")
    m = _LAT_RE.match(raw)
    if not m:
        raise ValueError(f"Unrecognized latency value: {raw!r}")
    val = float(m.group(1))
    unit = m.group(2).lower()
    if unit in {"us", "µs"}:
        return val
    if unit == "ms":
        return val * 1000.0
    if unit == "ns":
        return val / 1000.0
    if unit == "s":
        return val * 1_000_000.0
    raise ValueError(f"Unsupported latency unit: {unit!r} (value={raw!r})")


def _percentile_aliases(percentile: str) -> List[str]:
    """
    Expand percentile aliases, e.g. "p50.00" -> ["p50.00", "50.0 perc", ...].
    """
    out: List[str] = [percentile]
    m = re.match(r"^\s*p?\s*([0-9]+(?:\.[0-9]+)?)\s*$", percentile.strip(), flags=re.IGNORECASE)
    if not m:
        return out
    p = float(m.group(1))
    out.extend(
        [
            f"p{p:.2f}",
            f"p{p:g}",
            f"{p:.2f} perc",
            f"{p:.1f} perc",
            f"{p:g} perc",
            f"{p:.2f}%",
            f"{p:.1f}%",
            f"{p:g}%",
        ]
    )
    return list(dict.fromkeys(out))


def _extract_latency_percentile_us(run: Dict[str, Any], percentile: str) -> float:
    """
    Read percentile latency from known run layouts, return us or NaN.
    """
    for key in _percentile_aliases(percentile):
        if key in run:
            try:
                return _latency_to_us(run.get(key))
            except ValueError:
                return float("nan")

    # Prefer full latency buckets from common containers.
    for container_key in ["Latency", "Latencies", "Percentiles"]:
        container = run.get(container_key)
        if not isinstance(container, dict):
            continue
        for key in _percentile_aliases(percentile):
            if key in container:
                try:
                    return _latency_to_us(container.get(key))
                except ValueError:
                    return float("nan")

    return float("nan")


def _iops_to_kiops(s: Any) -> float:
    """
    Convert IOPS value to kIOPS (thousands of IOPS).
    Input is typically a string number like "104301".
    """
    return _parse_float(s) / 1000.0


def _is_multi_device(group: Dict[str, Any]) -> bool:
    for it in group.get("InFlights", []):
        if not isinstance(it, dict):
            continue
        for run in it.get("Runs", []):
            if isinstance(run, dict) and "Device" in run:
                return True
    return False


def _get_device_ids(group: Dict[str, Any]) -> List[str]:
    devices: set = set()
    for it in group.get("InFlights", []):
        if not isinstance(it, dict):
            continue
        for run in it.get("Runs", []):
            if isinstance(run, dict):
                dev = run.get("Device")
                if dev is not None and str(dev) != "SUM":
                    devices.add(str(dev))
    return sorted(devices)


def _compute_stats(values: List[float]) -> Dict[str, float]:
    values = [v for v in values if not math.isnan(v)]
    if not values:
        return {}
    values.sort()
    n = len(values)
    mn = values[0]
    mx = values[-1]
    md = (values[n // 2 - 1] + values[n // 2]) / 2.0 if n % 2 == 0 else values[n // 2]
    mean = sum(values) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / n)
    return {"min": mn, "max": mx, "median": md, "stdev": sd}


def _split_multi_device_group(
    group: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Split a multi-device group into one synthetic group per device."""
    device_ids = _get_device_ids(group)
    per_device: List[Dict[str, Any]] = []
    for dev_id in device_ids:
        g = dict(group)
        label = str(group.get("Label", "")).strip()
        g["Label"] = f"{label} dev{dev_id}".strip()
        new_inflights = []
        for it in group.get("InFlights", []):
            if not isinstance(it, dict):
                continue
            dev_runs = [
                r for r in it.get("Runs", [])
                if isinstance(r, dict) and str(r.get("Device", "")) == dev_id
            ]
            if not dev_runs:
                continue
            new_it = dict(it)
            new_it["Runs"] = dev_runs
            speeds = [_bandwidth_to_mbs(r.get("Speed")) for r in dev_runs]
            st = _compute_stats(speeds)
            if st:
                new_it["Speed"] = {
                    k: f"{v:.1f} MB/s" if isinstance(v, float) else v
                    for k, v in st.items()
                }
            iops_vals = [_parse_float(r.get("IOPS")) for r in dev_runs]
            st = _compute_stats(iops_vals)
            if st:
                new_it["IOPS"] = {
                    k: f"{v:.1f}" if isinstance(v, float) else v
                    for k, v in st.items()
                }
            new_inflights.append(new_it)
        g["InFlights"] = new_inflights
        per_device.append(g)
    return per_device


def _filter_to_sum_runs(group: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only SUM runs; InFlight-level Speed/IOPS stats are already SUM-based."""
    g = dict(group)
    new_inflights = []
    for it in group.get("InFlights", []):
        if not isinstance(it, dict):
            continue
        sum_runs = [
            r for r in it.get("Runs", [])
            if isinstance(r, dict) and str(r.get("Device", "")) == "SUM"
        ]
        if not sum_runs:
            continue
        new_it = dict(it)
        new_it["Runs"] = sum_runs
        new_inflights.append(new_it)
    g["InFlights"] = new_inflights
    return g


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
    x_label: str = "QueueDepth (Inflight)",
    x_min: int | None = None,
    x_max: int | None = None,
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
        if x_min is not None or x_max is not None:
            clipped = [
                (x, lo, med, hi)
                for x, lo, med, hi in zip(xs, mins, meds, maxs)
                if (x_min is None or x >= x_min) and (x_max is None or x <= x_max)
            ]
            if not clipped:
                continue
            xs = [it[0] for it in clipped]
            mins = [it[1] for it in clipped]
            meds = [it[2] for it in clipped]
            maxs = [it[3] for it in clipped]
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
    ax.set_xlabel(x_label)
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

    if x_min is not None or x_max is not None:
        left = x_min if x_min is not None else 0
        right = x_max if x_max is not None else None
        ax.set_xlim(left=left, right=right)
    else:
        ax.set_xlim(left=0)
    ax.set_ylim(bottom=0)

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
    title: str = "Latency percentiles vs QueueDepth (Inflight) (median-IOPS run)",
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
                ys_by_p[p].append(_extract_latency_percentile_us(run, p))

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

    ax.set_title(title)
    ax.set_xlabel("QueueDepth (Inflight)")
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

    ax.set_xlim(left=0)
    ax.set_ylim(bottom=0)

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
        "paths",
        nargs="+",
        help=(
            "Path(s) to resulting JSON file(s) (array of groups). "
            "For backward compatibility, the final positional value may be a prefix."
        ),
    )
    p.add_argument(
        "--prefix",
        default="",
        help=(
            "Prefix for output files (e.g. /tmp/pdisk_write). "
            "If omitted, a default prefix is derived from the first input file."
        ),
    )
    p.add_argument(
        "--label",
        action="append",
        default=[],
        help="Optional label for each input file (repeat per file).",
    )
    args = p.parse_args()

    input_json = list(args.paths)
    prefix = str(args.prefix).strip()

    # Backward compatibility: if --prefix is not set and last positional token
    # does not look like a JSON path, treat it as legacy positional prefix.
    if not prefix and len(input_json) >= 2 and not str(input_json[-1]).lower().endswith(".json"):
        prefix = str(input_json.pop()).strip()

    if not prefix:
        first_base = os.path.splitext(os.path.basename(input_json[0]))[0]
        prefix = os.path.join(".", first_base)
        print(f"[info] --prefix not provided; using default prefix: {prefix}")

    if len(args.label) > len(input_json):
        raise SystemExit(
            "--label provided more times than input files "
            f"({len(args.label)} > {len(input_json)})"
        )

    groups: List[Dict[str, Any]] = []
    test_types: List[str] = []

    for idx, path in enumerate(input_json):
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

    multi_count = sum(1 for g in groups if _is_multi_device(g))
    single_count = len(groups) - multi_count

    if len(input_json) == 1 and multi_count > 0:
        # Single input file with multi-device data: plot each device as a series
        plot_groups: List[Dict[str, Any]] = []
        for g in groups:
            if _is_multi_device(g):
                plot_groups.extend(_split_multi_device_group(g))
            else:
                plot_groups.append(g)
        groups = plot_groups
    elif len(input_json) > 1:
        if multi_count > 0 and single_count > 0:
            raise SystemExit(
                "Cannot mix single-device and multi-device result files. "
                "All inputs must be either single-device or all multi-device."
            )
        if multi_count > 0:
            # Multiple inputs, all multi-device: plot only SUM/aggregate
            groups = [_filter_to_sum_runs(g) for g in groups]

    out_speed = f"{prefix}_speed.png"
    out_speed_qd_1_8 = f"{prefix}_speed_qd1_8.png"
    out_iops = f"{prefix}_iops.png"
    out_iops_qd_1_8 = f"{prefix}_iops_qd1_8.png"
    out_latency = f"{prefix}_latency.png"

    title_suffix = ""
    if test_types:
        unique_types = sorted(set(test_types))
        if len(unique_types) == 1:
            title_suffix = f" ({unique_types[0]})"
        else:
            title_suffix = " (mixed)"

    _plot_min_med_max(
        groups=groups,
        title=f"Throughput vs QueueDepth (Inflight){title_suffix}",
        ylabel="Speed (MB/s)",
        metric_key="Speed",
        value_parser=_bandwidth_to_mbs,
        out_path=out_speed,
    )
    _plot_min_med_max(
        groups=groups,
        title=f"Throughput vs QueueDepth (Inflight){title_suffix} [1..8]",
        ylabel="Speed (MB/s)",
        metric_key="Speed",
        value_parser=_bandwidth_to_mbs,
        out_path=out_speed_qd_1_8,
        x_min=1,
        x_max=8,
    )

    _plot_min_med_max(
        groups=groups,
        title=f"IOPS vs QueueDepth (Inflight){title_suffix}",
        ylabel="IOPS (kIOPS)",
        metric_key="IOPS",
        value_parser=_iops_to_kiops,
        out_path=out_iops,
    )
    _plot_min_med_max(
        groups=groups,
        title=f"IOPS vs QueueDepth (Inflight){title_suffix} [1..8]",
        ylabel="IOPS (kIOPS)",
        metric_key="IOPS",
        value_parser=_iops_to_kiops,
        out_path=out_iops_qd_1_8,
        x_min=1,
        x_max=8,
    )

    print(out_speed)
    print(out_speed_qd_1_8)
    print(out_iops)
    print(out_iops_qd_1_8)
    if len(groups) >= 3:
        latency_percentiles = ["p50.00", "p90.00", "p99.00"]
        for p in latency_percentiles:
            p_slug = p.lower().replace(".", "")
            out_p = f"{prefix}_latency_{p_slug}.png"
            _plot_latency_percentiles(
                groups=groups,
                out_path=out_p,
                percentiles=[p],
                title=f"Latency {p} vs QueueDepth (Inflight) (median-IOPS run){title_suffix}",
            )
            print(out_p)
    else:
        _plot_latency_percentiles(
            groups=groups,
            out_path=out_latency,
            percentiles=["p50.00", "p90.00", "p95.00", "p99.00"],
            title=f"Latency percentiles vs QueueDepth (Inflight) (median-IOPS run){title_suffix}",
        )
        print(out_latency)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

