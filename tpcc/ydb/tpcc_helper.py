#!/usr/bin/env python3

import argparse
import bisect
import collections
import concurrent.futures
import datetime
import json
import numpy as np
import os
import re
import subprocess
import sys
import time
import traceback
import ydb


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

# only for heavy tbales
PER_WAREHOUSE_MB = {
    "stock": 40.6,
    "customer": 20.1,
    "order_line": 28.2,
    "history": 2.4,
    "oorder": 1.5,
}

DEFAULT_MIN_PARTITIONS = 50
DEFAULT_SHARD_SIZE_MB = 2048


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

        self.last_warehouse = self.start_warehouse + self.warehouses_per_host - 1
        self.terminals_per_host = self.warehouses_per_host * 10

        assert self.warehouses_per_host > 0

    def get_split_keys(self, table_name):
        # we have warehouses [self.start_warehouse; self.last_warehouse]
        # in total self.warehouses_per_host

        if table_name not in PER_WAREHOUSE_MB:
            print("not")
            return []

        mb_per_wh = PER_WAREHOUSE_MB[table_name]
        warehouses_per_shard = (DEFAULT_SHARD_SIZE_MB + mb_per_wh - 1) //  mb_per_wh

        if warehouses_per_shard > self.warehouses_per_host:
            return []

        split_keys = []
        current_split_key = self.start_warehouse + warehouses_per_shard
        while current_split_key < self.last_warehouse:
            split_keys.append(current_split_key)
            current_split_key += warehouses_per_shard

        return split_keys

    def get_config(self, template_file, **kwargs):
        with open(template_file) as f:
            template = f.read()
        return template.format(
            warehouse=self.warehouses_per_host,
            terminals=self.terminals_per_host,
            **kwargs)


class YdbConnection:
    def __init__(self, args):
        try:
            credentials = None
            userName = os.getenv("YDB_USER")
            if args.token:
                if not os.path.isfile(args.token):
                    print("Token file {} not found".format(args.token), file=sys.stderr)
                    sys.exit(1)
                with open(args.token, 'r') as f:
                    token = f.readline()
                credentials = ydb.AuthTokenCredentials(token)
            elif userName is not None:
                temp_config = ydb.DriverConfig(
                    args.endpoint, args.database,
                    root_certificates=ydb.load_ydb_root_certificate(),
                )
                temp_pass = os.getenv("YDB_PASSWORD")
                credentials = ydb.StaticCredentials(temp_config, user=userName, password=temp_pass)
            else:
                credentials = ydb.credentials_from_env_variables()

            self.database = args.database
            self.endpoint = args.endpoint

            driver_config = ydb.DriverConfig(
                args.endpoint, args.database, credentials=credentials,
                root_certificates=ydb.load_ydb_root_certificate(),
            )

            self.driver = ydb.Driver(driver_config)
            try:
                self.driver.wait(timeout=5)
            except concurrent.futures.TimeoutError:
                print("Connect failed to YDB", file=sys.stderr)
                print("Last reported errors by discovery:", file=sys.stderr)
                print(self.driver.discovery_debug_details(), file=sys.stderr)
                sys.exit(1)

        except Exception as e:
            print("Error creating YDB driver: {}".format(e), file=sys.stderr)
            sys.exit(1)

    def get_database(self):
        return self.database

    def get_endpoint(self):
        return self.endpoint


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
                    "max_sessions": args.max_sessions,
                    "mport": host_to_monport[host],
                    "mname": f"node_{node_num}",
                    "ydb_host": args.ydb_host,
                    "db_path": args.database,
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


class GetLoadArgs:
    def run(self, args):
        host_config = HostConfig(
            args.warehouse_count,
            args.node_count,
            args.node_num)

        s = f"""--create=false --load=true --execute=false --start-from-id {host_config.start_warehouse}"""
        s += f""" --total-warehouses {args.warehouse_count}"""
        print(s)


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


class DropTables:
    def run(self, args, ydb_connection=None):
        if not ydb_connection:
            self.ydb_connection = YdbConnection(args)
        else:
            self.ydb_connection = ydb_connection

        for t in TABLES:
            self.drop_table(t)

        print("Tables dropped")

    def drop_table(self, table_name):
        sql = """
            --!syntax_v1
            DROP TABLE `{}`;
        """.format(table_name)

        try:
            session = self.ydb_connection.driver.table_client.session().create()
            session.execute_scheme(sql)
        except (ydb.issues.NotFound, ydb.issues.SchemeError):
            pass
        except Exception as e:
            print(type(e))
            print("Error dropping table {}: {}".format(table_name, e), file=sys.stderr)
            sys.exit(1)


