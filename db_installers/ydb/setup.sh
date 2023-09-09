#!/bin/bash

set -e

debug=

log() {
    echo "`date` SETUP: $@"
}

usage() {
    echo "Usage: setup.sh --ydbd <PATH_TO_YDBD_TAR> --ydbd-url <YDB_TAR_URL> --config <PATH_TO_SETUP_CONFIG> [--stop]"
}

if ! command -v parallel-ssh &> /dev/null
then
    echo "'parallel-ssh' could not be found in your PATH. You can install it using the command: 'sudo apt install pssh'."
    exit 1
fi

stop_ydb=0

while [[ $# -gt 0 ]]; do case $1 in
    --ydbd)
        ydbd_tar=$2
        shift;;
    --ydbd-url)
        ydbd_url=$2
        shift;;
    --config|-c)
        setup_config=$2
        shift;;
    --stop)
        stop_ydb=1;;
    --help|-h)
        usage
        exit;;
    *)
        usage
        exit;;
esac; shift; done

if [[ -z "$ydbd_tar" && -z "$ydbd_url" && -z $stop_ydb ]]; then
    echo "ERROR: you must specify either option: --ydbd, --ydbd-url, --stop"
    usage
    exit 1
fi

if [[ -z "$setup_config" ]]; then
    echo "ERROR: Setup config is not specified"
    usage
    exit 1
fi

if [[ ! -e "$setup_config" ]]; then
  log "Config file $setup_config doesn't exist"
  exit 1
fi

source $setup_config

if [[ -z $YDB_SETUP_PATH ]]; then
  log "YDB_SETUP_PATH is not specified in $setup_config"
  exit 1
fi

if [[ "$YDB_SETUP_PATH" != /* ]]; then
  log "YDB_SETUP_PATH must be absolute path"
  exit 1
fi

if [[ "$YDB_SETUP_PATH" == "/" ]]; then
  log "YDB_SETUP_PATH can't be root"
  exit 1
fi

eval "HOSTS_FILE=$HOSTS_FILE"
eval "CONFIG_DIR=$CONFIG_DIR"

if [[ ! -e "$HOSTS_FILE" ]]; then
  log "Hosts file $HOSTS_FILE doesn't exist"
  exit 1
fi

if [[ ! -e "$CONFIG_DIR" ]]; then
  log "Config dir $CONFIG_DIR doesn't exist"
  exit 1
fi

if [[ ! -e "$CONFIG_DIR/config.yaml" ]]; then
  log "Config file $CONFIG_DIR/config.yaml doesn't exist"
  exit 1
fi

if [[ ! -e "$CONFIG_DIR/config_dynnodes.yaml" ]]; then
  log "Config file $CONFIG_DIR/config_dynnodes.yaml doesn't exist"
  exit 1
fi

init_host=$(cat "$HOSTS_FILE" | head -1)

log "Stop YDB if it is running"

$debug parallel-ssh -h "$HOSTS_FILE" -t 0 -p 20 "sudo sh -c 'pkill ydbd; sleep 5; pkill -9 ydbd'" &>/dev/null || true

if [ $stop_ydb -eq 1 ]; then
  exit 0
fi

log "Deploy"

$debug parallel-ssh -i -h "$HOSTS_FILE" -t 0 -p 20 "\
  sudo rm -rf $YDB_SETUP_PATH;   \
  sudo mkdir -p $YDB_SETUP_PATH; \
  sudo chown $USER $YDB_SETUP_PATH; \
  mkdir $YDB_SETUP_PATH/cfg $YDB_SETUP_PATH/logs"

if [[ -n "$ydbd_tar" ]]; then
  tar_name=$(basename "$ydbd_tar")
  $debug parallel-scp -h "$HOSTS_FILE" -t 0 -p 20 "$ydbd_tar" "$YDB_SETUP_PATH"
elif [[ -n "$ydbd_url" ]]; then
  tar_name=$(basename $ydbd_url)
  $debug parallel-ssh -h "$HOSTS_FILE" -t 0 -p 20 "wget -q $ydbd_url -O $YDB_SETUP_PATH/$tar_name"
else
  echo "ERROR: ydbd tar or url is not specified"
  usage
  exit 1
fi

$debug parallel-ssh -i -h "$HOSTS_FILE" -t 0 -p 20 "\
  tar -xzf $YDB_SETUP_PATH/$tar_name --strip-component=1 -C $YDB_SETUP_PATH; \
  rm -f $YDB_SETUP_PATH/$tar_name"

$debug parallel-scp -h "$HOSTS_FILE" -t 0 -p 20 "$CONFIG_DIR"/config.yaml "$YDB_SETUP_PATH/cfg"
$debug parallel-scp -h "$HOSTS_FILE" -t 0 -p 20 "$CONFIG_DIR"/config_dynnodes.yaml "$YDB_SETUP_PATH/cfg"

log "Format disks"

for d in "${DISKS[@]}"; do
  $debug parallel-ssh -i -h "$HOSTS_FILE" -t 0 -p 20 "sudo LD_LIBRARY_PATH=$YDB_SETUP_PATH/lib $YDB_SETUP_PATH/bin/ydbd admin bs disk obliterate $d"
done

GRPC_PORT=$GRPC_PORT_BEGIN
IC_PORT=$IC_PORT_BEGIN
MON_PORT=$MON_PORT_BEGIN

NODE_BROKERS=$(cat "$HOSTS_FILE" | sed "s/.*/--node-broker &:$GRPC_PORT/" | tr '\n' ' ')

log "Start static nodes"

$debug parallel-ssh -h "$HOSTS_FILE" -t 0 -p 20 "sudo LD_LIBRARY_PATH=$YDB_SETUP_PATH/lib bash -c ' \
    taskset -c $STATIC_TASKSET_CPU nohup \
    $YDB_SETUP_PATH/bin/ydbd server --log-level 3 --tcp --yaml-config $YDB_SETUP_PATH/cfg/config.yaml \
    --grpc-port $((GRPC_PORT++)) --ic-port $((IC_PORT++)) --mon-port $((MON_PORT++)) --node static &>$YDB_SETUP_PATH/logs/static.log &'"
$debug sleep 1m

for host in `cat $HOSTS_FILE`; do
  $debug ssh $host "pgrep ydbd" > /dev/null
  if [[ "$?" -eq 1 ]]; then
    echo "ERROR: On $host the static node did not start"
    exit 1
  fi
done

log "Init BS"

$debug ssh "$init_host" \
    "sudo LD_LIBRARY_PATH=$YDB_SETUP_PATH/lib $YDB_SETUP_PATH/bin/ydbd admin blobstorage config init --yaml-file $YDB_SETUP_PATH/cfg/config.yaml"

log "Create storage pools"

$debug ssh "$init_host" \
    "sudo LD_LIBRARY_PATH=$YDB_SETUP_PATH/lib $YDB_SETUP_PATH/bin/ydbd admin database /Root/$DATABASE_NAME create $STORAGE_POOLS"

if [[ $DYNNODE_COUNT -gt ${#DYNNODE_TASKSET_CPU[@]} ]]; then
  echo "DYNNODE_COUNT is greater than DYNNODE_TASKSET_CPU. The values are equalized."
  DYNNODE_COUNT=${#DYNNODE_TASKSET_CPU[@]}
fi

for ind in $(seq 0 $(($DYNNODE_COUNT-1))); do
  log "Start dynnodes: $((ind+1))"
  $debug parallel-ssh -h "$HOSTS_FILE" -t 0 -p 20 "sudo bash -c ' \
      taskset -c ${DYNNODE_TASKSET_CPU[$ind]} nohup \
      sudo LD_LIBRARY_PATH=$YDB_SETUP_PATH/lib $YDB_SETUP_PATH/bin/ydbd server --log-level 3 --grpc-port $((GRPC_PORT++)) --ic-port $((IC_PORT++)) --mon-port $((MON_PORT++)) \
      --yaml-config  $YDB_SETUP_PATH/cfg/config_dynnodes.yaml \
      --tenant /Root/$DATABASE_NAME \
      $NODE_BROKERS \
      &>$YDB_SETUP_PATH/logs/dyn$((ind+1)).log &'"
done
$debug sleep 30s

if [[ -z "$debug" ]]; then
  expected_count=$((DYNNODE_COUNT+1))
  for host in `cat $HOSTS_FILE`; do
    ydbd_count=`ssh $host "pgrep ydbd | wc -l" 2> /dev/null`
    if [[ $ydbd_count -ne $expected_count ]]; then
      echo "ERROR: not all ydbd processes are running on ${HOSTS_LIST[index]}: $ydbd_count out of $expected_count"
    fi
  done
fi
