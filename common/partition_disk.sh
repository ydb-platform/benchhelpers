#!/bin/bash
# Partition disk for YDB on all hosts in the cluster

usage() {
    echo "partition_disk.sh --hosts <hosts_file> --disk <disk>"
}

unique_hosts=

cleanup() {
    if [ -n "$unique_hosts" ]; then
        rm -f $unique_hosts
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

unique_hosts=`mktemp`
sort -u $hosts > $unique_hosts

parallel-ssh -h $unique_hosts -i "sudo parted ${disk} mklabel gpt -s"
if [ $? -ne 0 ]; then
    echo "Failed to create partition table on $disk"
    exit 1
fi

parallel-ssh -h $unique_hosts -i "sudo parted -a optimal ${disk} mkpart primary 0% 100%"
if [ $? -ne 0 ]; then
    echo "Failed to create partition on $disk"
    exit 1
fi

parallel-ssh -h $unique_hosts -i "sudo parted ${disk} name 1 ydb_disk_ssd_01"
if [ $? -ne 0 ]; then
    echo "Failed to name partition on $disk"
    exit 1
fi

parallel-ssh -h $unique_hosts -i "sudo partx --u ${disk}"
if [ $? -ne 0 ]; then
    echo "Failed to update partition table on $disk"
    exit 1
fi

echo "created /dev/disk/by-partlabel/ydb_disk_ssd_01"
