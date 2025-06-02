#!/usr/bin/python3

# Wrapper for `ydb debug latency`: runs the command and uploads results to the
# specified YDB database.
# For each `debug latency` kind we store max observed throughput (among all inflights)
# and latency percentiles for inflight 1.

# full cmd is `ydb -p perf1 debug latency --min-inflight 1 --max-inflight 64 --interval 5 -f JSON`
# current json format:
# {
#    "RawResults" : [
#       {
#          "PlainGrpc" : [
#             {
#                "Inflight" : 1,
#                "Throughput" : 10057,
#                "p50" : 97,
#                "p90" : 105,
#                "p99" : 152
#             },
#             ...
#          ]
#       },
#       {
#          "GrpcProxy" : [
#             ...
#          ]
#       },
#       ...
#    ],
#    "Throughputs" : {
#       "ActorChain" : 10345,
#       "GrpcProxy" : 18657,
#       "PlainGrpc" : 19752,
#       "PlainKqp" : 16796,
#       "SchemeCache" : 17318,
#       "Select1" : 7937,
#       "TxProxy" : 17682
#    }
# }

MIN_INFLIGHT = 1
MAX_INFLIGHT = 64
INTERVAL = 5
RUN_KIND = "AllKinds"

import argparse
import concurrent
import json
import os
import subprocess
import sys
import ydb

from datetime import datetime, timezone


def drop(session, table_path):
    sql = f"""
        DROP TABLE `{table_path}`;
    """

    try:
        session.execute_scheme(sql)
    except (ydb.issues.NotFound, ydb.issues.SchemeError):
        pass
    except Exception as e:
        print("Error dropping table {}: {}".format(table_path, e), file=sys.stderr)
        raise e


def create_table(session, table_path):
    sql = f"""
        CREATE TABLE `{table_path}` (
            timestamp Timestamp,
            cluster Utf8,
            version Utf8,
            git_repository Utf8,
            git_commit_timestamp Timestamp,
            git_branch Utf8,
            kind Utf8,
            label Utf8,
            throughput Uint32,
            latency_p50 Uint32,
            latency_p90 Uint32,
            latency_p99 Uint32,

            PRIMARY KEY (timestamp)
        );
    """

    try:
        session.execute_scheme(sql)
    except (ydb.issues.AlreadyExists):
        pass
    except Exception as e:
        print("Error creating table: {}, sql:\n{}".format(e, sql), file=sys.stderr)
        raise e


def insert_ydb_results_row(session, path, args, results):
    """
    Insert debug latency results into YDB table.
    For each debug latency kind, we store max observed throughput (from Throughputs)
    and latency percentiles for inflight 1 (from RawResults).
    """

    # Get current timestamp
    current_timestamp = int(datetime.now(timezone.utc).timestamp())

    # Extract throughputs for each kind
    throughputs = results.get('Throughputs', {})

    # Extract raw results to get percentiles for inflight 1
    raw_results = results.get('RawResults', [])

    # Process each kind of latency test
    for result_group in raw_results:
        for kind, inflight_results in result_group.items():
            # Find results for inflight 1
            inflight_1_data = None
            for inflight_data in inflight_results:
                if inflight_data.get('Inflight') == 1:
                    inflight_1_data = inflight_data
                    break

            if inflight_1_data is None:
                continue

            # Get max throughput for this kind
            max_throughput = throughputs.get(kind, 0)

            # Extract percentiles
            p50 = inflight_1_data.get('p50', 0)
            p90 = inflight_1_data.get('p90', 0)
            p99 = inflight_1_data.get('p99', 0)

            sql = f"""
                UPSERT INTO `{path}`
                (
                    `timestamp`,
                    `cluster`,
                    `version`,
                    `git_repository`,
                    `git_commit_timestamp`,
                    `git_branch`,
                    `kind`,
                    `label`,
                    `throughput`,
                    `latency_p50`,
                    `latency_p90`,
                    `latency_p99`
                ) VALUES (
                    DateTime::FromSeconds({current_timestamp}),
                    "{args.label_cluster}",
                    "{args.ydb_version}",
                    "{args.git_repository}",
                    DateTime::FromSeconds({args.git_commit_timestamp}),
                    "{args.git_branch}",
                    "{kind}",
                    "{args.label}",
                    {max_throughput},
                    {p50},
                    {p90},
                    {p99}
                );
            """

            session.transaction().execute(sql, commit_tx=True)


