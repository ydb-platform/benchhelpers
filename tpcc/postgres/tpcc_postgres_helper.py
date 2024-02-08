#!/usr/bin/env python3

import argparse
import collections
import os
import psycopg2
import re
import sys
import time
import threading
import traceback
import xml.etree.ElementTree as ET

from concurrent.futures import ProcessPoolExecutor, as_completed
from psycopg2 import sql


sript_dir = os.path.dirname(os.path.abspath(__file__))

CREATE_DDL = os.path.join(sript_dir, "ddl-create.sql")
DROP_DDL = os.path.join(sript_dir, "ddl-drop.sql")

TABLES = (
    "warehouse",
    "district",
    "customer",
    "history",
    "new_order",
    "oorder",
    "order_line",
    "item",
    "stock",
)


def execute_sql(connection, sql):
    cur = connection.cursor()
    cur.execute(sql)
    if cur.description:
        result = cur.fetchall()
    else:
        result = None
    cur.close()
    return result


def connect_execute_sql(connection_params, sql, app_name=None):
    connection = psycopg2.connect(**connection_params)
    connection.autocommit = True
    if app_name:
        connection.cursor().execute(f"SET application_name = '{app_name}';")
    return execute_sql(connection, sql)


def execute_sql_async(connection_params, sql, app_name=None):
    with ProcessPoolExecutor() as executor:
        return executor.submit(connect_execute_sql, connection_params, sql, app_name)


def execute_ddl(connection, ddl_name):
    # sanity check
    if not os.path.exists(ddl_name):
        print(f"File {ddl_name} does not exist", file=sys.stderr)
        sys.exit(1)

    with open(ddl_name, "r") as file:
        sql_script = file.read()

    cur = connection.cursor()
    cur.execute(sql_script)

    print(f"{ddl_name} executed")


def connect_execute_ddl(connection_params, ddl_name, app_name=None):
    connection = psycopg2.connect(**connection_params)
    connection.autocommit = True
    if app_name:
        connection.cursor().execute(f"SET application_name = '{app_name}';")
    return execute_ddl(connection, ddl_name)


def execute_ddl_async(connection_params, ddl_name, app_name=None):
    with ProcessPoolExecutor() as executor:
        return executor.submit(connect_execute_ddl, connection_params, ddl_name, app_name)


def get_connection_params(args):
    tree = ET.parse(args.tpcc_config_path)
    root = tree.getroot()

    db = {}

    db["user"] = root.find("username").text if root.find("username") is not None else None
    db["password"] = root.find("password").text if root.find("password") is not None else None

    url = root.find("url").text if root.find("url") is not None else None

    match = re.search(r"jdbc:postgresql://([^/:]+)(?::(\d+))?/([^?]+)", url)

    if match:
        db["host"] = match.group(1)
        db["port"] = match.group(2) if match.group(2) else "5432"
        db["database"] = match.group(3)
    else:
        print(f"Invalid JDBC URL format: {url}", file=sys.stderr)
        sys.exit(1)

    if args.force_host:
        db["host"] = args.force_host

    if args.force_port:
        db["port"] = args.force_port

    return db


def get_table_row_count(args, table):
    sql = f"SELECT COUNT(*) FROM {table};"
    connection = PostgresConnection(args)
    return connection.execute_sql(sql)[0][0]


def validate_warehouses(args):
    wh_count = get_table_row_count(args, "warehouse")
    if wh_count != args.warehouse_count:
        return f"Warehouse count is {wh_count} and not {args.warehouse_count}"
    return None


def validate_districts(args):
    district_count = get_table_row_count(args, "district")
    expected_count = args.warehouse_count * 10
    if district_count != expected_count:
        return f"District count is {district_count} and not {expected_count}"

    return None


def validate_customers(args):
    customer_count = get_table_row_count(args, "customer")
    expected_count = args.warehouse_count * 30000
    if customer_count != expected_count:
        return f"Customer count is {customer_count} and not {expected_count}"

    return None


def validate_items(args):
    item_count = get_table_row_count(args, "item")
    if item_count != 100000:
        return f"Item count is {item_count} and not 100000"

    return None


def validate_open_orders(args):
    orders_count = get_table_row_count(args, "oorder")
    expected_count = args.warehouse_count * 30000
    if orders_count != expected_count:
        return f"Order count is {orders_count} and not {expected_count}"

    return None