class CreateTables:
    def run(self, args, ydb_connection=None):
        if not ydb_connection:
            self.ydb_connection = YdbConnection(args)
        else:
            self.ydb_connection = ydb_connection

        drop_tables = DropTables()
        drop_tables.run(args, ydb_connection=self.ydb_connection)

        sql = f"""
            --!syntax_v1
            CREATE TABLE warehouse (
                W_ID       Int32          NOT NULL,
                W_YTD      Double,
                W_TAX      Double,
                W_NAME     Utf8,
                W_STREET_1 Utf8,
                W_STREET_2 Utf8,
                W_CITY     Utf8,
                W_STATE    Utf8,
                W_ZIP      Utf8,
                PRIMARY KEY (W_ID)
            )
            WITH (
                AUTO_PARTITIONING_BY_LOAD = ENABLED,
                AUTO_PARTITIONING_MIN_PARTITIONS_COUNT = {DEFAULT_MIN_PARTITIONS}
            );
        """
        self.create_table(sql)

        sql = f"""
            --!syntax_v1
            CREATE TABLE item (
                I_ID    Int32           NOT NULL,
                I_NAME  Utf8,
                I_PRICE Double,
                I_DATA  Utf8,
                I_IM_ID Int32,
                PRIMARY KEY (I_ID)
            )
            WITH (
                AUTO_PARTITIONING_BY_LOAD = ENABLED,
                AUTO_PARTITIONING_MIN_PARTITIONS_COUNT = {DEFAULT_MIN_PARTITIONS}
            );
        """
        self.create_table(sql)

        stock_split_keys, stock_shard_count = self.get_split_keys_str(args, "stock")
        sql = f"""
            --!syntax_v1
            CREATE TABLE stock (
                S_W_ID       Int32           NOT NULL,
                S_I_ID       Int32           NOT NULL,
                S_QUANTITY   Int32,
                S_YTD        Double,
                S_ORDER_CNT  Int32,
                S_REMOTE_CNT Int32,
                S_DATA       Utf8,
                S_DIST_01    Utf8,
                S_DIST_02    Utf8,
                S_DIST_03    Utf8,
                S_DIST_04    Utf8,
                S_DIST_05    Utf8,
                S_DIST_06    Utf8,
                S_DIST_07    Utf8,
                S_DIST_08    Utf8,
                S_DIST_09    Utf8,
                S_DIST_10    Utf8,
                PRIMARY KEY (S_W_ID, S_I_ID)
            )
            WITH (
                AUTO_PARTITIONING_BY_LOAD = ENABLED,
                AUTO_PARTITIONING_MIN_PARTITIONS_COUNT = {stock_shard_count}
                {stock_split_keys}
            );
        """
        self.create_table(sql)

        sql = f"""
            --!syntax_v1
            CREATE TABLE district (
                D_W_ID      Int32            NOT NULL,
                D_ID        Int32            NOT NULL,
                D_YTD       Double,
                D_TAX       Double,
                D_NEXT_O_ID Int32,
                D_NAME      Utf8,
                D_STREET_1  Utf8,
                D_STREET_2  Utf8,
                D_CITY      Utf8,
                D_STATE     Utf8,
                D_ZIP       Utf8,
                PRIMARY KEY (D_W_ID, D_ID)
            )
            WITH (
                AUTO_PARTITIONING_BY_LOAD = ENABLED,
                AUTO_PARTITIONING_MIN_PARTITIONS_COUNT = {DEFAULT_MIN_PARTITIONS}
            );
        """
        self.create_table(sql)

        customer_split_keys, custromer_shard_count = self.get_split_keys_str(args, "customer")
        sql = f"""
            --!syntax_v1
            CREATE TABLE customer (
                C_W_ID         Int32            NOT NULL,
                C_D_ID         Int32            NOT NULL,
                C_ID           Int32            NOT NULL,
                C_DISCOUNT     Double,
                C_CREDIT       Utf8,
                C_LAST         Utf8,
                C_FIRST        Utf8,
                C_CREDIT_LIM   Double,
                C_BALANCE      Double,
                C_YTD_PAYMENT  Double,
                C_PAYMENT_CNT  Int32,
                C_DELIVERY_CNT Int32,
                C_STREET_1     Utf8,
                C_STREET_2     Utf8,
                C_CITY         Utf8,
                C_STATE        Utf8,
                C_ZIP          Utf8,
                C_PHONE        Utf8,
                C_SINCE        Timestamp,
                C_MIDDLE       Utf8,
                C_DATA         Utf8,

                PRIMARY KEY (C_W_ID, C_D_ID, C_ID)
            )
            WITH (
                AUTO_PARTITIONING_BY_LOAD = ENABLED,
                AUTO_PARTITIONING_MIN_PARTITIONS_COUNT = {custromer_shard_count}
                {customer_split_keys}
            );
        """
        self.create_table(sql)

        history_split_keys, history_shard_count = self.get_split_keys_str(args, "history")
        sql = f"""
            --!syntax_v1
            CREATE TABLE history (
                H_C_W_ID    Int32,
                H_C_ID      Int32,
                H_C_D_ID    Int32,
                H_D_ID      Int32,
                H_W_ID      Int32,
                H_DATE      Timestamp,
                H_AMOUNT    Double,
                H_DATA      Utf8,
                H_C_NANO_TS Int64        NOT NULL,

                PRIMARY KEY (H_C_W_ID, H_C_NANO_TS)
            )
            WITH (
                AUTO_PARTITIONING_BY_LOAD = ENABLED,
                AUTO_PARTITIONING_MIN_PARTITIONS_COUNT = {history_shard_count}
                {history_split_keys}
            );
        """
        self.create_table(sql)

        oorder_split_keys, oorder_shard_count = self.get_split_keys_str(args, "oorder")
        sql = f"""
            --!syntax_v1
            CREATE TABLE oorder (
                O_W_ID       Int32       NOT NULL,
                O_D_ID       Int32       NOT NULL,
                O_ID         Int32       NOT NULL,
                O_C_ID       Int32,
                O_CARRIER_ID Int32,
                O_OL_CNT     Int32,
                O_ALL_LOCAL  Int32,
                O_ENTRY_D    Timestamp,

                PRIMARY KEY (O_W_ID, O_D_ID, O_ID)
            )
            WITH (
                AUTO_PARTITIONING_BY_LOAD = ENABLED,
                AUTO_PARTITIONING_MIN_PARTITIONS_COUNT = {oorder_shard_count}
                {oorder_split_keys}
            );
        """
        self.create_table(sql)

        sql = f"""
            --!syntax_v1
            CREATE TABLE new_order (
                NO_W_ID Int32 NOT NULL,
                NO_D_ID Int32 NOT NULL,
                NO_O_ID Int32 NOT NULL,

                PRIMARY KEY (NO_W_ID, NO_D_ID, NO_O_ID)
            )
            WITH (
                AUTO_PARTITIONING_BY_LOAD = ENABLED,
                AUTO_PARTITIONING_MIN_PARTITIONS_COUNT = {DEFAULT_MIN_PARTITIONS}
            );
        """
        self.create_table(sql)

        order_line_split_keys, order_line_shard_count = self.get_split_keys_str(args, "order_line")
        sql = f"""
            --!syntax_v1
            CREATE TABLE order_line (
                OL_W_ID        Int32           NOT NULL,
                OL_D_ID        Int32           NOT NULL,
                OL_O_ID        Int32           NOT NULL,
                OL_NUMBER      Int32           NOT NULL,
                OL_I_ID        Int32,
                OL_DELIVERY_D  Timestamp,
                OL_AMOUNT      Double,
                OL_SUPPLY_W_ID Int32,
                OL_QUANTITY    Double,
                OL_DIST_INFO   Utf8,

                PRIMARY KEY (OL_W_ID, OL_D_ID, OL_O_ID, OL_NUMBER)
            )
            WITH (
                AUTO_PARTITIONING_BY_LOAD = ENABLED,
                AUTO_PARTITIONING_MIN_PARTITIONS_COUNT = {order_line_shard_count}
                {order_line_split_keys}
            );
        """
        self.create_table(sql)

        print("Tables created")

    def create_table(self, sql):
        try:
            print(sql)
            session = self.ydb_connection.driver.table_client.session().create()
            session.execute_scheme(sql)
        except Exception as e:
            print("Error creating table: {}, sql:\n{}".format(e, sql), file=sys.stderr)
            sys.exit(1)

    def get_split_keys_str(self, args, table_name):
        split_keys = []
        for node_num in range(1, args.node_count + 1):
            host_config = HostConfig(
                args.warehouse_count,
                args.node_count,
                node_num)
            split_keys += host_config.get_split_keys(table_name)

        if len(split_keys) == 0:
            return "", DEFAULT_MIN_PARTITIONS
        else:
            split_keys = [str(int(x)) for x in split_keys]
            split_keys_str = ",PARTITION_AT_KEYS = (" + ",".join(split_keys) + ")"
            min_partitions = max(DEFAULT_MIN_PARTITIONS, len(split_keys) + 1)
            return split_keys_str, min_partitions,

