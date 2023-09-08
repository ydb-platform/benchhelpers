#!/usr/bin/env python3

import argparse
import os
import sys
import yaml


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("hosts", help="Path to file containing the list of YDB hosts")

    args = parser.parse_args()

    if not os.path.isfile(args.hosts):
        print("File {} does not exist".format(args.hosts))
        sys.exit(1)

    with open(args.hosts, 'r') as f:
        hosts = f.readlines()

    if len(hosts) == 0:
        print("File {} is empty".format(args.hosts))
        sys.exit(1)

    if len(hosts) % 3 != 0:
        print("File {} must contain a multiple of 3 hosts".format(args.hosts))
        sys.exit(1)

    hosts = [host.strip() for host in hosts]

    body = 1
    rack = 1

    hosts_description = []
    for host_num, host in enumerate(hosts):
        host_dict = {
            "host": host,
            "host_config_id": 1,
            "location": {
                "body": body,
                "rack": str(rack),
                "data_center": "zone-" + str(host_num // 3 + 1),
            }
        }
        body += 1
        rack += 1
        hosts_description.append(host_dict)

    hosts_section = {
        "hosts": hosts_description,
    }

    print(yaml.dump(hosts_section, default_flow_style=False))

if __name__ == '__main__':
    main()
