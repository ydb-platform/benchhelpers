#!/usr/bin/env python3

import argparse
import json
import math
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple


def _parse_float(s: Any) -> float:
    if s is None:
        return float("nan")
    if isinstance(s, (int, float)):
        return float(s)
    raw = str(s).strip()
    if raw == "":
        return float("nan")
    return float(raw)


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
    """
    return _parse_float(s) / 1000.0


def _group_name(group: Dict[str, Any]) -> str:
    label = str(group.get("Label", "")).strip()
    log_mode = str(group.get("LogMode", "")).strip()
    name = " ".join([p for p in [label, log_mode] if p])
    return name if name else "result"


def _fmt_num(v: float, decimals: int = 1) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    if abs(v) >= 10_000:
        # avoid ugly "104300.9" -> "104301"
        return f"{v:.0f}"
    return f"{v:.{decimals}f}"


def _extract_min_med_max(
    group: Dict[str, Any],
    metric_key: str,
    value_parser,
) -> List[Tuple[int, float, float, float, float]]:
    items = group.get("InFlights", [])
    if not isinstance(items, list):
        return []

    out: List[Tuple[int, float, float, float, float]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        inflight = it.get("InFlight")
        stats = it.get(metric_key, {})
        if inflight is None or not isinstance(stats, dict):
            continue
        stddev_raw = stats.get("stdev")
        if stddev_raw is None:
            stddev_raw = stats.get("stddev")
        out.append(
            (
                int(inflight),
                value_parser(stats.get("min")),
                value_parser(stats.get("median")),
                value_parser(stats.get("max")),
                value_parser(stddev_raw),
            )
        )
    out.sort(key=lambda t: t[0])
    return out


def _select_median_iops_run(inflight_item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Pick the run whose IOPS is closest to the reported median IOPS.
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


def _extract_latency_percentiles(
    group: Dict[str, Any], percentiles: Sequence[str]
) -> List[Tuple[int, List[float]]]:
    items = group.get("InFlights", [])
    if not isinstance(items, list):
        return []

    out: List[Tuple[int, List[float]]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        inflight = it.get("InFlight")
        if inflight is None:
            continue
        run = _select_median_iops_run(it)
        if run is None:
            continue
        vals = [_latency_to_us(run.get(p)) for p in percentiles]
        out.append((int(inflight), vals))

    out.sort(key=lambda t: t[0])
    return out


def _render_table(headers: List[str], rows: List[List[str]]) -> str:
    cols = len(headers)
    widths = [len(h) for h in headers]
    for r in rows:
        for i in range(cols):
            widths[i] = max(widths[i], len(r[i]))

    def fmt_row(r: List[str]) -> str:
        return "  ".join(r[i].rjust(widths[i]) if i else r[i].ljust(widths[i]) for i in range(cols))

    sep = "  ".join("-" * w for w in widths)
    out_lines = [fmt_row(headers), sep]
    out_lines.extend(fmt_row(r) for r in rows)
    return "\n".join(out_lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Print YDB stress tool results as human-readable tables (new InFlights format)."
    )
    ap.add_argument("input_json", help="Path to resulting JSON file (array of groups).")
    args = ap.parse_args()

    with open(args.input_json, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise SystemExit("Input JSON must be an array (list) of result groups.")
    groups: List[Dict[str, Any]] = [g for g in data if isinstance(g, dict)]
    if not groups:
        raise SystemExit("No result groups found in input JSON.")

    test_type = str(groups[0].get("TestType", "")).strip()
    title_suffix = f" ({test_type})" if test_type else ""

    # 1) Speed table
    speed_sections: List[str] = [f"Throughput vs InFlight{title_suffix}", ""]
    for g in groups:
        rows_data = _extract_min_med_max(g, "Speed", _bandwidth_to_mbs)
        rows = [
            [str(inf), _fmt_num(mn, 1), _fmt_num(md, 1), _fmt_num(mx, 1), _fmt_num(sd, 1)]
            for inf, mn, md, mx, sd in rows_data
        ]
        speed_sections.append(f"[{_group_name(g)}]  Speed (MB/s): min / median / max / stddev")
        speed_sections.append(_render_table(["InFlight", "min", "median", "max", "stddev"], rows))
        speed_sections.append("")
    speed_txt = "\n".join(speed_sections).rstrip() + "\n"

    # 2) IOPS table
    iops_sections: List[str] = [f"IOPS vs InFlight{title_suffix}", ""]
    for g in groups:
        rows_data = _extract_min_med_max(g, "IOPS", _iops_to_kiops)
        rows = [
            [str(inf), _fmt_num(mn, 1), _fmt_num(md, 1), _fmt_num(mx, 1), _fmt_num(sd, 1)]
            for inf, mn, md, mx, sd in rows_data
        ]
        iops_sections.append(f"[{_group_name(g)}]  IOPS (kIOPS): min / median / max / stddev")
        iops_sections.append(_render_table(["InFlight", "min", "median", "max", "stddev"], rows))
        iops_sections.append("")
    iops_txt = "\n".join(iops_sections).rstrip() + "\n"

    # 3) Latency percentiles table (median-IOPS run)
    ps = ["p50.00", "p90.00", "p95.00", "p99.00"]
    lat_sections: List[str] = [
        f"Latency percentiles vs InFlight{title_suffix}",
        "NOTE: for each InFlight we pick the run whose IOPS is closest to IOPS.median, then read percentiles from that run.",
        "",
    ]
    for g in groups:
        rows_data = _extract_latency_percentiles(g, ps)
        rows = [
            [str(inf)] + [_fmt_num(v, 1) for v in vals]
            for inf, vals in rows_data
        ]
        lat_sections.append(f"[{_group_name(g)}]  Latency (us) from median-IOPS run")
        lat_sections.append(_render_table(["InFlight"] + ps, rows))
        lat_sections.append("")
    lat_txt = "\n".join(lat_sections).rstrip() + "\n"

    # Print to stdout (human-readable).
    print(speed_txt)
    print(iops_txt)
    print(lat_txt)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