class AsyncCreateIndices:
    def run(self, args, ydb_connection=None):
        if not ydb_connection:
            self.ydb_connection = YdbConnection(args)
        else:
            self.ydb_connection = ydb_connection

        indices = {
            "idx_customer_name":
                """
                    --!syntax_v1
                    ALTER TABLE `customer` ADD INDEX `idx_customer_name` GLOBAL ON (C_W_ID, C_D_ID, C_LAST, C_FIRST);
                """,
            "idx_order":
                """
                    --!syntax_v1
                    ALTER TABLE `oorder` ADD INDEX `idx_order` GLOBAL ON (O_W_ID, O_D_ID, O_C_ID, O_ID);
                """,
        }

        futures = {}
        for index, sql in indices.items():
            print("Creating index {}".format(index))
            futures[index] = self.create_index(sql)

        for index, future in futures.items():
            try:
                # we set timeout to 1 second, because we want to check here
                # that requests have started execution and then check state manually
                result = future.result(timeout=1)
            except ydb.issues.DeadlineExceed:
                print("DeadlineExceed for {}, but will check state manually".format(index))
            except concurrent.futures.TimeoutError:
                pass
            except Exception as e:
                print("Failed to create index {}: {}".format(index, e), file=sys.stderr)
                sys.exit(1)

        print("Indices are being created")


    def create_index(self, sql):
        try:
            session = self.ydb_connection.driver.table_client.session().create()
            return session.async_execute_scheme(sql)
        except Exception as e:
            print(type(e))
            print("Error creating indices: {}".format(e), file=sys.stderr)
            sys.exit(1)


