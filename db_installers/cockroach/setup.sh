#!/bin/bash

set -e

usage() {
    echo "Usage: setup.sh --package <PATH_TO_COCKROACH_PACKAGE> --config <PATH_TO_CLUSTER_CONFIG> [--ha-bin <PATH_TO_HA_PROXY_BIN>] [--user <USER>]"
}

if ! command -v parallel-ssh &> /dev/null
then
    echo "'parallel-ssh' could not be found in your PATH. You can install it using the command: 'sudo apt install pssh'."
    exit 1
fi

user=$(whoami)

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
    --user)
        user=$2
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

if [ -n "$INIT_PER_DISK" ] || [ "$INIT_PER_DISK" != "0" ]; then
  START_ARGS="--per-disk-instance"
fi

PATH_TO_SCRIPT=$(dirname "$0")

DEPLOY_TMP_PATH=$("$PATH_TO_SCRIPT"/control.py -c $COCKROACH_CONFIG --deploy-tmp-path)
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

NODES=$(echo "$COCKROACH_NODES $HA_PROXY_NODES" | tr ' ' '\n' | sort -u)

parallel-scp --user $user -H "$NODES" -p 30 "$PATH_TO_SCRIPT"/cockroach_wrapper "$DEPLOY_TMP_PATH"
parallel-ssh --user $user -H "$NODES" -p 30 "sudo mkdir -p $COCKROACH_DEPLOY_PATH/logs;
                                                sudo mv $DEPLOY_TMP_PATH/cockroach_wrapper $COCKROACH_DEPLOY_PATH"

"$PATH_TO_SCRIPT"/control.py --ssh-user $user -c "$COCKROACH_CONFIG" --clean
"$PATH_TO_SCRIPT"/control.py --ssh-user $user -c "$COCKROACH_CONFIG" --format
"$PATH_TO_SCRIPT"/control.py --ssh-user $user -c "$COCKROACH_CONFIG" --deploy "$COCKROACH_TAR" --hosts "$NODES"
"$PATH_TO_SCRIPT"/control.py --ssh-user $user -c "$COCKROACH_CONFIG" --start "$START_ARGS"
"$PATH_TO_SCRIPT"/control.py --ssh-user $user -c "$COCKROACH_CONFIG" --init --hosts "$INIT_NODE"
sleep 10s

if [[ -n "$HA_PROXY_BIN" ]]; then
    echo "Deploy HAProxy"

    if [[ ! -x "$HA_PROXY_BIN" ]]; then
        echo "No ha proxy bin in path: $HA_PROXY_BIN"
        exit 1
    fi

    parallel-ssh --user $user -H "$HA_PROXY_NODES" -p 30 "sudo mkdir -p $HA_PROXY_SETUP_PATH"

    # !!! this assumes no other HAProxy on these slices
    if [ -e "$HA_PROXY_BIN" ]; then
      parallel-scp --user $user --user $user -H "$HA_PROXY_NODES" -p 30 "$HA_PROXY_BIN" "$DEPLOY_TMP_PATH";
      parallel-ssh --user $user -H "$HA_PROXY_NODES" -p 30 "sudo mv $DEPLOY_TMP_PATH/$(basename $HA_PROXY_BIN) $HA_PROXY_SETUP_PATH"
    fi

    # generate haproxy.cfg
    parallel-ssh --user $user -H "$HA_PROXY_NODES" -p 30 "cd $HA_PROXY_SETUP_PATH; sudo $COCKROACH_DEPLOY_PATH/cockroach gen haproxy --insecure --host=$INIT_NODE --port=$COCKROACH_PORT"

    # change maxconn to 250000
    parallel-ssh --user $user -H "$HA_PROXY_NODES" -p 30 "echo \"\$(cat $HA_PROXY_SETUP_PATH/haproxy.cfg | sed '/[space]*maxconn/s/4096/250000/' | sed 's/defaults/defaults\n    maxconn\t\t250000/')\" | sudo tee $HA_PROXY_SETUP_PATH/haproxy.cfg"


    if [ -n "$HA_PROXY_SETUP_PATH" ]; then
      HA_PROXY="./haproxy"
    else
      HA_PROXY="haproxy"
    fi

    echo "Start HAProxy"
    HA_PROXY_CONFIG_NAME="haproxy.cfg"
    parallel-ssh --user $user -H "$HA_PROXY_NODES" -p 30 "sudo -u root sh -c 'ulimit -n 500000; cd $HA_PROXY_SETUP_PATH; $HA_PROXY -q -D -f $HA_PROXY_CONFIG_NAME'"
    sleep 5s

    IFS=', ' read -r -a HOSTS_LIST <<< "$HA_PROXY_NODES"

    for index in "${!HOSTS_LIST[@]}"
    do
      $debug ssh "${HOSTS_LIST[index]}" "pgrep -f 'haproxy'" > /dev/null
      if [[ "$?" -eq 1 ]]; then
        echo "ERROR: haproxy crashed on ${HOSTS_LIST[index]}"
      fi
    done
fi

echo "Cockroach setup complete"
