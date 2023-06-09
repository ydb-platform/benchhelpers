#!/bin/bash

HOSTS="host1.com host2.com host3.com"

DISKS=(/dev/nvme0n1p2
       /dev/nvme1n1p2
       /dev/nvme2n1p2
       /dev/nvme3n1p2)

CONFIG_DIR="CLUSTER_CONFIGS_PATH"
YDB_SETUP_PATH=""

GRPC_PORT_BEGIN=1234
IC_PORT_BEGIN=2345
MON_PORT_BEGIN=3456

DYNNODE_COUNT=4
DYNNODE_TASKSET_CPU=(0-31 32-63 64-95 96-127)

DATABASE_NAME="db"

# <pool type>:<pool size>
STORAGE_POOLS="ssd:15"