def validate_new_orders(args):
    new_orders_count = get_table_row_count(args, "new_order")
    expected_count = args.warehouse_count * 9000
    if new_orders_count != expected_count:
        return f"New order count is {new_orders_count} and not {expected_count}"


def validate_stock(args):
    sql = "SELECT COUNT(distinct S_W_ID) FROM stock;"

    connection = PostgresConnection(args)
    actual_count = connection.execute_sql(sql)[0][0]
    if actual_count != args.warehouse_count:
        return f"Warehouse count is {actual_count} and not {args.warehouse_count}"

    return None


def validate_history(args):
    history_count = get_table_row_count(args, "history")
    expected_count = args.warehouse_count * 30000
    if history_count != expected_count:
        return f"History count is {history_count} and not {expected_count}"

    return None


class HostConfig:
    def __init__(self, warehouses, node_count, node_num):
        if node_num <= 0 or node_num > node_count:
            print("Invalid node_num: {}, must be [1; {}]".format(node_num, node_count), file=sys.stderr)
            sys.exit(1)

        self.warehouses = warehouses
        self.node_num = node_num
        self.node_count = node_count

        # ceil
        self.warehouses_per_host = (warehouses + node_count - 1) // node_count

        self.start_warehouse = 1 + self.warehouses_per_host * (node_num - 1)

        if node_num == node_count:
            # last node
            self.warehouses_per_host = warehouses - (self.warehouses_per_host * (node_count - 1))

        self.terminals_per_host = self.warehouses_per_host * 10

        assert self.warehouses_per_host > 0

    def get_config(self, template_file, **kwargs):
        with open(template_file) as f:
            template = f.read()
        return template.format(
            warehouse=self.warehouses_per_host,
            terminals=self.terminals_per_host,
            **kwargs)


class GenerateConfig:
    def run(self, args):
        host_to_monport = collections.defaultdict(lambda: 8080)

        with open(args.hosts_file) as f:
            num_nodes = len(f.readlines())

        with open(args.hosts_file) as f:
            for node_num, line in enumerate(f, start=1):
                host = line.strip()
                if host == "":
                    continue

                kwargs = {
                    "loader_threads": args.loader_threads,
                    "execute_time_seconds": args.execute_time,
                    "warmup_time_seconds": args.warmup_time,
                    "max_connections": args.max_connections,
                    "mport": host_to_monport[host],
                    "mname": f"node_{node_num}",
                }

                host_config = HostConfig(
                    args.warehouse_count,
                    num_nodes,
                    node_num)

                config = host_config.get_config(args.input, **kwargs)
                output = f"config.{node_num}.xml"
                with open(output, "w") as f:
                    f.write(config)

                host_to_monport[host] = host_to_monport[host] + 1


class GetStartArgs:
    def run(self, args):
        host_config = HostConfig(
            args.warehouse_count,
            args.node_count,
            args.node_num)

        s = "--create=false --load=false --execute=true --start-from-id {start_from} ".format(
            start_from=host_config.start_warehouse,
        )
        print(s)


class PostgresConnection:
    def __init__(self, args):
        try:
            self.connection_params = get_connection_params(args)
            self.connection = psycopg2.connect(**self.connection_params)
            self.connection.autocommit = True
        except Exception as e:
            print(f"Unable to connect to database using: {e}", file=sys.stderr)
            sys.exit(1)

    def get_database(self):
        return self.connection_params["database"]

    def get_endpoint(self):
        return self.connection_params["host"]

    def execute_ddl(self, ddl_name):
        try:
            execute_ddl(self.connection, ddl_name)
        except Exception as e:
            print(f"Unable to execute {ddl_name}: {e}", file=sys.stderr)
            sys.exit(1)

    def execute_sql(self, sql):
        return execute_sql(self.connection, sql)


class DropTables:
    def run(self, args, pg_connection=None):
        if not pg_connection:
            self.pg_connection = PostgresConnection(args)
        else:
            self.pg_connection = pg_connection

        self.pg_connection.execute_ddl(DROP_DDL)


