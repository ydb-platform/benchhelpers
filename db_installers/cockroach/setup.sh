#!/bin/bash

HA_PROXY_BIN="./haproxy"

# i.e. homedir
YCSB_SETUP_PATH=""

# Don't change, value also used in cockroach_wrapper
COCKROACH_SETUP_PATH="/place/berkanavt/cockroach"

# TODO: make it configurable
YCSB_NODES="node1 node2 node3"

# TODO: take from cluster config (i.e. ./control.py -c "$COCKROACH_CONFIG" --list-hosts)
INIT_NODE=""
CLUSTER=""

usage() {
    echo "Usage: setup_vla_dev04.sh --package <PATH_TO_COCKROACH_PACKAGE>"
}

while [[ "$#" > 0 ]]; do case $1 in
    --package)
        COCKROACH_TAR=$2
        shift;;
    --config)
        COCKROACH_CONFIG=$2
        shift;;
    --ha-config)
        HA_PROXY_CONFIG=$2
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

if ! command -v pssh &> /dev/null
then
    echo "`pssh` could not be found in your PATH"
    exit 1
fi

HA_PROXY_NODES=$YCSB_NODES
for node in $HA_PROXY_NODES; do
    HA_PROXY_NODES_WITH_PATH="$HA_PROXY_NODES_WITH_PATH ${node}:$YCSB_SETUP_PATH"
done

for node in $YCSB_NODES; do
    YCSB_NODES_WITH_PATH="$YCSB_NODES_WITH_PATH ${node}:$YCSB_SETUP_PATH"
done

echo "Deploy Cockroach"

pssh scp -p 30 --no-bastion --no-yubikey ./cockroach_wrapper $CLUSTER:
pssh run -ap30 --no-bastion --no-yubikey "sudo mkdir -p $COCKROACH_SETUP_PATH; sudo mv cockroach_wrapper $COCKROACH_SETUP_PATH" $CLUSTER
./control.py -c "$COCKROACH_CONFIG" --format
./control.py -c "$COCKROACH_CONFIG" --deploy "$COCKROACH_TAR"
./control.py -c "$COCKROACH_CONFIG" --start --per-disk-instance
./control.py -c "$COCKROACH_CONFIG" --init --hosts "$INIT_NODE"
sleep 10s

echo "Deploy Cockroach to shooting nodes"
pssh scp -p 30 --no-bastion --no-yubikey "$COCKROACH_TAR" $YCSB_NODES_WITH_PATH
tar_name=`basename $COCKROACH_TAR`
dir_name=`echo $tar_name | sed 's/.tgz//'`
pssh run -ap 30 --no-bastion --no-yubikey "rm -rf cockroach; tar xzf $tar_name; mv $dir_name cockroach" $YCSB_NODES

echo "Deploy HAProxy"

# !!! this assumes no other HAProxy on these slices
pssh run -ap30 --no-bastion --no-yubikey "sudo killall haproxy &>/dev/null" $HA_PROXY_NODES

pssh scp -p 30 --no-bastion --no-yubikey "$HA_PROXY_BIN" $HA_PROXY_NODES_WITH_PATH
pssh scp -p 30 --no-bastion --no-yubikey "$HA_PROXY_CONFIG" $HA_PROXY_NODES_WITH_PATH

echo "Start HAProxy"
HA_PROXY_CONFIG_NAME=`basename $HA_PROXY_CONFIG`
pssh run -ap30 --no-bastion --no-yubikey  "sudo -u root sh -c 'ulimit -n 500000; cd $YCSB_SETUP_PATH; pkill -9 haproxy; ./haproxy -q -D -f $HA_PROXY_CONFIG_NAME'" $HA_PROXY_NODES
