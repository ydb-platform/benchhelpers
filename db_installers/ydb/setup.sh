#!/bin/bash

set -e

debug=

log() {
    echo "`date` SETUP: $@"
}

usage() {
    echo "Usage: setup.sh [--ydbd-tar <PATH_TO_YDBD_TAR> --config <PATH_TO_SETUP_CONFIG>]"
}

if ! command -v parallel-ssh &> /dev/null
then
    echo "'parallel-ssh' could not be found in your PATH. You can install it using the command: 'sudo apt install pssh'."
    exit 1
fi

STOP_YDB=0

while [[ $# -gt 0 ]]; do case $1 in
    --ydbd)
        YDBD_TAR=$2
        shift;;
    --config|-c)
        SETUP_CONFIG=$2
        shift;;
    --stop)
        STOP_YDB=1;;
    --help|-h)
        usage
        exit;;
    *)
        usage
        exit;;
esac; shift; done

if [[ ! -e "$YDBD_TAR" ]] && [ $STOP_YDB -eq 0 ]; then
    log "YDBD $YDBD_TAR is not exist"
    exit 1
fi

if [[ ! -e "$SETUP_CONFIG" ]]; then
  log "Config file $SETUP_CONFIG is not exist"
  exit 1
fi

source $SETUP_CONFIG

INIT_HOST=$(echo "$HOSTS" | tr ' ' '\n' | head -1)

echo "Stop"
$debug parallel-ssh -H "$HOSTS" -t 0 -p 20 "sudo sh -c 'pkill ydbd; sleep 5; pkill -9 ydbd; echo \"DONE\"'"

if [ $STOP_YDB -eq 1 ]; then
  exit 0
fi

echo "Deploy"
$debug parallel-ssh -H "$HOSTS" -t 0 -p 20 "sudo mkdir -p $YDB_SETUP_PATH/cfg $YDB_SETUP_PATH/logs"
$debug parallel-scp -H "$HOSTS" -t 0 -p 20 "$YDBD_TAR" "~"
$debug parallel-ssh -H "$HOSTS" -t 0 -p 20 "sudo tar -xzf ~/$(basename "$YDBD_TAR") --strip-component=1 -C $YDB_SETUP_PATH; \
                                            rm -f $(basename "$YDBD_TAR")"

$debug parallel-scp -H "$HOSTS" -t 0 -p 20 $CONFIG_DIR/config.yaml "~"
$debug parallel-ssh -H "$HOSTS" -t 0 -p 20 "sudo mv ~/config.yaml $YDB_SETUP_PATH/cfg"
$debug parallel-scp -H "$HOSTS" -t 0 -p 20 $CONFIG_DIR/config_dynnodes.yaml "~"
$debug parallel-ssh -H "$HOSTS" -t 0 -p 20 "sudo mv ~/config_dynnodes.yaml $YDB_SETUP_PATH/cfg"

echo "Format disks"
for d in "${DISKS[@]}"; do
  $debug parallel-ssh -H "$HOSTS" -t 0 -p 20 "sudo LD_LIBRARY_PATH=$YDB_SETUP_PATH/lib $YDB_SETUP_PATH/bin/ydbd admin bs disk obliterate $d"
done

GRPC_PORT=$GRPC_PORT_BEGIN
IC_PORT=$IC_PORT_BEGIN
MON_PORT=$MON_PORT_BEGIN

NODE_BROKERS=$(echo "$HOSTS" | tr ' ' '\n' | sed "s/.*/--node-broker &:$GRPC_PORT/" | tr '\n' ' ')

echo "Start static nodes"
$debug parallel-ssh -H "$HOSTS" -t 0 -p 20 "sudo LD_LIBRARY_PATH=$YDB_SETUP_PATH/lib bash -c 'nohup \
    $YDB_SETUP_PATH/bin/ydbd server --log-level 3 --tcp --yaml-config $YDB_SETUP_PATH/cfg/config.yaml \
    --grpc-port $((GRPC_PORT++)) --ic-port $((IC_PORT++)) --mon-port $((MON_PORT++)) --node static &>$YDB_SETUP_PATH/logs/static.log &'"
$debug sleep 1m

IFS=', ' read -r -a HOSTS_LIST <<< "$HOSTS"
for index in "${!HOSTS_LIST[@]}"
do
  $debug ssh "${HOSTS_LIST[index]}" "pgrep ydbd" > /dev/null
  if [[ "$?" -eq 1 ]]; then
    echo "ERROR: On ${HOSTS_LIST[index]} the static node did not start with this error:"
    $debug ssh "${HOSTS_LIST[index]}" "cat $YDB_SETUP_PATH/logs/static.log"
  fi
done


echo "Init BS"
$debug parallel-ssh -H "$INIT_HOST" -t 0 -p 20  \
    "sudo LD_LIBRARY_PATH=$YDB_SETUP_PATH/lib $YDB_SETUP_PATH/bin/ydbd admin blobstorage config init --yaml-file $YDB_SETUP_PATH/cfg/config.yaml"

$debug parallel-ssh -H "$INIT_HOST" -t 0 -p 20  \
    "sudo LD_LIBRARY_PATH=$YDB_SETUP_PATH/lib $YDB_SETUP_PATH/bin/ydbd admin database /Root/$DATABASE_NAME create $STORAGE_POOLS"

if [[ $DYNNODE_COUNT -gt ${#DYNNODE_TASKSET_CPU[@]} ]]; then
  echo "DYNNODE_COUNT is greater than DYNNODE_TASKSET_CPU. The values are equalized."
  DYNNODE_COUNT=${#DYNNODE_TASKSET_CPU[@]}
fi

for ind in $(seq 0 $(($DYNNODE_COUNT-1))); do
  echo "Start dynnodes: $((ind+1))"
  $debug parallel-ssh -H "$HOSTS" -t 0 -p 20 "sudo bash -c ' \
      taskset -c ${DYNNODE_TASKSET_CPU[$ind]} nohup \
      sudo LD_LIBRARY_PATH=$YDB_SETUP_PATH/lib $YDB_SETUP_PATH/bin/ydbd server --log-level 3 --grpc-port $((GRPC_PORT++)) --ic-port $((IC_PORT++)) --mon-port $((MON_PORT++)) \
      --yaml-config  $YDB_SETUP_PATH/cfg/config_dynnodes.yaml \
      --tenant /Root/$DATABASE_NAME \
      $NODE_BROKERS \
      &>$YDB_SETUP_PATH/logs/dyn$((ind+1)).log &'"
done
$debug sleep 30s

for index in "${!HOSTS_LIST[@]}"
do
  $debug ssh "${HOSTS_LIST[index]}" "pgrep -f 'ydbd server'" > /dev/null
  if [[ "$?" -eq 1 ]]; then
    echo "ERROR: On ${HOSTS_LIST[index]} the dynnodes did not start with this error:"
    $debug ssh "${HOSTS_LIST[index]}" "cat $YDB_SETUP_PATH/logs/dyn1.log"
  fi
done