class CreateTables:
    def run(self, args, pg_connection=None):
        if args.unlogged:
            global CREATE_DDL
            CREATE_DDL = os.path.join(sript_dir, "ddl-create-unlogged.sql")

        if not pg_connection:
            self.pg_connection = PostgresConnection(args)
        else:
            self.pg_connection = pg_connection

        drop_tables = DropTables()
        drop_tables.run(args, pg_connection=self.pg_connection)

        self.pg_connection.execute_sql("ALTER SYSTEM SET wal_level = minimal;")
        self.pg_connection.execute_sql("ALTER SYSTEM SET synchronous_commit=OFF;")
        self.pg_connection.execute_sql("SELECT pg_reload_conf();")

        self.pg_connection.execute_ddl(CREATE_DDL)


class PostLoad:
    def run(self, args, pg_connection=None):
        if not pg_connection:
            self.pg_connection = PostgresConnection(args)
        else:
            self.pg_connection = pg_connection

        current_work_mem = None
        if args.max_work_mem:
            current_work_mem = self.pg_connection.execute_sql(
                "SELECT setting FROM pg_settings WHERE name = 'maintenance_work_mem';")[0][0]
            print(f"Current maintenance_work_mem = {current_work_mem} KiB")
            print(f"Setting max maintenance_work_mem to {args.max_work_mem} KiB...")
            self.pg_connection.execute_sql(f"ALTER SYSTEM SET maintenance_work_mem='{args.max_work_mem}';")
            self.pg_connection.execute_sql("SELECT pg_reload_conf();")

        print("Creating indexes ...")

        index1_sql = "CREATE INDEX idx_customer_name ON customer (c_w_id, c_d_id, c_last, c_first);"
        index2_sql = "CREATE INDEX idx_order ON oorder (o_w_id, o_d_id, o_c_id, o_id);"

        futures = []
        futures.append(execute_sql_async(self.pg_connection.connection_params, index1_sql, "customer_index"))
        futures.append(execute_sql_async(self.pg_connection.connection_params, index2_sql, "order_index"))

        try:
            for future in as_completed(futures):
                future.result()
        except Exception as e:
            print(f"Unable to execute ddl: {e}", file=sys.stderr)
            sys.exit(1)

        if current_work_mem:
            print(f"Restoring maintenance_work_mem to {current_work_mem} KiB...")
            self.pg_connection.execute_sql(f"ALTER SYSTEM SET maintenance_work_mem='{current_work_mem}';")
            self.pg_connection.execute_sql("SELECT pg_reload_conf();")

        self.pg_connection.execute_sql("ALTER SYSTEM SET wal_level = replica;")
        self.pg_connection.execute_sql("ALTER SYSTEM SET synchronous_commit=On;")
        self.pg_connection.execute_sql("SELECT pg_reload_conf();")


class VacuumAnalyze:
    def run(self, args, pg_connection=None):
        if not pg_connection:
            self.pg_connection = PostgresConnection(args)
        else:
            self.pg_connection = pg_connection

        current_work_mem = None
        if args.max_work_mem:
            current_work_mem = self.pg_connection.execute_sql(
                "SELECT setting FROM pg_settings WHERE name = 'maintenance_work_mem';")[0][0]
            print(f"Current maintenance_work_mem = {current_work_mem} KiB")
            print(f"Setting max maintenance_work_mem to {args.max_work_mem} KiB...")
            self.pg_connection.execute_sql(f"ALTER SYSTEM SET maintenance_work_mem='{args.max_work_mem}';")
            self.pg_connection.execute_sql("SELECT pg_reload_conf();")

        print("Running vacuum analyze ...")

        futures = []
        for table in TABLES:
            sql = f"VACUUM ANALYZE {table};"
            futures.append(execute_sql_async(self.pg_connection.connection_params, sql, f"vacuum_{table}"))

        try:
            for future in as_completed(futures):
                future.result()
        except Exception as e:
            print(f"Unable to vacuum: {e}", file=sys.stderr)

        if current_work_mem:
            print(f"Restoring maintenance_work_mem to {current_work_mem} KiB...")
            self.pg_connection.execute_sql(f"ALTER SYSTEM SET maintenance_work_mem='{current_work_mem}';")
            self.pg_connection.execute_sql("SELECT pg_reload_conf();")


