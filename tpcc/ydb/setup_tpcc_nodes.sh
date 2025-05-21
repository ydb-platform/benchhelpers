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

function setup_python {
    if ! which pip3 >/dev/null; then
        sudo apt-get install -yyq python3-pip
        if [[ $? -ne 0 ]]; then
            echo "Failed to install pip3 using apt-get. Please install it manually"
            return 1
        fi
    else
        echo "pip3 already installed"
    fi

    venv_dir="$script_dir/venv"

    if [ ! -d $venv_dir ]; then
        python3 -m venv $venv_dir
        if [[ $? -ne 0 ]]; then
            echo "Faild to create virtual environment $venv_dir"
            return 1
        fi
    else
        echo "$venv_dir already exists. Skipping virtual environment creation"
    fi

    source "$venv_dir/bin/activate"
    if [[ $? -ne 0 ]]; then
        echo "Faild to activate virtual environment $venv_dir"
        return 1
    fi

    pip3 install ydb ydb[yc] numpy requests
    if [[ $? -ne 0 ]]; then
        echo "Failed to install python packages: ydb, numpy, requests. Please install it manually"
        return 1
    fi
}

unique_hosts=`mktemp`
sort -u $hosts > $unique_hosts

script_path=`readlink -f "$0"`
script_dir=`dirname "$script_path"`
common_dir="$script_dir/../../common"

if which apt-get >/dev/null; then
    sudo apt-get install -yyq pssh
    if [[ $? -ne 0 ]]; then
        echo "Failed to install pssh. Please install it manually"
        some_failed=1
    fi
else
    echo "apt-get not found. Please install pssh manually"
    some_failed=1
fi

setup_python

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
