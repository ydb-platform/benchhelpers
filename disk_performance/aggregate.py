#!/usr/bin/env python3

import argparse
import json
import os
import sys


def parse_result(result_file, test_type):
    with open(result_file, 'r') as f:
        json_result = json.load(f)

    result = {
        "bw": 0,
        "iops": 0,
    }

    # Collect percentile latencies from all jobs
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

    # Average percentiles across jobs, convert ns to us
    result['p50'] = int(percentile_sums['50.000000'] / num_jobs / 1000) if num_jobs > 0 else 0
    result['p90'] = int(percentile_sums['90.000000'] / num_jobs / 1000) if num_jobs > 0 else 0
    result['p95'] = int(percentile_sums['95.000000'] / num_jobs / 1000) if num_jobs > 0 else 0
    result['p99'] = int(percentile_sums['99.000000'] / num_jobs / 1000) if num_jobs > 0 else 0
    result['p99.9'] = int(percentile_sums['99.900000'] / num_jobs / 1000) if num_jobs > 0 else 0

    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("results_dir", help="results directory")

    args = parser.parse_args()

    result = {}

    write_bandwidth_path = os.path.join(args.results_dir, "write_bandwidth_test.json")
    result['write_bandwidth'] = parse_result(write_bandwidth_path, 'write')

    write_iops_path = os.path.join(args.results_dir, "write_iops_test.json")
    result['write_iops'] = parse_result(write_iops_path, 'write')

    write_iops_path = os.path.join(args.results_dir, "write_iops_test_8K.json")
    result['write_iops_8K'] = parse_result(write_iops_path, 'write')

    write_latency_path = os.path.join(args.results_dir, "write_latency_test.json")
    if os.path.exists(write_latency_path):
        result['write_latency'] = parse_result(write_latency_path, 'write')

    write_latency_path = os.path.join(args.results_dir, "write_latency_test_8K.json")
    if os.path.exists(write_latency_path):
        result['write_latency_8K'] = parse_result(write_latency_path, 'write')

    read_bandwidth_path = os.path.join(args.results_dir, "read_bandwidth_test.json")
    result['read_bandwidth'] = parse_result(read_bandwidth_path, 'read')

    read_iops_path = os.path.join(args.results_dir, "read_iops_test.json")
    result['read_iops'] = parse_result(read_iops_path, 'read')

    read_iops_path = os.path.join(args.results_dir, "read_iops_test_8K.json")
    result['read_iops_8K'] = parse_result(read_iops_path, 'read')

    read_latency_path = os.path.join(args.results_dir, "read_latency_test.json")
    if os.path.exists(read_latency_path):
        result['read_latency'] = parse_result(read_latency_path, 'read')

    read_latency_path = os.path.join(args.results_dir, "read_latency_test_8K.json")
    if os.path.exists(read_latency_path):
        result['read_latency_8K'] = parse_result(read_latency_path, 'read')

    row_fmt = "{:<20} {:>12} {:>12} {:>10} {:>10} {:>10} {:>10} {:>10}"
    header = row_fmt.format('Test', 'BW, MiB/s', 'IOPS (K)', 'p50 us', 'p90 us', 'p95 us', 'p99 us', 'p99.9 us')
    separator = "-" * len(header)

    print("RAW RESULTS")
    print(header)
    for test_name,r in result.items():
        print(separator)
        print(row_fmt.format(
            test_name,
            r['bw'],
            r['iops'],
            r['p50'],
            r['p90'],
            r['p95'],
            r['p99'],
            r['p99.9'],))

    # Helper to get percentiles from latency test or None
    def get_latency_percentiles(lat_result):
        if lat_result is None:
            return {'p50': None, 'p90': None, 'p95': None, 'p99': None, 'p99.9': None}
        return {
            'p50': lat_result['p50'],
            'p90': lat_result['p90'],
            'p95': lat_result['p95'],
            'p99': lat_result['p99'],
            'p99.9': lat_result['p99.9'],
        }

    # we assume defaults
    summary = {
        "Random read 4K": {
            "bw": result['read_iops']['bw'],
            "iops": result['read_iops']['iops'],
            **get_latency_percentiles(result.get('read_latency')),
        },

        "Random write 4K": {
            "bw": result['write_iops']['bw'],
            "iops": result['write_iops']['iops'],
            **get_latency_percentiles(result.get('write_latency')),
        },

        "Random read 8K": {
            "bw": result['read_iops_8K']['bw'],
            "iops": result['read_iops_8K']['iops'],
            **get_latency_percentiles(result.get('read_latency_8K')),
        },

        "Random write 8K": {
            "bw": result['write_iops_8K']['bw'],
            "iops": result['write_iops_8K']['iops'],
            **get_latency_percentiles(result.get('write_latency_8K')),
        },

        "Read 1M": {
            "bw": result['read_bandwidth']['bw'],
            "iops": result['read_bandwidth']['iops'],
            **get_latency_percentiles(result.get('read_bandwidth')),
        },

        "Write 1M": {
            "bw": result['write_bandwidth']['bw'],
            "iops": result['write_bandwidth']['iops'],
            **get_latency_percentiles(result.get('write_bandwidth')),
        },
    }

    print("\n\nSUMMARY")

    def fmt_val(v):
        return str(v) if v is not None else 'n/a'

    summary_header = row_fmt.format('Operation', 'BW, MiB/s', 'IOPS (K)', 'p50 us', 'p90 us', 'p95 us', 'p99 us', 'p99.9 us')
    print(summary_header)
    for operation,r in summary.items():
        print(separator)
        print(row_fmt.format(
            operation,
            r['bw'],
            r['iops'],
            fmt_val(r['p50']),
            fmt_val(r['p90']),
            fmt_val(r['p95']),
            fmt_val(r['p99']),
            fmt_val(r['p99.9']),))


if __name__ == '__main__':
    main()
