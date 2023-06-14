#!/bin/bash

set -e

usage() {
    echo "Usage: setup.sh --package <PATH_TO_COCKROACH_PACKAGE> --config <PATH_TO_CLUSTER_CONFIG> --ha-bin <PATH_TO_HA_PROXY_BIN>"
}

if ! command -v parallel-ssh &> /dev/null
then
    echo "'parallel-ssh' could not be found in your PATH. You can install it using the command: 'sudo apt install pssh'."
    exit 1
fi

while [[ $# -gt 0 ]]; do case $1 in
    --package)
        COCKROACH_TAR=$2
        shift;;
    --config)
        COCKROACH_CONFIG=$2
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

if [[ ! -x "$HA_PROXY_BIN" ]]; then
    echo "No ha proxy bin in path: $HA_PROXY_BIN"
    exit 1
fi


PATH_TO_SCRIPT=$(dirname "$0")

COCKROACH_DEPLOY_PATH=$("$PATH_TO_SCRIPT"/control.py -c $COCKROACH_CONFIG --deploy-path)
COCKROACH_NODES=$("$PATH_TO_SCRIPT"/control.py -c $COCKROACH_CONFIG --list-hosts)
COCKROACH_PORT=$("$PATH_TO_SCRIPT"/control.py -c $COCKROACH_CONFIG --listen-port)
HA_PROXY_NODES=$("$PATH_TO_SCRIPT"/control.py -c $COCKROACH_CONFIG --ha-proxy-hosts)
HA_PROXY_SETUP_PATH=$("$PATH_TO_SCRIPT"/control.py -c $COCKROACH_CONFIG --ha-proxy-setup-path)
INIT_NODE=$(echo "$COCKROACH_NODES" | tr ' ' '\n' | head -1)


if [[ ! -v COCKROACH_NODES ]]; then
    echo "COCKROACH_NODES is not set in config."
    exit 1
fi

for node in $HA_PROXY_NODES; do
    HA_PROXY_NODES_WITH_PATH="$HA_PROXY_NODES_WITH_PATH ${node}:$HA_PROXY_SETUP_PATH"
done


echo "Deploy Cockroach"

parallel-ssh -H "$COCKROACH_NODES $HA_PROXY_NODES" -p 30 "sudo mkdir -p $COCKROACH_DEPLOY_PATH/logs"
parallel-scp -H "$COCKROACH_NODES $HA_PROXY_NODES" -p 30 $PATH_TO_SCRIPT/cockroach_wrapper $COCKROACH_DEPLOY_PATH

"$PATH_TO_SCRIPT"/control.py -c "$COCKROACH_CONFIG" --format
"$PATH_TO_SCRIPT"/control.py -c "$COCKROACH_CONFIG" --deploy "$COCKROACH_TAR" --hosts "$COCKROACH_NODES $HA_PROXY_NODES"
"$PATH_TO_SCRIPT"/control.py -c "$COCKROACH_CONFIG" --start --per-disk-instance
"$PATH_TO_SCRIPT"/control.py -c "$COCKROACH_CONFIG" --init --hosts "$INIT_NODE"
sleep 10s


echo "Deploy HAProxy"

# !!! this assumes no other HAProxy on these slices
parallel-ssh -H "$HA_PROXY_NODES" -p 30 "sudo killall haproxy &>/dev/null; sudo mkdir -p $HA_PROXY_SETUP_PATH"

parallel-scp -H "$HA_PROXY_NODES" -p 30 "$HA_PROXY_BIN" $HA_PROXY_SETUP_PATH

# generate haproxy.cfg
parallel-ssh -H "$HA_PROXY_NODES" -p 30 "cd $HA_PROXY_SETUP_PATH; $COCKROACH_DEPLOY_PATH/cockroach gen haproxy --insecure --host=$INIT_NODE --port=$COCKROACH_PORT"


echo "Start HAProxy"
HA_PROXY_CONFIG_NAME="haproxy.cfg"
parallel-ssh -H "$HA_PROXY_NODES" -p 30 "sudo -u root sh -c 'ulimit -n 500000; cd $HA_PROXY_SETUP_PATH; pkill -9 haproxy; $HA_PROXY_SETUP_PATH/haproxy -q -D -f $HA_PROXY_CONFIG_NAME'"
