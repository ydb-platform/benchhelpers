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

    total_latency = 0
    for job in json_result['jobs']:
        result['bw'] += job[test_type]['bw']
        result['iops'] += job[test_type]['iops']
        total_latency += job[test_type]['lat_ns']['mean']

    # average of averages
    result['bw'] = int(result['bw'] / 1024)
    result['iops'] = int(result['iops'] / 1000)
    result['latency'] = int(total_latency / len(json_result['jobs']) / 1000)

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

    write_latency_path = os.path.join(args.results_dir, "write_latency_test.json")
    result['write_latency'] = parse_result(write_latency_path, 'write')

    read_bandwidth_path = os.path.join(args.results_dir, "read_bandwidth_test.json")
    result['read_bandwidth'] = parse_result(read_bandwidth_path, 'read')

    read_iops_path = os.path.join(args.results_dir, "read_iops_test.json")
    result['read_iops'] = parse_result(read_iops_path, 'read')

    read_latency_path = os.path.join(args.results_dir, "read_latency_test.json")
    result['read_latency'] = parse_result(read_latency_path, 'read')

    print("RAW RESULTS")
    print("{:<20} {:>15} {:>20} {:>15}".format('Test', 'Bandwidth, MiB/s', 'IOPS (in Kilo)', "Latency, us"))
    for test_name,r in result.items():
        print("-" * 75)
        print("{:<20} {:>15} {:>20} {:>15}".format(
            test_name,
            r['bw'],
            r['iops'],
            r['latency'],))

    # we assume defaults
    summary = {
        "Random read 4k": {
            "bw": result['read_iops']['bw'],
            "iops": result['read_iops']['iops'],
            "latency": result['read_latency']['latency'],
        },

        "Random write 4k": {
            "bw": result['write_iops']['bw'],
            "iops": result['write_iops']['iops'],
            "latency": result['write_latency']['latency'],
        },

        "Read 1M": {
            "bw": result['read_bandwidth']['bw'],
            "iops": result['read_bandwidth']['iops'],
            "latency": result['read_bandwidth']['latency'],
        },

        "Write 1M": {
            "bw": result['write_bandwidth']['bw'],
            "iops": result['write_bandwidth']['iops'],
            "latency": result['write_bandwidth']['latency'],
        },
    }

    print("\n\nSUMMARY")

    print("{:<20} {:>15} {:>20} {:>15}".format('Operation', 'Bandwidth, MiB/s', 'IOPS (in Kilo)', "Latency, us"))
    for operation,result in summary.items():
        print("-" * 75)
        print("{:<20} {:>15} {:>20} {:>15}".format(
            operation,
            result['bw'],
            result['iops'],
            result['latency'],))


if __name__ == '__main__':
    main()
