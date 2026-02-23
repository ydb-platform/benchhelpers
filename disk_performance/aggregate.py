#!/usr/bin/env python3

import argparse
import json
import os
import statistics


def parse_result(result_file, test_type):
    with open(result_file, 'r') as f:
        json_result = json.load(f)

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("results_dir", help="results directory")
    args = parser.parse_args()

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

    detailed_row_fmt = "{:<20} {:>8} {:>12} {:>12} {:>10} {:>10} {:>10} {:>10} {:>10}"
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

    print_detailed_table("THROUGHPUT SUMMARY", build_detailed_table(throughput_ops))
    print()
    print_detailed_table("LATENCY SUMMARY", build_detailed_table(latency_ops))
    print()
    print_variance_table("THROUGHPUT RUNS VARIANCE", build_variance_table(throughput_ops))
    print()
    print_variance_table("LATENCY RUNS VARIANCE", build_variance_table(latency_ops))


if __name__ == '__main__':
    main()
