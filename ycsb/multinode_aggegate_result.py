#!/usr/bin/python3
#
# When YCSB runs on multiple nodes via pssh, this script parses the log and aggregates a result

import argparse
import concurrent
import math
import re
import ydb

from datetime import datetime, timezone


def format_number(num, decimal_places=1):
    if num >= 1000:
        factor = 10 ** decimal_places
        rounded_up = math.ceil(num * factor / 1000) / factor
        if rounded_up.is_integer():
            return f"{int(rounded_up)}K"
        return f"{rounded_up:.{decimal_places}f}K"

    return str(int(num))


workload_p = re.compile(".*: ([a-zA-Z]+) workload (.) from ([1-9]+) ycsb instances started on ([0-9]+)$")
workload_done_p = re.compile(".*: done$")

host_p = re.compile(r"\[\d\] \d\d:\d\d:\d\d \[SUCCESS\] (?P<host>.*)")


# YCSB result: either of one instance (host) or total (aggregated from all hosts)
class WorkloadResult:
    def __init__(self):
        self.host = "" # host or "total"
        self.workload = ""
        self.distribution = ""
        self.ycsb_instances_count = 0

        # time in UTC
        self.when = None

        self.per_host = [] # in case of total only

        self.throughput = 0
        self.latency = 0

        self.oks = 0
        self.errors = 0
        self.not_found = 0
        self.max_time_ms = 0
        self.min_time_ms = 0

    def __str__(self):
        return "{} {} {} workload {} with throughput {} and time {}".format(
            self.when,
            self.host,
            self.distribution,
            self.workload,
            self.throughput,
            self.max_time_ms
        )


# calc aggregation result (sum RPS, max latency between hosts)
def calc_aggregated_result(results):
    total = WorkloadResult()
    total.host = "total"
    if results:
        total.min_time_ms = results[0].min_time_ms
    for r in results:
        total.throughput += r.throughput
        total.latency = max(total.latency, r.latency)
        total.oks += r.oks
        total.errors += r.errors
        total.not_found += r.not_found
        total.max_time_ms = max(total.max_time_ms, r.max_time_ms)
        total.min_time_ms = min(total.min_time_ms, r.min_time_ms)
    return total


