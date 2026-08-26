#!/usr/bin/env python3

"""Normalize ydb_stress_tool JSON and attach benchmark metadata."""

import argparse
import json
import math
import re
import statistics
from collections import defaultdict
from typing import Any, Dict, Iterable, List


_SPEED_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s+MB/s\s*$")


def _number(value: Any) -> float:
    return float(str(value).strip())


def _speed_mbs(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    match = _SPEED_RE.match(str(value))
    if not match:
        raise ValueError(f"unsupported Speed value: {value!r}")
    return float(match.group(1))


def _stats(values: Iterable[float], unit: str = "") -> Dict[str, str]:
    samples = list(values)
    if not samples:
        return {}
    suffix = f" {unit}" if unit else ""
    return {
        "min": f"{min(samples):.1f}{suffix}",
        "median": f"{statistics.median(samples):.1f}{suffix}",
        "max": f"{max(samples):.1f}{suffix}",
        "stdev": f"{statistics.pstdev(samples):.1f}{suffix}",
    }


def _measurement_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Use aggregate rows for multi-device output and all rows otherwise."""
    aggregate = [row for row in rows if str(row.get("Device", "")) == "SUM"]
    return aggregate or rows


def normalize(raw: Any) -> Dict[str, Any]:
    """Return the structured InFlights shape for either stress-tool JSON mode."""
    if isinstance(raw, dict):
        if not isinstance(raw.get("InFlights"), list):
            raise ValueError("structured result has no InFlights array")
        return dict(raw)

    if not isinstance(raw, list):
        raise ValueError("stress-tool JSON must be an object or an array")

    rows_by_inflight: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for row in raw:
        if not isinstance(row, dict) or row.get("InFlight") is None:
            continue
        rows_by_inflight[int(row["InFlight"])].append(dict(row))
    if not rows_by_inflight:
        raise ValueError("single-run result has no rows with InFlight")

    inflights: List[Dict[str, Any]] = []
    for inflight, rows in sorted(rows_by_inflight.items()):
        measured = _measurement_rows(rows)
        speed_stats = _stats((_speed_mbs(row.get("Speed")) for row in measured), "MB/s")
        iops_stats = _stats((_number(row.get("IOPS")) for row in measured))
        inflights.append(
            {
                "InFlight": inflight,
                "Speed": speed_stats,
                "IOPS": iops_stats,
                "Runs": rows,
            }
        )

    return {"TestType": "DDiskLoad", "Params": {}, "InFlights": inflights}


def _finite_ratio(value: str) -> float:
    ratio = float(value)
    if not math.isfinite(ratio) or not 0.0 <= ratio <= 1.0:
        raise argparse.ArgumentTypeError("ratio must be a finite number in [0, 1]")
    return ratio


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--log-mode", required=True)
    parser.add_argument("--background-write-ratio", required=True, type=_finite_ratio)
    parser.add_argument("--background-write-size-kib", required=True, type=int)
    parser.add_argument("--areas-count", required=True, type=int)
    parser.add_argument("--init-inflight", required=True, type=int)
    parser.add_argument("--checksums", required=True, choices=["enabled", "disabled"])
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as stream:
        group = normalize(json.load(stream))

    params = group.get("Params")
    if not isinstance(params, dict):
        params = {}
    params.update(
        {
            "BackgroundWriteRatio": args.background_write_ratio,
            "BackgroundWriteSizeKiB": args.background_write_size_kib,
            "AreasCount": args.areas_count,
            "InitInFlight": args.init_inflight,
            "Checksums": args.checksums,
        }
    )
    group["Params"] = params
    group["Label"] = args.label
    group["LogMode"] = args.log_mode

    with open(args.output, "w", encoding="utf-8") as stream:
        json.dump(group, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
