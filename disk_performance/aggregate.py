#!/usr/bin/env python3

import argparse
import json
import os
import statistics
import sys


def parse_result(result_file, test_type):
    with open(result_file, 'r', encoding='utf-8') as f:
        raw = f.read()

    # fio may prepend warnings (e.g. setaffinity) before JSON output.
    first_obj = raw.find('{')
    last_obj = raw.rfind('}')
    if first_obj == -1 or last_obj == -1 or first_obj > last_obj:
        raise ValueError(f"{result_file}: JSON object boundaries not found")

    try:
        json_result = json.loads(raw[first_obj:last_obj + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"{result_file}: malformed JSON payload ({exc})") from exc

    result = {
        "bw": 0,
        "iops": 0,
    }

    # Collect percentile latencies
    percentile_keys = ['50.000000', '90.000000', '95.000000', '99.000000', '99.900000']
    percentile_sums = {k: 0 for k in percentile_keys}

    for job in json_result['jobs']:
        result['bw'] += job[test_type]['bw']
        result['iops'] += job[test_type]['iops']

        # Get percentile latencies from clat_ns (completion latency, in nanoseconds)
        clat_ns = job[test_type].get('clat_ns', {})
        percentile = clat_ns.get('percentile', {})
        for key in percentile_keys:
            percentile_sums[key] += percentile.get(key, 0)

    num_jobs = len(json_result['jobs'])
    result['bw'] = int(result['bw'] / 1024)
    result['iops'] = int(result['iops'] / 1000)

    # Percentiles are meaningful here only for single-job results.
    if num_jobs == 1:
        result['p50'] = int(percentile_sums['50.000000'] / 1000)
        result['p90'] = int(percentile_sums['90.000000'] / 1000)
        result['p95'] = int(percentile_sums['95.000000'] / 1000)
        result['p99'] = int(percentile_sums['99.000000'] / 1000)
        result['p99.9'] = int(percentile_sums['99.900000'] / 1000)
    else:
        result['p50'] = None
        result['p90'] = None
        result['p95'] = None
        result['p99'] = None
        result['p99.9'] = None

    return result


def collect_run_dirs(results_dir):
    run_dirs = []
    numeric_dir_names = []
    for name in os.listdir(results_dir):
        path = os.path.join(results_dir, name)
        if os.path.isdir(path) and name.isdigit():
            numeric_dir_names.append(name)

    for name in sorted(numeric_dir_names, key=int):
        run_dirs.append(os.path.join(results_dir, name))

    if run_dirs:
        return run_dirs
    return [results_dir]


def collect_result_samples(run_dirs, filename, test_type):
    samples = []
    for run_dir in run_dirs:
        result_path = os.path.join(run_dir, filename)
        if not os.path.exists(result_path):
            continue
        parsed = parse_result(result_path, test_type)
        samples.append(parsed)
    return samples


def metric_median(samples, key):
    values = [s[key] for s in samples if s.get(key) is not None]
    if not values:
        return None
    return int(statistics.median(values))


def make_bw_stats(samples):
    bw_values = [s["bw"] for s in samples if s.get("bw") is not None]
    if not bw_values:
        return {"min": None, "max": None, "median": None, "stddev": None}
    if len(bw_values) == 1:
        return {
            "min": bw_values[0],
            "max": bw_values[0],
            "median": bw_values[0],
            "stddev": 0.0,
        }
    return {
        "min": min(bw_values),
        "max": max(bw_values),
        "median": int(statistics.median(bw_values)),
        "stddev": statistics.pstdev(bw_values),
    }


def make_plot_slug(name):
    slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in name)
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug.strip("_")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("results_dir", help="results directory")
    parser.add_argument(
        "--plot",
        action="store_true",
        help="Generate per-latency-operation plots (p50/p90/p99/p99.9) by run",
    )
    parser.add_argument(
        "--prefix",
        default="latency_runs",
        help="Image filename prefix for --plot (default: latency_runs)",
    )
    args = parser.parse_args()

    if not os.path.isdir(args.results_dir):
        print(f"results dir does not exist: {args.results_dir}", file=sys.stderr)
        return 1

    run_dirs = collect_run_dirs(args.results_dir)

    throughput_ops = [
        ("Random read 4K", 256, "read_iops_test.json", "read"),
        ("Random write 4K", 256, "write_iops_test.json", "write"),
        ("Random read 8K", 256, "read_iops_test_8K.json", "read"),
        ("Random write 8K", 256, "write_iops_test_8K.json", "write"),
        ("Read 1M", 64, "read_bandwidth_test.json", "read"),
        ("Write 1M", 64, "write_bandwidth_test.json", "write"),
    ]

    latency_ops = [
        ("Random read 4K", 1, "read_latency_test.json", "read"),
        ("Random write 4K", 1, "write_latency_test.json", "write"),
        ("Random read 8K", 1, "read_latency_test_8K.json", "read"),
        ("Random write 8K", 1, "write_latency_test_8K.json", "write"),
    ]
    latency_op_order = [
        "Random read 4K",
        "Random read 8K",
        "Random write 4K",
        "Random write 8K",
    ]

    detailed_row_fmt = "{:<20} {:>8} {:>12} {:>12} {:>10} {:>10} {:>10} {:>10} {:>10}"
    latency_run_row_fmt = "{:>6} {:<20} {:>6} {:>12} {:>12} {:>10} {:>10} {:>10} {:>10} {:>10}"
    variance_row_fmt = "{:<20} {:>6} {:>10} {:>10} {:>12} {:>10}"

    def fmt_num(v):
        if v is None:
            return "n/a"
        if isinstance(v, float):
            return f"{v:.2f}"
        return str(v)

    def build_detailed_table(specs):
        rows = []
        for operation, qd, filename, test_type in specs:
            samples = collect_result_samples(run_dirs, filename, test_type)
            rows.append({
                "operation": operation,
                "qd": qd,
                "bw": metric_median(samples, "bw"),
                "iops": metric_median(samples, "iops"),
                "p50": metric_median(samples, "p50"),
                "p90": metric_median(samples, "p90"),
                "p95": metric_median(samples, "p95"),
                "p99": metric_median(samples, "p99"),
                "p99.9": metric_median(samples, "p99.9"),
            })
        return rows

    def build_variance_table(specs):
        rows = []
        for operation, qd, filename, test_type in specs:
            samples = collect_result_samples(run_dirs, filename, test_type)
            rows.append((operation, qd, make_bw_stats(samples)))
        return rows

    def build_latency_run_rows(specs):
        rows = []
        op_rank = {op: idx for idx, op in enumerate(latency_op_order)}
        ordered_specs = sorted(specs, key=lambda s: op_rank.get(s[0], len(op_rank)))

        for operation, qd, filename, test_type in ordered_specs:
            for idx, run_dir in enumerate(run_dirs, start=1):
                run_name = os.path.basename(run_dir)
                run_id = int(run_name) if run_name.isdigit() else idx
                result_path = os.path.join(run_dir, filename)
                if os.path.exists(result_path):
                    parsed = parse_result(result_path, test_type)
                    row = {
                        "run": run_id,
                        "operation": operation,
                        "qd": qd,
                        "bw": parsed["bw"],
                        "iops": parsed["iops"],
                        "p50": parsed["p50"],
                        "p90": parsed["p90"],
                        "p95": parsed["p95"],
                        "p99": parsed["p99"],
                        "p99.9": parsed["p99.9"],
                    }
                else:
                    row = {
                        "run": run_id,
                        "operation": operation,
                        "qd": qd,
                        "bw": None,
                        "iops": None,
                        "p50": None,
                        "p90": None,
                        "p95": None,
                        "p99": None,
                        "p99.9": None,
                    }
                rows.append(row)
        return rows

    def print_detailed_table(title, rows):
        header = detailed_row_fmt.format('Operation', 'QD', 'BW, MiB/s', 'IOPS (K)', 'p50 us', 'p90 us', 'p95 us', 'p99 us', 'p99.9 us')
        separator = "-" * len(header)
        print(title)
        print(header)
        for row in rows:
            print(separator)
            print(detailed_row_fmt.format(
                row["operation"],
                row["qd"],
                fmt_num(row["bw"]),
                fmt_num(row["iops"]),
                fmt_num(row["p50"]),
                fmt_num(row["p90"]),
                fmt_num(row["p95"]),
                fmt_num(row["p99"]),
                fmt_num(row["p99.9"]),
            ))

    def print_variance_table(title, rows):
        header = variance_row_fmt.format("Operation", "QD", "BW min", "BW max", "BW median", "BW stddev")
        separator = "-" * len(header)
        print(title)
        print(header)
        for operation, qd, stats in rows:
            print(separator)
            print(variance_row_fmt.format(
                operation,
                qd,
                fmt_num(stats["min"]),
                fmt_num(stats["max"]),
                fmt_num(stats["median"]),
                fmt_num(stats["stddev"]),
            ))

    def print_latency_runs_table(title, rows):
        header = latency_run_row_fmt.format("Run", "Operation", "QD", "BW, MiB/s", "IOPS (K)", "p50 us", "p90 us", "p95 us", "p99 us", "p99.9 us")
        separator = "-" * len(header)
        print(title)
        print(header)
        current_operation = None
        for row in rows:
            if row["operation"] != current_operation:
                if current_operation is not None:
                    print()
                print(f"# {row['operation']}")
            current_operation = row["operation"]
            print(separator)
            print(latency_run_row_fmt.format(
                row["run"],
                row["operation"],
                row["qd"],
                fmt_num(row["bw"]),
                fmt_num(row["iops"]),
                fmt_num(row["p50"]),
                fmt_num(row["p90"]),
                fmt_num(row["p95"]),
                fmt_num(row["p99"]),
                fmt_num(row["p99.9"]),
            ))

    def plot_latency_runs(rows):
        try:
            import matplotlib.pyplot as plt
        except ImportError as exc:
            raise RuntimeError("plotting requires matplotlib (pip install matplotlib)") from exc

        percentile_fields = [
            ("p50", "p50"),
            ("p90", "p90"),
            ("p99", "p99"),
            ("p99.9", "p99.9"),
        ]

        operation_order = [
            "Random read 4K",
            "Random write 4K",
            "Random read 8K",
            "Random write 8K",
        ]

        rows_by_operation = {}
        for row in rows:
            rows_by_operation.setdefault(row["operation"], []).append(row)

        generated_paths = []
        for operation in operation_order:
            op_rows = rows_by_operation.get(operation, [])
            if not op_rows:
                continue

            op_rows = sorted(op_rows, key=lambda r: int(r["run"]))
            x_vals = [int(r["run"]) for r in op_rows]

            fig, ax = plt.subplots(figsize=(10, 6))
            has_points = False
            for key, label in percentile_fields:
                y_vals = [r.get(key) for r in op_rows]
                if any(v is not None for v in y_vals):
                    has_points = True
                    ax.plot(x_vals, y_vals, marker="o", label=label)

            if not has_points:
                plt.close(fig)
                continue

            ax.set_xlabel("Run")
            ax.set_ylabel("Latency (us)")
            ax.set_title(f"{operation} latency by run")
            ax.grid(True, linestyle="--", alpha=0.4)
            ax.legend()
            fig.tight_layout()

            output_path = os.path.join(
                args.results_dir, f"{args.prefix}_{make_plot_slug(operation)}.png"
            )
            fig.savefig(output_path, dpi=120)
            plt.close(fig)
            generated_paths.append(output_path)

        return generated_paths

    latency_run_rows = build_latency_run_rows(latency_ops)
    print_detailed_table("THROUGHPUT SUMMARY", build_detailed_table(throughput_ops))
    print()
    print_detailed_table("LATENCY SUMMARY", build_detailed_table(latency_ops))
    print()
    print_latency_runs_table("LATENCY RUNS DETAIL", latency_run_rows)
    print()
    print_variance_table("THROUGHPUT RUNS VARIANCE", build_variance_table(throughput_ops))
    print()
    print_variance_table("LATENCY RUNS VARIANCE", build_variance_table(latency_ops))

    if args.plot:
        try:
            plot_paths = plot_latency_runs(latency_run_rows)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        if not plot_paths:
            print("plot: no data to plot", file=sys.stderr)
        else:
            for path in plot_paths:
                print(f"plot: {path}", file=sys.stderr)

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