class WaitIndicesReady:
    def run(self, args, ydb_connection=None):
        print("Waiting for indices to be ready...")

        # TODO: use SDK?

        command = [
            "ydb",
            "--endpoint",
            args.endpoint,
            "--database",
            args.database,
            "operation",
            "list",
            "buildindex",
            "--format",
            "proto-json-base64",
        ]

        while True:
            for i in range(10):
                result = subprocess.run(command, capture_output=True, text=True)
                if result.returncode != 0:
                    time.sleep(10)

            if result.returncode != 0:
                print("Error getting index status: {}".format(result.stderr), file=sys.stderr)
                sys.exit(1)

            output = result.stdout
            if output == "":
                print("Error getting index status: empty output", file=sys.stderr)
                sys.exit(1)

            output = json.loads(output)
            operations = output["operations"]

            bad_states = (
                "STATE_UNSPECIFIED",
                "STATE_CANCELLATION",
                "STATE_CANCELLED",
                "STATE_REJECTION",
                "STATE_REJECTED",
            )

            in_progress_states = (
                "STATE_UNSPECIFIED",
                "STATE_PREPARING",
                "STATE_TRANSFERING_DATA",
                "STATE_APPLYING",
            )

            all_ready = False
            for op in operations:
                if op["metadata"]["state"] in bad_states:
                    print(f"Error creating indices: {operations}", file=sys.stderr)
                    sys.exit(1)

                if op["metadata"]["state"] in in_progress_states:
                    break

                if "ready" in op:
                    if not op["ready"]:
                        break

                if op["metadata"]["state"] == "STATE_DONE":
                    if "status" in op:
                        if op["status"] != "SUCCESS":
                            print("Error creating indices: {}".format(op), file=sys.stderr)
                            sys.exit(1)
            else:
                all_ready = True

            if all_ready:
                time.sleep(10) # hack, because we have a small issue with reporting OK
                print("Indices created")
                break
            time.sleep(10)

        print("Indices are ready")


