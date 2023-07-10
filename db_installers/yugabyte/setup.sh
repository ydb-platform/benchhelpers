#!/bin/bash

set -e

usage() {
    echo "Usage: setup.sh --package <PATH_TO_YUGABYTE_PACKAGE>"
}

if ! command -v parallel-ssh &> /dev/null
then
    echo "'parallel-ssh' could not be found in your PATH. You can install it using the command: 'pip install parallel-ssh'."
    exit 1
fi

while [[ $# -gt 0 ]]; do case $1 in
    --package)
        YUGABYTE_TAR=$2
        shift;;
    --config)
        YUGABYTE_CONFIG=$2
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


PATH_TO_SCRIPT=$(dirname "$0")

YUGABYTE_DEPLOY_PATH=$("$PATH_TO_SCRIPT"/control.py -c $YUGABYTE_CONFIG --deploy-path)
YUGABYTE_HOSTS=$("$PATH_TO_SCRIPT"/control.py -c $YUGABYTE_CONFIG --list-hosts)

if [[ ! -v YUGABYTE_HOSTS ]]; then
    echo "YUGABYTE_HOSTS is not set in config."
    exit 1
fi

parallel-scp -H "$YUGABYTE_HOSTS" -p 30 "$PATH_TO_SCRIPT/yugabyte_wrapper" "~"
parallel-ssh -H "$YUGABYTE_HOSTS" -p 30 "sudo rm -rf $YUGABYTE_DEPLOY_PATH; sudo mkdir -p $YUGABYTE_DEPLOY_PATH; sudo mv ~/yugabyte_wrapper $YUGABYTE_DEPLOY_PATH"

echo "Deploy yugabyte"
"$PATH_TO_SCRIPT"/control.py -c $YUGABYTE_CONFIG --clean
"$PATH_TO_SCRIPT"/control.py -c $YUGABYTE_CONFIG --format
"$PATH_TO_SCRIPT"/control.py -c $YUGABYTE_CONFIG --deploy "$YUGABYTE_TAR"
"$PATH_TO_SCRIPT"/control.py -c $YUGABYTE_CONFIG --start #--per-disk-instance
sleep 10s