# parse output of run_workloads.sh (i.e. pssh/ycsb output)
class Parser:
    def __init__(self):
        # seconds since epoch -> WorkloadResult
        self.results = {}

    def add_workload(self, workload, distribution, when, ycsb_instances_count, workload_results):
        if not workload:
            return
        total = calc_aggregated_result(workload_results)
        total.workload = workload
        total.when = when
        total.distribution = distribution
        total.ycsb_instances_count = ycsb_instances_count
        total.per_host = workload_results
        total.per_host.sort(key=lambda x: x.host)

        if total.max_time_ms == 0:
            print("WARNING: load skipped, because time is 0", str(total))
            return

        if total.min_time_ms * 1.0 / total.max_time_ms < 0.9:
            print("WARNING: too big difference between min/max time to calc avg: {}/{} ms".format(
                total.min_time_ms, total.max_time_ms))

        self.results[total.when.timestamp()] = total

    # Parse output of Cockroach YCSB output
    # Example:
    #
    # Wed Mar  8 00:10:36 CET 2023: uniform workload a from 24 ycsb instances started on 1678230636
    # ...
    # [1] 00:10:53 [SUCCESS] 127.0.0.1
    # ...
    #_elapsed___errors_____ops(total)___ops/sec(cum)__avg(ms)__p50(ms)__p95(ms)__p99(ms)_pMax(ms)__result
    #15952.9s        0       40000511         2507.4    197.4      1.4     11.0    234.9 103079.2
    def parse_cockroach(self, filename):
        result_title_p = re.compile(".*__result$")

        # state while we parse
        current_workload_results = []
        current_workload = ""
        current_distribution = ""
        current_when = None
        current_ycsb_instances_count = 0
        current_host_result = WorkloadResult()
        current_prev_line_result = False

        f = open(filename)
        for line in f.readlines():
            if current_prev_line_result:
                # per host results
                current_prev_line_result = False
                columns = line.split()

                current_host_result.min_time_ms = 0

                time_string = columns[0]
                if time_string[-1:] == "s":
                    current_host_result.min_time_ms = round(float(time_string[:-1]) * 1000)
                current_host_result.max_time_ms = current_host_result.min_time_ms

                current_host_result.errors = int(columns[1])
                current_host_result.oks = int(columns[2])
                current_host_result.throughput = round(float(columns[3]))
                current_host_result.latency = round(float(columns[7]))

                continue
            current_prev_line_result = False

            m = workload_p.match(line)
            if m:
                current_host_result = WorkloadResult()
                current_workload_results = []
                current_distribution = m.groups()[0]
                current_workload = m.groups()[1]
                current_ycsb_instances_count = m.groups()[2]
                when_string = m.groups()[3]
                current_when = datetime.fromtimestamp(int(when_string), timezone.utc)
                continue

            m = host_p.match(line)
            if m:
                if current_host_result.host:
                    current_workload_results.append(current_host_result)
                current_host_result = WorkloadResult()
                current_host_result.host = m.group('host')
                continue

            m = workload_done_p.match(line)
            if m:
                if current_host_result.host:
                    current_workload_results.append(current_host_result)
                self.add_workload(
                    current_workload,
                    current_distribution,
                    current_when,
                    current_ycsb_instances_count,
                    current_workload_results)
                current_host_result = WorkloadResult()
                current_workload = ""
                continue

            m = result_title_p.match(line)
            if m:
                current_prev_line_result = True
                continue

        if current_host_result.host:
            current_workload_results.append(current_host_result)
        self.add_workload(
            current_workload,
            current_distribution,
            current_when,
            current_ycsb_instances_count,
            current_workload_results)

    #Run finished, takes 8.259992138s
    #READ   - Takes(s): 8.2, Count: 331628, OPS: 40310.3, Avg(us): 821, Min(us): 335, Max(us): 33247, 50th(us): 767, 90th(us): 1019, 95th(us): 1180, 99th(us): 1835, 99.9th(us): 5927, 99.99th(us): 23039
    #READ_ERROR - Takes(s): 7.2, Count: 1706, OPS: 237.9, Avg(us): 511775, Min(us): 24, Max(us): 1212415, 50th(us): 5731, 90th(us): 1032191, 95th(us): 1044479, 99th(us): 1054719, 99.9th(us): 1212415, 99.99th(us): 1212415
    #TOTAL  - Takes(s): 8.2, Count: 331628, OPS: 40308.6, Avg(us): 821, Min(us): 335, Max(us): 33247, 50th(us): 767, 90th(us): 1019, 95th(us): 1180, 99th(us): 1835, 99.9th(us): 5927, 99.99th(us): 23039
    def parse_go(self, filename):
        # 4m50.943532166
        result_title_p = re.compile("^Run finished, takes (\d+)?m?(\d+\.\d+)s$")
        error_p = re.compile("^[A-Z]+_ERROR.*Count: ([0-9]+).*")
        total_p = re.compile("^TOTAL.*Count: ([0-9]+), OPS: ([0-9]+).*99th.us.: ([0-9]+)")

        # state while we parse
        current_workload_results = []
        current_workload = ""
        current_distribution = ""
        current_when = None
        current_ycsb_instances_count = 0
        current_host_result = WorkloadResult()
        met_finished = False

        f = open(filename)
        for line in f.readlines():
            m = result_title_p.match(line)
            if m:
                minutes = m.group(1)
                seconds = m.group(2)

                minutes_ms = int(minutes) * 60_000 if minutes else 0
                seconds_ms = float(seconds) * 1_000
                total_ms = minutes_ms + seconds_ms

                current_host_result.min_time_ms = total_ms
                current_host_result.max_time_ms = total_ms
                met_finished = True
                continue

            if met_finished:
                m = error_p.match(line)
                if m:
                    current_host_result.errors += int(m.groups()[0])
                    continue

                m = total_p.match(line)
                if m:
                    current_host_result.oks = int(m.groups()[0])
                    current_host_result.throughput = int(m.groups()[1])
                    current_host_result.latency = int(int(m.groups()[2]) / 1000)

                continue

            m = workload_p.match(line)
            if m:
                met_finished = False

                current_host_result = WorkloadResult()
                current_workload_results = []
                current_distribution = m.groups()[0]
                current_workload = m.groups()[1]
                current_ycsb_instances_count = m.groups()[2]
                when_string = m.groups()[3]
                current_when = datetime.fromtimestamp(int(when_string), timezone.utc)
                continue

            m = host_p.match(line)
            if m:
                met_finished = False

                if current_host_result.host:
                    current_workload_results.append(current_host_result)
                current_host_result = WorkloadResult()
                current_host_result.host = m.groups()[0]
                continue

            m = workload_done_p.match(line)
            if m:
                if current_host_result.host:
                    current_workload_results.append(current_host_result)
                self.add_workload(
                    current_workload,
                    current_distribution,
                    current_when,
                    current_ycsb_instances_count,
                    current_workload_results)
                current_host_result = WorkloadResult()
                current_workload = ""
                continue

        if current_host_result.host:
            current_workload_results.append(current_host_result)
        self.add_workload(
            current_workload,
            current_distribution,
            current_when,
            current_ycsb_instances_count,
            current_workload_results)


    # parse output of the original Java YCSB
    def parse(self, filename, type):
        if type == "cockroach":
            return self.parse_cockroach(filename)
        if type == "go" or type == "postgresql":
            return self.parse_go(filename)

        # Java YCSB result

        ops_p = re.compile("^\[OVERALL\], Throughput\(ops/sec\), ([0-9]+)\..*")
        time_p = re.compile("\[OVERALL\], RunTime\(ms\), ([0-9]+)$")
        latency_99_p = re.compile("\[(?:INSERT|READ|UPDATE|SCAN)(?:-FAILED)?], 99thPercentileLatency\(us\), (\d+)")

        oks_p = re.compile("^\[TotalOKs\] ([0-9]+)$")
        errors_p = re.compile("^\[TotalErrors\] ([0-9]+)$")
        not_found_p = re.compile("^\[TotalNotFound\] ([0-9]+)$")

        # state while we parse
        current_workload_results = []
        current_workload = ""
        current_distribution = ""
        current_when = None
        current_ycsb_instances_count = 0
        current_host_result = WorkloadResult()

        f = open(filename)
        for line in f.readlines():
            m = workload_p.match(line)
            if m:
                current_host_result = WorkloadResult()
                current_workload_results = []
                current_distribution = m.groups()[0]
                current_workload = m.groups()[1]
                current_ycsb_instances_count = m.groups()[2]
                when_string = m.groups()[3]
                current_when = datetime.fromtimestamp(int(when_string), timezone.utc)
                continue

            m = host_p.match(line)
            if m:
                if current_host_result.host:
                    current_workload_results.append(current_host_result)
                current_host_result = WorkloadResult()
                current_host_result.host = m.groups()[0]
                continue

            m = ops_p.match(line)
            if m:
                current_host_result.throughput = round(float(m.groups()[0]))
                continue

            m = time_p.match(line)
            if m:
                current_host_result.max_time_ms = int(m.groups()[0])
                current_host_result.min_time_ms = current_host_result.max_time_ms
                continue

            m = latency_99_p.match(line)
            if m:
                current_host_result.latency = max(current_host_result.latency, int(m.groups()[0]) / 1000)
                continue

            m = oks_p.match(line)
            if m:
                current_host_result.oks = int(m.groups()[0])
                continue

            m = errors_p.match(line)
            if m:
                current_host_result.errors = int(m.groups()[0])
                continue

            m = not_found_p.match(line)
            if m:
                current_host_result.not_found = int(m.groups()[0])
                continue

            m = workload_done_p.match(line)
            if m:
                if current_host_result.host:
                    current_workload_results.append(current_host_result)
                self.add_workload(
                    current_workload,
                    current_distribution,
                    current_when,
                    current_ycsb_instances_count,
                    current_workload_results)
                current_host_result = WorkloadResult()
                current_workload = ""
                continue

        if current_host_result.host:
            current_workload_results.append(current_host_result)
        self.add_workload(
            current_workload,
            current_distribution,
            current_when,
            current_ycsb_instances_count,
            current_workload_results)

    def dump_workloads_ydb(self, args):
        if len(self.results) == 0:
            return

        with open(args.token, 'r') as f:
            token = f.readline()

        driver_config = ydb.DriverConfig(
            args.endpoint, args.database, credentials=ydb.AccessTokenCredentials(token),
            root_certificates=ydb.load_ydb_root_certificate(),
        )
        with ydb.Driver(driver_config) as driver:
            try:
                driver.wait(timeout=5)
            except concurrent.futures._base.TimeoutError:
                print("Connect failed to YDB")
                print("Last reported errors by discovery:")
                print(driver.discovery_debug_details())
                exit(1)

            session = driver.table_client.session().create()
            path = args.database + "/" + args.table

            for ts,result in self.results.items():
                row = {
                    "ts": ts,
                    "version": args.ydb_version,
                    "record_count": args.record_count,
                    "label": args.label,
                    "workload": result.workload,
                    "ycsb_instances_count": result.ycsb_instances_count,
                    "distribution": result.distribution,
                    "rpsK": math.floor(result.throughput / 1000),
                    "latency": math.ceil(result.latency),
                    "oks": result.oks,
                    "errors": result.errors,
                    "not_found": result.not_found,
                    "min_instance_time_ms": result.min_time_ms,
                    "max_instance_time_ms": result.max_time_ms
                }

                session.transaction().execute(
                    """
                    --!syntax_v1
                    UPSERT INTO `{path}`
                    (
                        datetime,
                        version,
                        record_count,
                        label,
                        workload,
                        ycsb_instances_count,
                        distribution,
                        rpsK,
                        latency99ms,
                        oks,
                        errors,
                        not_found,
                        min_instance_time_ms,
                        max_instance_time_ms
                    )
                    VALUES
                    (
                        DateTime::MakeDatetime(DateTime::FromSeconds(CAST({ts} as Uint32))),
                        "{version}",
                        {record_count},
                        "{label}",
                        "{workload}",
                        {ycsb_instances_count},
                        "{distribution}",
                        {rpsK},
                        {latency},
                        {oks},
                        {errors},
                        {not_found},
                        {min_instance_time_ms},
                        {max_instance_time_ms}
                    );
                    """.format(**row, path=path),
                    commit_tx=True,
                )

    def dump_workloads_txt(self):
        for ts,result in self.results.items():
            print("{} {} workload {}: {} Op/s, latency 99% {} ms: {} oks, {} not_found, {} errors, {} ms run time".format(
                result.when.strftime('%Y-%m-%d %H:%M %Z'),
                result.distribution,
                result.workload,
                format_number(result.throughput),
                result.latency,
                format_number(result.oks),
                format_number(result.not_found),
                format_number(result.errors),
                result.max_time_ms))

            for r in result.per_host:
                print("    {}: {} Op/s, time {} ms, latency 99% {} ms".format(r.host, format_number(r.throughput), r.max_time_ms, r.latency))
            print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("log_file", help="file with multiple YCSB results")
    parser.add_argument("--type", help="ydb|cockroach|go", default="ydb")
    parser.add_argument("-e", "--endpoint", help="YDB endpoint")
    parser.add_argument("-d", "--database", help="YDB database")
    parser.add_argument("--table", help="YDB table name")
    parser.add_argument("--token", help="YDB token")
    parser.add_argument("--ydb-version", help="YDB version")
    parser.add_argument("--record-count", help="DB size in rows", type=int)
    parser.add_argument("--label", help="Label to save results with", default="")

    args = parser.parse_args()

    log_parser = Parser()
    log_parser.parse(args.log_file, args.type)
    log_parser.dump_workloads_txt()

    if args.endpoint:
       log_parser.dump_workloads_ydb(args)

    return 0

main()
