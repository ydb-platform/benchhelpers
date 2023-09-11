#!/bin/bash
# This scripts setups mirror-3-DC YDB cluster. It assumes than
# - all nodes are the same and contain 1 disk
# - nodes are at least 16 cores and 32 GB RAM, in particular this matches AWS' c5d.4xlarge
# - each node will run both static and dynamic node
# - number of nodes >= 9, number of nodes divisible by 3
# - first n/3 nodes are in DC1, second n/3 nodes are in DC2, last n/3 nodes are in DC3


DISK_LABEL="/dev/disk/by-partlabel/ydb_disk_ssd_01"
YDBD_URL="https://storage.yandexcloud.net/ydb-benchmark-builds/ydb-23-3.tar.gz"

usage() {
    echo "setup_mirror_3_dc.sh --hosts <hosts_file> --disk <disk>"
}

tmp_dir=

cleanup() {
    if [ -n "$tmp_dir" ]; then
        rm -rf $tmp_dir
    fi
}

trap cleanup EXIT

if ! which parallel-ssh >/dev/null; then
    echo "parallel-ssh not found, you should install pssh"
    exit 1
fi

if ! which parallel-scp >/dev/null; then
    echo "parallel-ssh not found, you should install pssh"
    exit 1
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --hosts)
            shift
            hosts=$1
            ;;
        --disk)
            shift
            disk=$1
            ;;
        *)
            usage
            exit 1
            ;;
    esac
    shift
done

if [ -z "$hosts" ]; then
    echo "Hosts file not specified"
    usage
    exit 1
fi

if [ ! -f "$hosts" ]; then
    echo "Hosts file $hosts not found"
    exit 1
fi

if [ -z "$disk" ]; then
    echo "Disk not specified"
    usage
    exit 1
fi

script_path=`readlink -f "$0"`
script_dir=`dirname "$script_path"`
common_dir="$script_dir/../../common"

"$common_dir"/copy_ssh_keys.sh --hosts "$hosts"

"$common_dir"/partition_disk.sh --hosts "$hosts" --disk "$disk"

"$common_dir"/enable_transparent_hugepages.sh --hosts "$hosts"

parallel-ssh -h "$hosts" "sudo apt-get update; sudo apt-get install -yyq libaio1 libidn11"

tmp_dir=`mktemp -d`

"$script_dir"/generate_ydb_configs.py --disk $DISK_LABEL --output-dir $tmp_dir --hosts "$hosts"
if [[ $? -ne 0 ]]; then
    echo "Failed to generate configs"
    exit 1
fi

"$script_dir"/setup.sh --ydbd-url $YDBD_URL --config $tmp_dir/setup_config
if [[ $? -ne 0 ]]; then
    echo "Failed to setup YDB"
    exit 1
fi

echo "Congrats, your YDB cluster is ready!"
