#!/bin/bash

usage() {
    echo "Usage: setup.sh --package <PATH_TO_YUGABYTE_PACKAGE>"
}

if ! command -v pssh &> /dev/null
then
    echo "`pssh` could not be found in your PATH. You can install it using the command: `pip install pssh`."
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


YUGABYTE_DEPLOY_PATH=$(./control.py -c $YUGABYTE_CONFIG --deploy-path)
YUGABYTE_HOSTS=$(./control.py -c $YUGABYTE_CONFIG --list-hosts)

if [[ ! -v YUGABYTE_HOSTS ]]; then
    echo "YUGABYTE_HOSTS is not set in config."
    exit 1
fi

pscp -H $YUGABYTE_HOSTS -p 30 "./yugabyte_wrapper" "$YUGABYTE_DEPLOY_PATH"
pssh -H $YUGABYTE_HOSTS -p 30 "sudo rm -rf /place/berkanavt/yugabyte; sudo mkdir -p /place/berkanavt/yugabyte; sudo mv $YUGABYTE_DEPLOY_PATH/yugabyte_wrapper /place/berkanavt/yugabyte/"

echo "Deploy yugabyte"
./control.py -c $YUGABYTE_CONFIG --clean
./control.py -c $YUGABYTE_CONFIG --format
./control.py -c $YUGABYTE_CONFIG --deploy "$YUGABYTE_TAR"
./control.py -c $YUGABYTE_CONFIG --start #--per-disk-instance
sleep 10s

