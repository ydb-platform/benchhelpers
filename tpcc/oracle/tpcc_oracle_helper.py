#!/usr/bin/env python3

import argparse
import collections
import sys

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
            num_nodes = 0
            for line in f:
                host = line.strip()
                if host != "":
                    num_nodes += 1

            if num_nodes == 0:
                print("No nodes found in {}".format(args.hosts_file), file=sys.stderr)
                sys.exit(1)

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

class GetCreateArgs:
    def run(self, args):
        s = f"--create=true --load=false --execute=false"
        print(s)

class GetLoadArgs:
    def run(self, args):
        host_config = HostConfig(
            args.warehouse_count,
            args.node_count,
            args.node_num)

        s = f"--create=false --load=true --execute=false --start-from-id {host_config.start_warehouse}"
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

    load_args_parser = subparsers.add_parser('get-create-args')
    load_args_parser.add_argument("--node-num", dest="node_num", required=True, type=int,
                             default=1, help="TPCC host number (1-based)")
    load_args_parser.set_defaults(func=GetCreateArgs().run)

    load_args_parser = subparsers.add_parser('get-load-args')
    load_args_parser.add_argument("--node-num", dest="node_num", required=True, type=int,
                             default=1, help="TPCC host number (1-based)")
    load_args_parser.set_defaults(func=GetLoadArgs().run)

    start_args_parser = subparsers.add_parser("get-start-args")
    start_args_parser.add_argument("--node-num", dest="node_num", required=True, type=int,
                             default=1, help="TPCC host number (1-based)")
    start_args_parser.set_defaults(func=GetStartArgs().run)

    args = parser.parse_args()

    args.func(args)


if __name__ == "__main__":
    main()
