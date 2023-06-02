#!/bin/bash

# default configs, normally should always setup with it
YUGABYTE_CQL="./yugabyte_cql.sql"
YUGABYTE_SQL="./yugabyte_sql.sql"

# TODO: make it configurable, option to download it
YCSB_TAR="ycsb-yugabyteCQL-binding-0.18.0-SNAPSHOT.tar.gz"
YCSB_TAR2="ycsb-yugabyteSQL-binding-0.18.0-SNAPSHOT.tar.gz"

# i.e. homedir
YCSB_SETUP_PATH=""

YCSB_NODES="node1 node2 node3"

# TODO: take from config
INIT_NODE=""
CLUSTER=""

usage() {
    echo "Usage: setup_vla_dev04.sh --package <PATH_TO_YUGABYTE_PACKAGE>"
}

while [[ "$#" > 0 ]]; do case $1 in
    --package)
        YUGABYTE_TAR=$2
        shift;;
    --config)
        YUGABYTE_CONFIG=$2
        shift;;
    --props)
        YCSB_PROPS=$2
        shift;;
    --cluster)
        CLUSTER=$2
        shift;;
    --help|-h)
        usage
        exit 0
esac; shift; done

if [[ ! -e "$YUGABYTE_TAR" ]]; then
    echo "No yugabyte package in path: $YUGABYTE_TAR"
    exit 1
fi

if [[ ! -e "$YUGABYTE_CONFIG" ]]; then
    echo "No yugabyte config in path: $YUGABYTE_CONFIG"
    exit 1
fi

if ! command -v pssh &> /dev/null
then
    echo "`pssh` could not be found in your PATH"
    exit 1
fi

for node in $YCSB_NODES; do
    YCSB_NODES_WITH_PATH="$YCSB_NODES_WITH_PATH ${node}:$YCSB_SETUP_PATH"
done

pssh scp -p 30 --no-bastion --no-yubikey ./yugabyte_wrapper $CLUSTER:
pssh run -ap30 --no-bastion --no-yubikey "sudo rm -rf /place/berkanavt/yugabyte; sudo mkdir -p /place/berkanavt/yugabyte; sudo mv yugabyte_wrapper /place/berkanavt/yugabyte/" $CLUSTER

echo "Deploy yugabyte"
./control.py -c $YUGABYTE_CONFIG --clean
./control.py -c $YUGABYTE_CONFIG --format
./control.py -c $YUGABYTE_CONFIG --deploy "$YUGABYTE_TAR"
./control.py -c $YUGABYTE_CONFIG --start #--per-disk-instance
sleep 10s

echo "Deploy Yugabyte's YCSB to shooting nodes"

pssh scp -p 30 --no-bastion --no-yubikey "$YCSB_TAR" "$YCSB_TAR2" "$YCSB_PROPS" $YCSB_NODES_WITH_PATH
tar_name=`basename $YCSB_TAR`
tar_name2=`basename $YCSB_TAR2`
dir_name=`basename -s .tar.gz $YCSB_TAR`
dir_name2=`basename -s .tar.gz $YCSB_TAR2`
props_name=`basename $YCSB_PROPS`
pssh run -ap 30 --no-bastion --no-yubikey "tar xzf $tar_name; tar xzf $tar_name2; cp $props_name ./$dir_name/db.properties; mv $props_name ./$dir_name2/db.properties" $YCSB_NODES

sql_name=`basename $YUGABYTE_CQL`
pssh scp -p 1 --no-bastion --no-yubikey "$YUGABYTE_CQL" ${INIT_NODE}:

sql_name=`basename $YUGABYTE_SQL`
pssh scp -p 1 --no-bastion --no-yubikey "$YUGABYTE_SQL" ${INIT_NODE}:
