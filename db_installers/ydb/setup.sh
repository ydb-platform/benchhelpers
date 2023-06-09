#!/bin/bash

debug=

log() {
    echo "`date` SETUP: $@"
}

usage() {
    echo "Usage: setup.sh [--ydbd <PATH_TO_YDBD>]"
}

if ! command -v pssh &> /dev/null
then
    echo "`pssh` could not be found in your PATH. You can install it using the command: `pip install pssh`."
    exit 1
fi

while [[ $# -gt 0 ]]; do case $1 in
    --ydbd)
        YDBD=$2
        shift;;
    --config)
        YDB_CONFIG=$2
        shift;;
    --help|-h)
        usage
        exit;;
    *)
        usage
        exit;;
esac; shift; done

if [[ ! -x "$YDBD" ]]; then
    log "missing $YDBD"
    exit 1
fi

source $YDB_CONFIG

INIT_HOST=$(echo "$HOSTS" | tr ' ' '\n' | head -1)

echo "Kill ydbd"
$debug pssh -H $HOSTS -p 20 "sudo killall -9 ydbd"

echo "mkdirs"
$debug pssh -H $HOSTS -p 20 "sudo rm -rf /opt/ydb/logs/*"

echo "Upload ydbd"
$debug pscp -H $HOSTS -p 20 $YDBD "$YDB_SETUP_PATH/ydb/bin"

echo "Format disks"
$debug pssh -H $HOSTS -p 20 "sudo sh -c 'for d in $DISKS; do sudo $YDB_SETUP_PATH/ydb/bin/ydbd admin bs disk obliterate \$d; done'"

echo "Upload config"
$debug pscp -H $HOSTS -p 20 $CONFIG_DIR/config.yaml "$YDB_SETUP_PATH/ydb/cfg"
$debug pscp -H $HOSTS -p 20 $CONFIG_DIR/config_dynnodes.yaml "$YDB_SETUP_PATH/ydb/cfg"

GRPC_PORT=$GRPC_PORT_BEGIN
IC_PORT=$IC_PORT_BEGIN
MON_PORT=$MON_PORT_BEGIN

NODE_BROKERS=$(echo "$HOSTS" | tr ' ' '\n' | sed "s/.*/--node-broker &:2134/")

echo "Start static nodes"
$debug pssh -H $HOSTS -p 20 "sudo bash -c 'nohup \
    $YDB_SETUP_PATH/ydb/bin/ydbd server --log-level 3 --tcp --yaml-config $YDB_SETUP_PATH/ydb/cfg/config.yaml \
    --grpc-port $((GRPC_PORT++)) --ic-port $((IC_PORT++)) --mon-port $((MON_PORT++)) --node static &>$YDB_SETUP_PATH/ydb/logs/static.log &'"
$debug sleep 10s

echo "Init BS"
$debug pssh -H $INIT_HOST -p 20  \
    "sudo $YDB_SETUP_PATH/ydb/bin/ydbd admin blobstorage config init --yaml-file $YDB_SETUP_PATH/ydb/cfg/config.yaml"

$debug pssh -H $INIT_HOST -p 20  \
    "sudo $YDB_SETUP_PATH/ydb/bin/ydbd admin database $DATABASE_NAME create $STORAGE_POOLS"

if [[ $DYNNODE_COUNT -gt ${#DYNNODE_TASKSET_CPU[@]} ]]; then
  echo "DYNNODE_COUNT is greater than DYNNODE_TASKSET_CPU. The values are equalized."
  DYNNODE_COUNT=${#DYNNODE_TASKSET_CPU[@]}
fi

for ind in $(seq 0 $(($DYNNODE_COUNT-1))); do
  echo "Start dynnodes: $(($ind+1))"
  $debug pssh -H $HOSTS -p 20 "sudo bash -c '\
      taskset -c ${DYNNODE_TASKSET_CPU[$ind]} nohup \
      $YDB_SETUP_PATH/ydb/bin/ydbd server --log-level 3 --grpc-port $((GRPC_PORT++)) --ic-port $((IC_PORT++)) --mon-port $((MON_PORT++)) \
      --yaml-config  $YDB_SETUP_PATH/ydb/cfg/config_dynnodes.yaml \
      --tenant $DATABASE_NAME \
      $NODE_BROKERS
      &>$YDB_SETUP_PATH/ydb/logs/dyn$(($ind+1)).log &'"
done
