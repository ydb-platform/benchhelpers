#!/bin/bash
# Prepares the nodes for running the TPC-C benchmark

usage() {
    echo "setup_tpcc_nodes.sh --hosts <hosts_file>"
}

unique_hosts=

cleanup() {
    if [ -n "$unique_hosts" ]; then
        rm -f $unique_hosts
    fi
}

trap cleanup EXIT

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


script_path=`readlink -f "$0"`
script_dir=`dirname "$script_path"`
common_dir="$script_dir/../../common"

sudo apt-get install -yyq python3-pip pssh
if [[ $? -ne 0 ]]; then
    echo "Failed to install required packages"
    exit 1
fi

pip3 install ydb numpy requests
if [[ $? -ne 0 ]]; then
    echo "Failed to install required python packages"
    exit 1
fi

curl -sSL https://storage.yandexcloud.net/yandexcloud-ydb/install.sh | bash
if [[ $? -ne 0 ]]; then
    echo "Failed to install YDB client"
    exit 1
fi

"$common_dir"/copy_ssh_keys.sh --hosts "$hosts" &>/dev/null

"$common_dir"/install_java21.sh --hosts "$hosts"
if [[ $? -ne 0 ]]; then
    echo "Failed to install Java 21"
    exit 1
fi

"$script_dir"/upload_benchbase.sh --hosts "$hosts"
if [[ $? -ne 0 ]]; then
    echo "Failed to install tpcc"
    exit 1
fi

echo "TPC-C nodes are ready"