class ValidateInitialData:
    def run(self, args):
        self.ydb_connection = YdbConnection(args)
        get_session = lambda: self.ydb_connection.driver.table_client.session().create()

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(self.validate_warehouses, args, get_session()),
                executor.submit(self.validate_districts, args, get_session()),
                executor.submit(self.validate_customers, args, get_session()),
                executor.submit(self.validate_items, args, get_session()),
                executor.submit(self.validate_open_orders, args, get_session()),
                executor.submit(self.validate_new_orders, args, get_session()),
                executor.submit(self.validate_order_lines, args, get_session()),
                executor.submit(self.validate_stock, args, get_session()),
                executor.submit(self.validate_history, args, get_session()),
            }

            for future in concurrent.futures.as_completed(futures):
                try:
                    error = future.result()
                    if error:
                        print(error)
                        sys.exit(1)
                except Exception as e:
                    print(f"An exception occurred: {e}")
                    traceback.print_exc()
                    sys.exit(1)


    def validate_warehouses(self, args, session):
        sql = """
            --!syntax_v1
            SELECT COUNT(*) as warehouse_count FROM `warehouse`;
        """

        result_sets = session.transaction().execute(sql)
        if not result_sets[0].rows:
            return "No warehouses found"

        row = result_sets[0].rows[0]
        if row.warehouse_count != args.warehouse_count:
            return "Warehouse count is {} and not {}".format(row.warehouse_count, args.warehouse_count)

        return None

    def validate_districts(self, args, session):
        sql = """
            --!syntax_v1
            SELECT COUNT(*) as district_count FROM `district`;
        """

        result_sets = session.transaction().execute(sql)
        if not result_sets[0].rows:
            return "No districts found"

        row = result_sets[0].rows[0]
        expected_count = args.warehouse_count * 10
        if row.district_count != expected_count:
            return "District count is {} and not {}".format(row.district_count, expected_count)

        return None

    def validate_customers(self, args, session):
        sql = """
            --!syntax_v1
            SELECT COUNT(*) as customer_count FROM `customer`;
        """

        result_sets = session.transaction().execute(sql)
        if not result_sets[0].rows:
            return "No customers found"

        row = result_sets[0].rows[0]
        expected_count = args.warehouse_count * 30000
        if row.customer_count != expected_count:
            return "Customer count is {} and not {}".format(row.customer_count, expected_count)

        return None

    def validate_items(self, args, session):
        sql = """
            --!syntax_v1
            SELECT COUNT(*) as item_count FROM `item`;
        """

        result_sets = session.transaction().execute(sql)
        if not result_sets[0].rows:
            return "No items found"

        row = result_sets[0].rows[0]
        if row.item_count != 100000:
            return "Item count is {} and not 100000".format(row.item_count)

        return None

    def validate_open_orders(self, args, session):
        sql = """
            --!syntax_v1
            SELECT COUNT(*) as order_count FROM `oorder`;
        """

        result_sets = session.transaction().execute(sql)
        if not result_sets[0].rows:
            return "No orders found"

        row = result_sets[0].rows[0]
        expected_count = args.warehouse_count * 30000
        if row.order_count != expected_count:
            return "Order count is {} and not {}".format(row.order_count, expected_count)

        return None

    def validate_new_orders(self, args, session):
        sql = """
            --!syntax_v1
            SELECT COUNT(*) as new_order_count FROM `new_order`;
        """

        result_sets = session.transaction().execute(sql)
        if not result_sets[0].rows:
            return "No new orders found"

        row = result_sets[0].rows[0]
        expected_count = args.warehouse_count * 9000
        if row.new_order_count != expected_count:
            return "New order count is {} and not {}".format(row.new_order_count, expected_count)

        return None

    def validate_order_lines(self, args, session):
        sql = """
            --!syntax_v1
            $s = SELECT OL_W_ID as warehouse, OL_D_ID as district, OL_O_ID as order
            FROM `order_line`
            GROUP BY OL_W_ID, OL_D_ID, OL_O_ID;

            SELECT warehouse, district, COUNT(order) as order_count
            FROM $s
            GROUP BY warehouse, district
            ORDER BY order_count ASC
            LIMIT 1
        """

        result_sets = session.transaction().execute(sql)
        if not result_sets[0].rows:
            return "No order lines found"

        row = result_sets[0].rows[0]
        if row.order_count != 3000:
            error = "Order lines count is {} and not 3000 in warehouse {} district {}".format(
                row.order_count, row.warehouse, row.district)

            warehouse = row.warehouse
            district = row.district
            orders = set()
            start = 1
            while start < 3000:
                end = start + 1000
                sql = """
                    --!syntax_v1
                    SELECT OL_W_ID as warehouse, OL_D_ID as district, OL_O_ID as order
                    FROM `order_line`
                    WHERE OL_W_ID == {w_id} AND OL_D_ID == {d_id} AND OL_O_ID >= {start} AND OL_O_ID < {end}
                    GROUP BY OL_W_ID, OL_D_ID, OL_O_ID
                    ORDER BY order;
                """.format(w_id=warehouse, d_id=district, start=start, end=end)
                result_sets = session.transaction().execute(sql)
                for row in result_sets[0].rows:
                    orders.add(row.order)
                start = end
            for order in range(1, 3001):
                if order not in orders:
                    print("Order {} is missing in warehouse {} district {}".format(order, warehouse, district))

            return error

        return None

    def validate_stock(self, args, session):
        sql = """
            --!syntax_v1
            SELECT COUNT(distinct S_W_ID) as warehouse_count FROM `stock`;
        """

        result_sets = session.transaction().execute(sql)
        if not result_sets[0].rows:
            return "No stock found (warehouse count is zero)"

        row = result_sets[0].rows[0]
        if row.warehouse_count != args.warehouse_count:
            return "Warehouse count is {} and not {}".format(row.warehouse_count, args.warehouse_count)

        sql = """
            --!syntax_v1
            SELECT S_W_ID as warehouse, COUNT(S_I_ID) as item_count
                FROM `/Root/db1/stock`
                GROUP BY S_W_ID
                ORDER BY item_count ASC
                LIMIT 1
        """

        result_sets = session.transaction().execute(sql)
        if not result_sets[0].rows:
            return "No stock found (no items)"

        row = result_sets[0].rows[0]
        if row.item_count != 100000:
            return "Stock count is {} and not 100000 in warehouse {}".format(row.item_count, row.warehouse)

        return None

    def validate_history(self, args, session):
        sql = """
            --!syntax_v1
            SELECT COUNT(*) as history_count FROM `history`;
        """

        result_sets = session.transaction().execute(sql)
        if not result_sets[0].rows:
            return "No history found"

        row = result_sets[0].rows[0]
        expected_count = args.warehouse_count * 30000
        if row.history_count != expected_count:
            return "History count is {} and not {}".format(row.history_count, expected_count)

        return None


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

    def run(self, args):
        self.scale_re = re.compile(r"^Scale Factor:\s*(\d+(\.\d+)?)$")
        self.start_measure_re = re.compile(r"^\[INFO\s*\] (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) \[main\].*Warmup complete, starting measurements.$")
        self.results_line_re = re.compile(r"^================RESULTS================")
        self.results_entry = re.compile(r".*\|\s*(\d+(\.\d+)?)%?\s*$")
        self.rate_re = re.compile(r"^Rate limited.*= (\d+(\.\d+)?) requests/sec \(throughput\), (\d+(\.\d+)?) requests/sec \(goodput\)$")

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

        if len(results_by_start_ts) > 1:
            min_start = results_by_start_ts[0].measure_start_ts
            max_start = results_by_start_ts[-1].measure_start_ts
            start_delta = max_start - min_start
            print(f"Delta between earliest and latest measurements start: {start_delta} seconds")

        transactions_dict = collections.defaultdict(
            lambda: Aggregator.Histogram([1, 5, 10, 50, 100, 500, 1000, 2000, 3000, 4000, 4500, 5000, 6000, 10000]))

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

        for transaction_name, stats in transactions_stats_dict.items():
            ok_count = stats['OK']
            failed_count = stats['FAILED']
            total_requests = stats['OK'] + stats['FAILED']
            failed_percent_str = ""
            if failed_count:
                failed_percent = round(failed_count * 100 / total_requests, 2)
                failed_percent_str = f" ({failed_percent}%)"

            print(f"{transaction_name}: OK: {ok_count}, FAILED: {failed_count}{failed_percent_str}")

        for transaction_name, histogram in transactions_dict.items():
            print(f"{transaction_name}:")
            for percentile in [50, 90, 95, 99, 99.9]:
                print(f"  {percentile}%: {histogram.percentile(percentile)} ms")

        print("\n*These results are not officially recognized TPC results and are not comparable with other TPC-C test results published on the TPC website")

    def process_raw_json(self, file, transactions_dict, transactions_stats_dict, start_ts):
        data = json.loads(file.read())
        for transaction_name, transaction_data in data.items():
            if transaction_name == "Invalid":
                continue
            transactions_stats_dict[transaction_name]["OK"] += transaction_data["SuccessCount"]
            transactions_stats_dict[transaction_name]["FAILED"] += transaction_data["FailureCount"]

            if len(transactions_dict[transaction_name]) == 0:
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
        result.time_seconds = float(m.group(1))

        line = file.readline()
        m = self.results_entry.match(line)
        if not m:
            raise Exception("Invalid results line2: {}".format(line))
        result.new_orders = int(m.group(1))

        line = file.readline()
        m = self.results_entry.match(line)
        if not m:
            raise Exception("Invalid results line3: {}".format(line))
        result.tpmc = float(m.group(1))

        line = file.readline()
        m = self.results_entry.match(line)
        if not m:
            raise Exception("Invalid results line4: {}".format(line))
        result.efficiency = float(m.group(1))

        line = file.readline()
        m = self.rate_re.match(line)
        if not m:
            raise Exception("Invalid results line5: {}".format(line))
        result.throughput = float(m.group(1))
        result.goodput = float(m.group(3))

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

    create_parser = subparsers.add_parser('create')
    create_parser.set_defaults(func=CreateTables().run)

    generate_config_parser = subparsers.add_parser('generate-configs')
    generate_config_parser.add_argument("--hosts", dest="hosts_file", required=True, help="File with hosts")
    generate_config_parser.add_argument("-i", "--input", dest="input", required=True, help="Input template file")

    generate_config_parser.add_argument("--ydb-host", required=True, help="Any YDB host")
    generate_config_parser.add_argument("--database", required=True, help="Path to database")

    generate_config_parser.add_argument("--loader-threads", dest="loader_threads",
                                        required=True, type=int, help="Loader threads per host")

    generate_config_parser.add_argument("--execute-time", dest="execute_time",
                                        required=True, help="Execute time in seconds")

    generate_config_parser.add_argument("--warmup-time", dest="warmup_time",
                                        required=True, help="Warmup time in seconds")

    generate_config_parser.add_argument("--max-sessions", dest="max_sessions",
                                        required=True, help="Max sessions per TPC-C instance")

    generate_config_parser.set_defaults(func=GenerateConfig().run)

    load_args_parser = subparsers.add_parser('get-load-args')
    load_args_parser.add_argument("--node-num", dest="node_num", required=True, type=int,
                             default=1, help="TPCC host number (1-based)")
    load_args_parser.set_defaults(func=GetLoadArgs().run)

    start_args_parser = subparsers.add_parser('get-start-args')
    start_args_parser.add_argument("--node-num", dest="node_num", required=True, type=int,
                             default=1, help="TPCC host number (1-based)")
    start_args_parser.set_defaults(func=GetStartArgs().run)

    index_parser = subparsers.add_parser('index')
    index_parser.set_defaults(func=AsyncCreateIndices().run)

    index_parser = subparsers.add_parser('wait-index')
    index_parser.set_defaults(func=WaitIndicesReady().run)

    validate_parser = subparsers.add_parser('validate')
    validate_parser.set_defaults(func=ValidateInitialData().run)

    drop_parser = subparsers.add_parser('drop')
    drop_parser.set_defaults(func=DropTables().run)

    aggregate_parser = subparsers.add_parser('aggregate')
    aggregate_parser.add_argument('results_dir', help="Directory with results")
    aggregate_parser.set_defaults(func=Aggregator().run)

    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
