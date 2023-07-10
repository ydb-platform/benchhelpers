#!/bin/bash

HOSTS="host1.com host2.com host3.com"

DISKS=(/dev/nvme0n1p2
       /dev/nvme1n1p2
       /dev/nvme2n1p2
       /dev/nvme3n1p2)

CONFIG_DIR="<PATH_TO_CONFIG>"
YDB_SETUP_PATH="/opt/ydb"

GRPC_PORT_BEGIN=2135
IC_PORT_BEGIN=19001
MON_PORT_BEGIN=8765

DYNNODE_COUNT=3
DYNNODE_TASKSET_CPU=(0-5 6-10 11-15)

DATABASE_NAME="/Root/db"

# <pool type>:<pool size>
STORAGE_POOLS="ssd:1"
