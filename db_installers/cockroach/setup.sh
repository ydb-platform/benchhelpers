#!/bin/bash

set -e

HA_PROXY_BIN="./haproxy"

usage() {
    echo "Usage: setup.sh --package <PATH_TO_COCKROACH_PACKAGE>"
}

if ! command -v pssh &> /dev/null
then
    echo "`pssh` could not be found in your PATH. You can install it using the command: `pip install pssh`."
    exit 1
fi

while [[ $# -gt 0 ]]; do case $1 in
    --package)
        COCKROACH_TAR=$2
        shift;;
    --config)
        COCKROACH_CONFIG=$2
        shift;;
    --ha-config)
        HA_PROXY_CONFIG=$2
        shift;;
    --ha-bin)
        HA_PROXY_BIN=$2
        shift;;
    --setup-path)
        COCKROACH_DEPLOY_PATH=$2
        shift;;
    --help|-h)
        usage
        exit 0
esac; shift; done

if [[ ! -e "$COCKROACH_TAR" ]]; then
    echo "No cockroach package in path: $COCKROACH_TAR"
    exit 1
fi

if [[ ! -e "$COCKROACH_CONFIG" ]]; then
    echo "No cockroach config in path: $COCKROACH_CONFIG"
    exit 1
fi

if [[ ! -e "$HA_PROXY_CONFIG" ]]; then
    echo "No ha proxy config in path: $HA_PROXY_CONFIG"
    exit 1
fi

if [[ ! -e "$HA_PROXY_BIN" ]]; then
    echo "No ha proxy bin in path: $HA_PROXY_BIN"
    exit 1
fi

COCKROACH_DEPLOY_PATH=$(./control.py -c $COCKROACH_CONFIG --deploy-path)
COCKROACH_HOSTS=$(./control.py -c $COCKROACH_CONFIG --list-hosts)
INIT_NODE=$(echo "$COCKROACH_HOSTS" | tr ' ' '\n' | head -1)

if [[ ! -v COCKROACH_HOSTS ]]; then
    echo "COCKROACH_HOSTS is not set in config."
    exit 1
fi

HA_PROXY_NODES=$YCSB_NODES
for node in $HA_PROXY_NODES; do
    HA_PROXY_NODES_WITH_PATH="$HA_PROXY_NODES_WITH_PATH ${node}:$HA_PROXY_SETUP_PATH"
done


echo "Deploy Cockroach"

pscp -H $COCKROACH_HOSTS -p 30 ./cockroach_wrapper $COCKROACH_DEPLOY_PATH
./control.py -c "$COCKROACH_CONFIG" --format
./control.py -c "$COCKROACH_CONFIG" --deploy "$COCKROACH_TAR"
./control.py -c "$COCKROACH_CONFIG" --start --per-disk-instance
./control.py -c "$COCKROACH_CONFIG" --init --hosts "$INIT_NODE"
sleep 10s


echo "Deploy HAProxy"

# !!! this assumes no other HAProxy on these slices
pssh -H $HA_PROXY_NODES -p 30 "sudo killall haproxy &>/dev/null"

pscp -H $HA_PROXY_NODES -p 30 "$HA_PROXY_BIN" $HA_PROXY_SETUP_PATH
pscp -H $HA_PROXY_NODES-p 30 "$HA_PROXY_CONFIG" $HA_PROXY_SETUP_PATH

echo "Start HAProxy"
HA_PROXY_CONFIG_NAME=`basename $HA_PROXY_CONFIG`
pssh -H $HA_PROXY_NODES -p 30 "sudo -u root sh -c 'ulimit -n 500000; cd $HA_PROXY_SETUP_PATH; pkill -9 haproxy; ./haproxy -q -D -f $HA_PROXY_CONFIG_NAME'"