def run_cli(ydb_path, endpoint, database):
    """
    Run YDB CLI debug latency command and return parsed JSON results.
    """

    # Check if YDB CLI path exists
    if not os.path.exists(ydb_path):
        print(f"YDB CLI not found at path: {ydb_path}")
        sys.exit(1)

    # Build the command
    cmd = [
        ydb_path,
        "-e", endpoint,
        "-d", database,
        "debug", "latency",
        "-k", RUN_KIND,
        "--min-inflight", str(MIN_INFLIGHT),
        "--max-inflight", str(MAX_INFLIGHT),
        "--interval", str(INTERVAL),
        "-f", "JSON"
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        json_result = json.loads(result.stdout)
        return json_result

    except subprocess.CalledProcessError as e:
        print(f"Error running YDB CLI command: {e}")
        print(f"stderr: {e.stderr}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON output: {e}")
        print(f"stdout: {result.stdout}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ydb-path", help="path to YDB CLI to use", required=True)
    parser.add_argument("-e", "--endpoint", help="YDB endpoint for storing results", required=True)
    parser.add_argument("-d", "--database", help="YDB database for storing results", required=True)
    parser.add_argument("--test-endpoint", help="YDB endpoint to test (target for debug latency)", required=True)
    parser.add_argument("--test-database", help="YDB database to test (target for debug latency)", required=True)
    parser.add_argument("--table", help="YDB table name", required=True)
    parser.add_argument("--token", help="YDB token")
    parser.add_argument("--sa-token", help="YDB service account token")
    parser.add_argument("--ydb-version", help="YDB version", required=True)
    parser.add_argument("--label", help="label", required=True)
    parser.add_argument("--label-cluster", help="cluster label", required=True)
    parser.add_argument("--git-commit-timestamp", help="git commit timestamp", required=True)
    parser.add_argument("--git-repository", help="repository", required=True)
    parser.add_argument("--git-branch", help="branch", required=True)
    parser.add_argument("--drop", help="Drop table with results", action="store_true")

    args = parser.parse_args()

    # Validate token arguments
    if args.token is not None:
        if not os.path.exists(args.token):
            print(f"YDB token file not found by path {args.token}")
            sys.exit(1)
        with open(args.token, 'r') as f:
            token = f.readline()

        driver_config = ydb.DriverConfig(
            args.endpoint,
            args.database,
            credentials=ydb.AccessTokenCredentials(token),
            root_certificates=ydb.load_ydb_root_certificate(),
        )
    elif args.sa_token is not None:
        if not os.path.exists(args.sa_token):
            print(f"IAM token file not found by path {args.sa_token}")
            sys.exit(1)
        driver_config = ydb.DriverConfig(
            args.endpoint,
            args.database,
            credentials=ydb.iam.ServiceAccountCredentials.from_file(args.sa_token),
        )
    else:
        driver_config = ydb.DriverConfig(
            args.endpoint,
            args.database,
            credentials=ydb.credentials.AnonymousCredentials())

    # Run the YDB CLI command to get debug latency results
    print("Running YDB debug latency command...")
    results = run_cli(args.ydb_path, args.test_endpoint, args.test_database)
    print("Debug latency command completed successfully")

    # Connect to YDB and insert results
    with ydb.Driver(driver_config) as driver:
        try:
            driver.wait(timeout=15)
        except concurrent.futures._base.TimeoutError:
            print("Connect failed to YDB")
            print("Last reported errors by discovery:")
            print(driver.discovery_debug_details())
            sys.exit(1)

        table_path = args.database + "/" + args.table

        with ydb.SessionPool(driver) as pool:
            if args.drop:
                print(f"Dropping table {table_path}")
                pool.retry_operation_sync(lambda session: drop(session, table_path))

            print(f"Creating table {table_path}")
            pool.retry_operation_sync(lambda session: create_table(session, table_path))

            print("Inserting results into YDB table...")
            pool.retry_operation_sync(lambda session: insert_ydb_results_row(session, table_path, args, results))
            print("Results inserted successfully")


main()
