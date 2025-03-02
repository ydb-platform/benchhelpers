#!/usr/bin/env python3

import argparse
import bisect
import collections
import datetime
import json
import os
import re
import sys

class Aggregator:

    class Histogram:
        def __init__(self, bucketlist):
            self.bucketlist = sorted(bucketlist)
            self.buckets = [0] * (len(self.bucketlist) + 1)

        def add(self, value):
            i = bisect.bisect_right(self.bucketlist, value)
            self.buckets[i] += 1

        def add_bucket(self, bucket_index, value):
            self.buckets[bucket_index] += value

        def count(self, value):
            i = bisect.bisect_right(self.bucketlist, value)
            return self.buckets[i]

        def total_count(self):
            return sum(self.buckets)

        def percentile(self, percentile):
            total = self.total_count()
            cumulative = 0
            for i, count in enumerate(self.buckets):
                cumulative += count
                if cumulative / total >= percentile / 100.0:
                    return self.bucketlist[i - 1] if i != 0 else "<{}".format(self.bucketlist[0])
            return ">= {}".format(self.bucketlist[-1])

        def __repr__(self):
            repr_str = ""
            for i in range(len(self.buckets)):
                if i == 0:
                    repr_str += f"<{self.bucketlist[0]}: {self.buckets[i]}, "
                elif i == len(self.buckets) - 1:
                    repr_str += f">={self.bucketlist[i-1]}: {self.buckets[i]}"
                else:
                    repr_str += f"{self.bucketlist[i-1]}-{self.bucketlist[i]}: {self.buckets[i]}, "
            return repr_str

        def __len__(self):
            return self.total_count()

    class TransactionStats:
        def __init__(self):
            self.new_orders = 0
            self.paymens = 0
            self.order_status = 0
            self.delivery = 0
            self.stock_level = 0
            self.total = 0

    class Result:
        def __init__(self):
            self.name = ""
            self.measure_start_ts = 0 # seconds since epoch
            self.time_seconds = 0
            self.warehouses = 0
            self.new_orders = 0
            self.tpmc = 0
            self.efficiency = 0
            self.throughput = 0
            self.goodput = 0

            self.completed_new_orders = 0
            self.completed_paymens = 0

            # completed, aborted, rejected_server_retry, unexpected, unknown -> TransactionStats
            self.stats = {}

        def __str__(self):
            return f"""\
Result: {self.name}
  Time: {self.time_seconds} seconds
  Start measure: {self.measure_start_ts}
  Warehouses: {self.warehouses}
  New orders: {self.new_orders}
  tpmC: {self.tpmc}
  Efficiency: {self.efficiency}
  Throughput: {self.throughput}
  Goodput: {self.goodput}
"""
        def to_json(self):
            return {
                "name": self.name,
                "time_seconds": self.time_seconds,
                "measure_start_ts": self.measure_start_ts,
                "warehouses": self.warehouses,
                "new_orders": self.new_orders,
                "tpmc": self.tpmc,
                "efficiency": self.efficiency,
                "throughput": self.throughput,
                "goodput": self.goodput,
                "completed_new_orders": self.completed_new_orders,
                "completed_paymens": self.completed_paymens,
                "stats": self.stats,
            }

    def run(self, args):
        self.scale_re = re.compile(r"^Scale Factor:\s*(\d+(\.\d+)?)$")
        self.start_measure_re = re.compile(r"^\[INFO\s*\] (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) \[main\].*Warmup complete, starting measurements.$")
        self.results_line_re = re.compile(r"^================RESULTS================")
        self.results_entry = re.compile(r".*\|\s*(\d+(\.\d+)?)%?\s*$")
        self.rate_re = re.compile(r"^(?:Rate limited|reqs/s).*= (\d+(\.\d+)?) requests/sec \(throughput\), (\d+(\.\d+)?) requests/sec \(goodput\)$")

        host_dirs = []
        for name in os.listdir(args.results_dir):
            if os.path.isdir(os.path.join(args.results_dir, name)):
                host_dirs.append(name)

        run_results = []
        for host_dir in host_dirs:
            hostname = host_dir.split(".")[0]
            full_path = os.path.join(args.results_dir, host_dir)
            for name in os.listdir(full_path):
                if name.endswith(".run.log"):
                    file = os.path.join(full_path, name)
                    with open(file, "r") as f:
                        try:
                            r = self.process_run_file(args, f)
                            r.name = hostname + "." + name[:-len(".run.log")]
                            run_results.append(r)
                        except Exception as e:
                            print(f"Error processing file {file}: {e}", file=sys.stderr)
                            raise e

        sorted(run_results, key=lambda r: r.name)

        results_by_start_ts = run_results
        sorted(results_by_start_ts, key=lambda r: r.measure_start_ts)

        total_result = Aggregator.Result()
        total_result.name = "Total"
        total_result.time_seconds = run_results[0].time_seconds
        total_result.measure_start_ts = run_results[0].measure_start_ts
        for r in run_results:
            total_result.warehouses += r.warehouses
            total_result.new_orders += r.new_orders
            total_result.tpmc += r.tpmc
            total_result.throughput += r.throughput
            total_result.goodput += r.goodput

        total_result.efficiency = total_result.tpmc * 100 / total_result.warehouses / 12.86
        total_result.efficiency = round(total_result.efficiency, 2)

        if len(results_by_start_ts) > 1:
            min_start = results_by_start_ts[0].measure_start_ts
            max_start = results_by_start_ts[-1].measure_start_ts
            start_delta = max_start - min_start
            print(f"Delta between earliest and latest measurements start: {start_delta} seconds")

        transactions_dict = {}

        transactions_stats_dict = collections.defaultdict(lambda: collections.defaultdict(int))

        for host_dir in host_dirs:
            full_path = os.path.join(args.results_dir, host_dir)
            for name in os.listdir(full_path):
                if name.startswith("results"):
                    rdir = os.path.join(full_path, name)
                    for fname in os.listdir(rdir):
                        if fname.endswith(".raw.json"):
                            # new version of benchbase
                            file = os.path.join(rdir, fname)
                            with open(file, "r") as f:
                                self.process_raw_json(f, transactions_dict, transactions_stats_dict, total_result.measure_start_ts)
                        elif fname.endswith(".raw.csv"):
                            # previous version of benchbase
                            file = os.path.join(rdir, fname)
                            with open(file, "r") as f:
                                self.process_raw_csv(f, transactions_dict, transactions_stats_dict, total_result.measure_start_ts)
                            break

        for r in run_results:
            print(r)
        print(total_result)

        transactions_json = {}
        for transaction_name, stats in transactions_stats_dict.items():
            ok_count = stats['OK']
            failed_count = stats['FAILED']
            total_requests = stats['OK'] + stats['FAILED']
            failed_percent_str = ""
            if failed_count:
                failed_percent = round(failed_count * 100 / total_requests, 2)
                failed_percent_str = f" ({failed_percent}%)"

            transactions_json[transaction_name] = {
                "ok_count": ok_count,
                "failed_count": failed_count,
                "percentiles": {},
            }
            print(f"{transaction_name}: OK: {ok_count}, FAILED: {failed_count}{failed_percent_str}")

        for transaction_name, histogram in transactions_dict.items():
            print(f"{transaction_name}:")
            if len(histogram) == 0:
                print("  No data")
                continue
            for percentile in [50, 90, 95, 99, 99.9]:
                transactions_json[transaction_name]["percentiles"][percentile] = histogram.percentile(percentile)
                print(f"  {percentile}%: {histogram.percentile(percentile)} ms")

        json_result = {
            "summary": total_result.to_json(),
            "instance_results": [r.to_json() for r in run_results],
            "transactions": transactions_json,
        }

        result_file = os.path.join(args.results_dir, "result.json")
        with open(result_file, "w") as f:
            json.dump(json_result, f, indent=4)

        print(f"Result saved to {result_file}")
        print("\n*These results are not officially recognized TPC results and are not comparable with other TPC-C test results published on the TPC website")

    def process_raw_json(self, file, transactions_dict, transactions_stats_dict, start_ts):
        data = json.loads(file.read())
        for transaction_name, transaction_data in data.items():
            if transaction_name == "Invalid":
                continue
            transactions_stats_dict[transaction_name]["OK"] += transaction_data["SuccessCount"]
            transactions_stats_dict[transaction_name]["FAILED"] += transaction_data["FailureCount"]

            if transaction_name not in transactions_dict:
                buckets = transaction_data["LatencySuccessHistogramMs"]["bucketlist"]
                transactions_dict[transaction_name] = Aggregator.Histogram(buckets)

            for bucket_index, bucket_count in enumerate(transaction_data["LatencySuccessHistogramMs"]["buckets"]):
                transactions_dict[transaction_name].add_bucket(bucket_index, bucket_count)

    def process_raw_csv(self, file, transactions_dict, transactions_stats_dict, start_ts):
        file.readline() # skip header
        for line in file:
            fields = line.split(",")
            transaction_name = fields[1]
            transaction_ts = fields[2]
            transaction_latency_ms = round(int(fields[3]) / 1000)

            if float(transaction_ts) < start_ts:
                continue

            if fields[-1].strip() == "true":
                transactions_dict[transaction_name].add(transaction_latency_ms)
                transactions_stats_dict[transaction_name]["OK"] += 1
            else:
                transactions_stats_dict[transaction_name]["FAILED"] += 1

    def process_run_file(self, args, file):
        result = Aggregator.Result()

        for line in file:
            m = self.scale_re.match(line)
            if m:
                result.warehouses = int(float(m.group(1)))
                break

        for line in file:
            m = self.start_measure_re.match(line)
            if m:
                datetime_str = m.group(1)
                dt = datetime.datetime.strptime(datetime_str, "%Y-%m-%d %H:%M:%S,%f")
                timestamp = dt.timestamp()
                result.measure_start_ts = round(timestamp)
                break

        for line in file:
            if self.results_line_re.match(line):
                break

        line = file.readline()
        m = self.results_entry.match(line)
        if not m:
            raise Exception("Invalid results line1: {}".format(line))
        result.time_seconds = int(float(m.group(1)))

        line = file.readline()
        m = self.results_entry.match(line)
        if not m:
            raise Exception("Invalid results line2: {}".format(line))
        result.new_orders = int(m.group(1))

        line = file.readline()
        m = self.results_entry.match(line)
        if not m:
            raise Exception("Invalid results line3: {}".format(line))
        result.tpmc = int(float(m.group(1)))

        line = file.readline()
        m = self.results_entry.match(line)
        if not m:
            raise Exception("Invalid results line4: {}".format(line))
        result.efficiency = float(m.group(1))

        line = file.readline()
        m = self.rate_re.match(line)
        if not m:
            raise Exception("Invalid results line5: {}".format(line))
        result.throughput = int(float(m.group(1)))
        result.goodput = int(float(m.group(3)))

        return result

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-e", "--endpoint", help="YDB endpoint")
    parser.add_argument("-d", "--database", help="YDB database")
    parser.add_argument("--token", help="YDB token file", required=False)
    parser.add_argument("-w", "--warehouses", dest="warehouse_count",
                        type=int, default=10,
                        help="Number of warehouses")
    parser.add_argument("-n", "--nodes", dest="node_count",
                        type=int, default=1,
                        help="Number of TPCC nodes")

    subparsers = parser.add_subparsers(dest='action', help="Action to perform")

    aggregate_parser = subparsers.add_parser('aggregate')
    aggregate_parser.add_argument('results_dir', help="Directory with results")
    aggregate_parser.set_defaults(func=Aggregator().run)

    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
