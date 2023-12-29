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

if which apt-get >/dev/null; then
    sudo apt-get install -yyq python3-pip pssh
    if [[ $? -ne 0 ]]; then
        echo "Failed to install python3-pip pssh on. Please install it manually"
        some_failed=1
    fi
else
    echo "apt-get not found. Please install python3-pip pssh manually"
    some_failed=1
fi

if which pip3 >/dev/null; then
    pip3 install ydb numpy requests
    if [[ $? -ne 0 ]]; then
        echo "Failed to install python packages: ydb, numpy, requests. Please install it manually"
        some_failed=1
    fi
else
    echo "pip3 not found. Please install ydb, numpy and requests packages manually"
    some_failed=1
fi

curl -sSL https://storage.yandexcloud.net/yandexcloud-ydb/install.sh | bash
if [[ $? -ne 0 ]]; then
    echo "Failed to install YDB CLI. Please install it manually"
    some_failed=1
fi

"$common_dir"/copy_ssh_keys.sh --hosts "$hosts" &>/dev/null
if [[ $? -ne 0 ]]; then
    echo "Failed to copy ssh keys. Please copy them manually"
    some_failed=1
fi

"$common_dir"/install_java21.sh --hosts "$hosts"
if [[ $? -ne 0 ]]; then
    echo "Failed to install Java 21. Please install it manually"
    some_failed=1
fi

"$script_dir"/upload_benchbase.sh --hosts "$hosts"
if [[ $? -ne 0 ]]; then
    echo "Failed to install tpcc. Please install it manually"
    some_failed=1
fi

if [ -n "$some_failed" ]; then
    echo "Some steps failed. Please fix the issues manually"
    exit 1
fi

echo "TPC-C nodes are ready"
