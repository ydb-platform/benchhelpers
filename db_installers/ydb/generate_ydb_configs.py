#!/usr/bin/env python3

import argparse
import os
import pathlib
import sys
import yaml


def generate_host_config_section(disk, disk_type):
    return {
        "host_configs": [
            {
                "host_config_id": 1,
                "drive": [
                    {
                        "path": disk,
                        "type": disk_type,
                    }
                ]
            },
        ]
    }


def generate_hosts_section(hosts_file):
    with open(hosts_file, 'r') as f:
        hosts = f.readlines()

    if len(hosts) == 0:
        print("File {} is empty".format(hosts_file))
        sys.exit(1)

    if len(hosts) % 3 != 0:
        print("File {} must contain a multiple of 3 hosts".format(hosts_file))
        sys.exit(1)

    hosts = [host.strip() for host in hosts]

    body = 1
    rack = 1

    hosts_description = []
    for host_num, host in enumerate(hosts):
        host_dict = {
            "host": host,
            "node_id": host_num + 1,
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

    return {
        "hosts": hosts_description,
    }


def generate_domains_section(num_hosts, disk_type):
    disk_kind = disk_type.lower()

    # we assume that there are n nodes spreaded evenly across 3 zones, each zone having n/3 nodes
    # first n/3 nodes are in zone-1, next n/3 nodes are in zone-2, last n/3 nodes are in zone-3

    per_zone = num_hosts // 3
    ring_nodes = []
    for zone in range(0, 3):
        ring_nodes += [zone * per_zone + i for i in range(1, 4)]

    return {
        "domains_config": {
            "domain": [
                {
                    "name": "Root",
                    "storage_pool_types": [
                        {
                            "kind": disk_kind,
                            "pool_config": {
                                "box_id": 1,
                                "erasure_species": "mirror-3-dc",
                                "kind": disk_kind,
                                "pdisk_filter": [
                                    {
                                        "property": [
                                            {
                                                "type": disk_type,
                                            },
                                        ],
                                    },
                                ],
                                "vdisk_kind": "Default",
                            },
                        },
                    ],
                },
            ],
            "security_config": {
                "enforce_user_token_requirement": False,
            },
            "state_storage": [
                {
                "ring": {
                    "node": ring_nodes,
                    "nto_select": len(ring_nodes),
                },
                "ssid": 1,
                },
            ]
        },
    }


def generate_blobstorage_section(num_hosts, disk_type, disk_path):
    per_zone = num_hosts // 3

    rings = []
    for zone in range(0, 3):
        domains = []
        for node_id in range(zone * per_zone + 1, (zone + 1) * per_zone + 1):
            vdisk_locations = [
                {
                    "node_id": node_id,
                    "pdisk_category": disk_type,
                    "path": disk_path,
                },
            ]
            domains.append({
                "vdisk_locations": vdisk_locations,
            })
        rings.append({
            "fail_domains": domains,
        })

    return {
        "blob_storage_config": {
            "service_set": {
                "groups": [
                    {
                        "erasure_species": "mirror-3-dc",
                        "rings": rings,
                    }
                ]
            }
        }
    }


def get_channel_profile_config(disk_type):
    disk_kind = disk_type.lower()
    return {
        "channel_profile_config": {
            "profile": [
                {
                    "channel": [
                        {
                            "erasure_species": "mirror-3-dc",
                            "pdisk_category": disk_type,
                            "storage_pool_kind": disk_kind,
                        },
                        {
                            "erasure_species": "mirror-3-dc",
                            "pdisk_category": disk_type,
                            "storage_pool_kind": disk_kind,
                        },
                        {
                            "erasure_species": "mirror-3-dc",
                            "pdisk_category": disk_type,
                            "storage_pool_kind": disk_kind,
                        },
                    ],
                    "profile_id": 0,
                },
            ],
        }
    }


def get_actor_system_config(node_type, cores):
    return {
        "actor_system_config": {
            "use_auto_config": True,
            "node_type": node_type,
            "cpu_count": cores,
        },
    }


def get_table_config():
    return {
        "table_service_config": {
            "sql_version": 1,
            "enable_kqp_data_query_stream_lookup": True,
            "enable_sequential_reads": True,
        }
    }


def get_cache_config():
    return {
        "shared_cache_config": {
            "memory_limit": 16_000_000_000,
        }
    }


def write_ydb_yaml(sections, path):
    with open(path, 'w') as f:
        for section in sections:
            f.write(yaml.dump(section, default_flow_style=False))
            f.write("\n")


def write_setup_config(disk, disk_type, hosts_num, storage_cores, compute_cores, hosts_file, output_dir):
    last_storage_core = storage_cores - 1
    start_compute_core = storage_cores
    last_compute_core = start_compute_core + compute_cores - 1

    pool_kind = disk_type.lower()

    vdisks_per_disk = 4
    disks_count = hosts_num
    replication = 3

    # calculate how many groups we are able to allocate and subtract 1 used for static group
    pool_size = hosts_num * 4 // 3 - 1

    config = f"""\
HOSTS_FILE="{hosts_file}"

DISKS=({disk})

CONFIG_DIR="{output_dir}"
YDB_SETUP_PATH="/opt/ydb"

GRPC_PORT_BEGIN=2135
IC_PORT_BEGIN=19001
MON_PORT_BEGIN=8765

STATIC_TASKSET_CPU="0-{last_storage_core}"

DYNNODE_COUNT=1
DYNNODE_TASKSET_CPU=({start_compute_core}-{last_compute_core})

DATABASE_NAME="db"

STORAGE_POOLS="{pool_kind}:{pool_size}"

"""

    with open(output_dir / "setup_config", 'w') as f:
        f.write(config)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hosts", help="Path to file containing the list of YDB hosts")
    parser.add_argument("--disk", help="Path to the disk on the VMs where YDB data will be stored", required=True)
    parser.add_argument("--disk-type", help="Type of disk on the VMs where YDB data will be stored", default="SSD")
    parser.add_argument("--output-dir", help="Directory where the generated YAML files will be stored", default="mydb/cluster_configs")
    parser.add_argument("--storage-cores", help="Number of storage cores", default=5)
    parser.add_argument("--compute-cores", help="Number of compute (dynnode) cores", default=9)

    args = parser.parse_args()

    if not os.path.isfile(args.hosts):
        print("File {} does not exist".format(args.hosts))
        sys.exit(1)

    hosts_config_section = generate_host_config_section(args.disk, args.disk_type)
    hosts_section = generate_hosts_section(args.hosts)

    host_count = len(hosts_section["hosts"])

    domains_section = generate_domains_section(host_count, args.disk_type)
    blobstorage_section = generate_blobstorage_section(host_count, args.disk_type, args.disk)
    channel_profile_config = get_channel_profile_config(args.disk_type)

    erasure_config = {
        "static_erasure": "mirror-3-dc",
    }

    storage_config = [
        erasure_config,
        hosts_config_section,
        hosts_section,
        domains_section,
        blobstorage_section,
        channel_profile_config,
        get_actor_system_config("STORAGE", args.storage_cores),
    ]

    compute_config = [
        erasure_config,
        hosts_config_section,
        hosts_section,
        domains_section,
        channel_profile_config,
        get_actor_system_config("COMPUTE", args.compute_cores),
        get_table_config(),
        get_cache_config(),
    ]

    output_dir = pathlib.Path(args.output_dir)
    os.makedirs(output_dir, exist_ok=True)

    write_ydb_yaml(storage_config, output_dir / "config.yaml")
    write_ydb_yaml(compute_config, output_dir / "config_dynnodes.yaml")

    write_setup_config(
        args.disk,
        args.disk_type,
        host_count,
        args.storage_cores,
        args.compute_cores,
        args.hosts,
        output_dir)


if __name__ == '__main__':
    main()
