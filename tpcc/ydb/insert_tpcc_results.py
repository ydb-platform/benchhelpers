#!/usr/bin/python3

# This script inserts TPC-C results into YDB table.

import argparse
import concurrent
import json
import os
import sys
import ydb

from datetime import datetime, timezone


def drop(session, table_path):
    sql = f"""
        --!syntax_v1
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
        --!syntax_v1
        CREATE TABLE `{table_path}` (
            timestamp Timestamp,
            version Utf8,
            label Utf8,
            warehouses Uint32,
            duration_seconds Uint32,
            tpmC Uint32,
            efficiency Double,
            throughput Uint32,
            goodput Uint32,
            newOrderLatency90 Uint32,
            json Json,

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
    summary = results['summary']
    json_string = json.dumps(results, separators=(',', ':'))

    sql = f"""
        --!syntax_v1
        UPSERT INTO `{path}`
        (
            `timestamp`,
            `version`,
            `label`,
            `warehouses`,
            `duration_seconds`,
            `tpmC`,
            `efficiency`,
            `throughput`,
            `goodput`,
            `newOrderLatency90`,
            `json`
        ) VALUES (
            DateTime::FromSeconds({summary['measure_start_ts']}),
            "{args.ydb_version}",
            "{args.label}",
            {summary['warehouses']},
            {summary['time_seconds']},
            {summary['tpmc']},
            {summary['efficiency']},
            {summary['throughput']},
            {summary['goodput']},
            {results['transactions']['NewOrder']['percentiles']['90']},
            '{json_string}'
        );
    """

    session.transaction().execute(sql, commit_tx=True)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("results_file", help="json file with TPC-C results")
    parser.add_argument("-e", "--endpoint", help="YDB endpoint")
    parser.add_argument("-d", "--database", help="YDB database")
    parser.add_argument("--table", help="YDB table name")
    parser.add_argument("--token", help="YDB token")
    parser.add_argument("--ydb-version", help="YDB version")
    parser.add_argument("--label", help="label")
    parser.add_argument("--drop", help="Drop table with results", action="store_true")

    args = parser.parse_args()

    if not os.path.exists(args.token):
        print("Token file not found")
        sys.exit(1)

    if not os.path.exists(args.results_file):
        print("Results file not found")
        sys.exit(1)

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
            sys.exit(1)

        path = args.database + "/" + args.table

        with ydb.SessionPool(driver) as pool:
            if args.drop:
                pool.retry_operation_sync(lambda session: drop(session, path))
            pool.retry_operation_sync(lambda session: create_table(session, path))

            with open(args.results_file, 'r') as f:
                results = json.load(f)

            summary = results['summary']
            pool.retry_operation_sync(lambda session: insert_ydb_results_row(session, path, args, results))


main()