class ValidateInitialData:
    def run(self, args):

        with ProcessPoolExecutor() as executor:
            futures = [
                executor.submit(validate_warehouses, args),
                executor.submit(validate_districts, args),
                executor.submit(validate_customers, args),
                executor.submit(validate_items, args),
                executor.submit(validate_open_orders, args),
                executor.submit(validate_new_orders, args),
                executor.submit(validate_stock, args),
                executor.submit(validate_history, args),
            ]

            for future in as_completed(futures):
                try:
                    error = future.result()
                    if error:
                        print(error)
                        sys.exit(1)
                except Exception as e:
                    print(f"An exception occurred: {e}")
                    traceback.print_exc()
                    sys.exit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-w", "--warehouses", dest="warehouse_count",
                        type=int, default=10,
                        help="Number of warehouses")
    parser.add_argument("-n", "--nodes", dest="node_count",
                        type=int, default=1,
                        help="Number of TPCC nodes")

    subparsers = parser.add_subparsers(dest="action", help="Action to perform")

    generate_config_parser = subparsers.add_parser("generate-configs")
    generate_config_parser.add_argument("--hosts", dest="hosts_file", required=True, help="File with hosts")
    generate_config_parser.add_argument("-i", "--input", dest="input", required=True, help="Input template file")

    generate_config_parser.add_argument("--loader-threads", dest="loader_threads",
                                        required=True, type=int, help="Loader threads per host")

    generate_config_parser.add_argument("--execute-time", dest="execute_time",
                                        required=True, help="Execute time in seconds")

    generate_config_parser.add_argument("--warmup-time", dest="warmup_time",
                                        required=True, help="Warmup time in seconds")

    generate_config_parser.add_argument("--max-connections", dest="max_connections",
                                        required=True, help="Max connections per TPC-C instance")

    generate_config_parser.set_defaults(func=GenerateConfig().run)

    start_args_parser = subparsers.add_parser("get-start-args")
    start_args_parser.add_argument("--node-num", dest="node_num", required=True, type=int,
                             default=1, help="TPCC host number (1-based)")
    start_args_parser.set_defaults(func=GetStartArgs().run)

    drop_parser = subparsers.add_parser("drop")
    drop_parser.add_argument("--tpcc-config", dest="tpcc_config_path", required=True, help="TPCC config file")
    drop_parser.add_argument("--force-host", dest="force_host", help="Force host")
    drop_parser.add_argument("--force-port", dest="force_port", help="Force port")
    drop_parser.set_defaults(func=DropTables().run)

    create_parser = subparsers.add_parser("create")
    create_parser.set_defaults(func=CreateTables().run)
    create_parser.add_argument("--tpcc-config", dest="tpcc_config_path", required=True, help="TPCC config file")
    create_parser.add_argument("--force-host", dest="force_host", help="Force host")
    create_parser.add_argument("--force-port", dest="force_port", help="Force port")
    create_parser.add_argument("--unlogged-tables", dest="unlogged", action="store_true", help="Use unlogged tables")

    post_load_alter = subparsers.add_parser("postload")
    post_load_alter.set_defaults(func=PostLoad().run)
    post_load_alter.add_argument("--tpcc-config", dest="tpcc_config_path", required=True, help="TPCC config file")
    post_load_alter.add_argument("--max-maintenance-work-mem", dest="max_work_mem", help="Max maintenance_work_mem, KiB")
    post_load_alter.add_argument("--force-host", dest="force_host", help="Force host")
    post_load_alter.add_argument("--force-port", dest="force_port", help="Force port")

    vacuum_analyze = subparsers.add_parser("vacuum-analyze")
    vacuum_analyze.set_defaults(func=VacuumAnalyze().run)
    vacuum_analyze.add_argument("--tpcc-config", dest="tpcc_config_path", required=True, help="TPCC config file")
    vacuum_analyze.add_argument("--max-maintenance-work-mem", dest="max_work_mem", help="Max maintenance_work_mem, KiB")
    vacuum_analyze.add_argument("--force-host", dest="force_host", help="Force host")
    vacuum_analyze.add_argument("--force-port", dest="force_port", help="Force port")

    validate_parser = subparsers.add_parser('validate')
    validate_parser.set_defaults(func=ValidateInitialData().run)
    validate_parser.add_argument("--tpcc-config", dest="tpcc_config_path", required=True, help="TPCC config file")
    validate_parser.add_argument("--max-maintenance-work-mem", dest="max_work_mem", help="Max maintenance_work_mem, KiB")
    validate_parser.add_argument("--force-host", dest="force_host", help="Force host")
    validate_parser.add_argument("--force-port", dest="force_port", help="Force port")

    args = parser.parse_args()

    args.func(args)


if __name__ == "__main__":
    main()
