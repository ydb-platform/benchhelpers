#!/bin/bash

usage() {
    echo "$0 --package <benchbase-ydb> --hosts <hosts_file>"
}

if [ ! -f "/usr/bin/parallel-scp" ]; then
    echo "/usr/bin/parallel-scp not found"
    exit 1
fi

if [ ! -f "/usr/bin/parallel-ssh" ]; then
    echo "/usr/bin/parallel-ssh not found"
    exit 1
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --package)
            shift
            package=$1
            ;;
        --hosts)
            shift
            hosts=$1
            ;;
        *)
            usage
            exit 1
            ;;
    esac
    shift
done

if [ -z "$package" ]; then
    echo "Package not specified"
    usage
    exit 1
fi

if [ -z "$hosts" ]; then
    echo "Hosts file not specified"
    usage
    exit 1
fi

if [ ! -f "$package" ]; then
    echo "Package $package not found"
    exit 1
fi

if [ ! -f "$hosts" ]; then
    echo "Hosts file $hosts not found"
    exit 1
fi

unique_hosts=`mktemp`
sort -u $hosts > $unique_hosts

/usr/bin/parallel-scp -h $unique_hosts $package $HOME
if [ $? -ne 0 ]; then
    echo "Failed to upload package $package to hosts $hosts"
    rm -f $unique_hosts
    exit 1
fi

/usr/bin/parallel-ssh -h $unique_hosts "tar -xzf `basename $package`"
if [ $? -ne 0 ]; then
    echo "Failed to extract package $package on hosts $hosts"
    rm -f $unique_hosts
    exit 1
fi

rm -f $unique_hosts
