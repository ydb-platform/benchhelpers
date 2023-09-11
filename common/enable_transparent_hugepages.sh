#!/bin/bash
# Enables transparant hugepages on the specified hosts


usage() {
    echo "enable_transparent_hugepages.sh --hosts <hosts_file>"
}

unique_hosts=

cleanup() {
    if [ -n "$unique_hosts" ]; then
        rm -f $unique_hosts
    fi
}

trap cleanup SIGINT SIGTERM

if ! which parallel-ssh >/dev/null; then
    echo "parallel-ssh not found, you should install pssh"
    exit 1
fi

while [ $# -gt 0 ]; do
    case "$1" in
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

if [ -z "$hosts" ]; then
    echo "Hosts file not specified"
    usage
    exit 1
fi

if [ ! -f "$hosts" ]; then
    echo "Hosts file $hosts not found"
    exit 1
fi

unique_hosts=`mktemp`
sort -u $hosts > $unique_hosts
some_host=`head -n 1 $unique_hosts`

parallel-ssh -h $unique_hosts "echo always | sudo tee /sys/kernel/mm/transparent_hugepage/enabled"
