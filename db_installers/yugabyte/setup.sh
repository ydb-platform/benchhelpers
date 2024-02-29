#!/bin/bash

set -e

usage() {
    echo "Usage: setup.sh --package <PATH_TO_YUGABYTE_PACKAGE>"
}

if ! command -v parallel-ssh &> /dev/null
then
    echo "'parallel-ssh' could not be found in your PATH. You can install it using the command: 'sudo apt install pssh'."
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

DEPLOY_TMP_PATH=$("$PATH_TO_SCRIPT"/control.py -c $YUGABYTE_CONFIG --deploy-tmp-path)
YUGABYTE_DEPLOY_PATH=$("$PATH_TO_SCRIPT"/control.py -c $YUGABYTE_CONFIG --deploy-path)
YUGABYTE_HOSTS=$("$PATH_TO_SCRIPT"/control.py -c $YUGABYTE_CONFIG --list-hosts)

if [[ ! -v YUGABYTE_HOSTS ]]; then
    echo "YUGABYTE_HOSTS is not set in config."
    exit 1
fi

parallel-scp -H "$YUGABYTE_HOSTS" -p 30 "$PATH_TO_SCRIPT/yugabyte_wrapper" "$DEPLOY_TMP_PATH"
parallel-ssh -H "$YUGABYTE_HOSTS" -p 30 "sudo rm -rf $YUGABYTE_DEPLOY_PATH; sudo mkdir -p $YUGABYTE_DEPLOY_PATH;
                                         sudo mv $DEPLOY_TMP_PATH/yugabyte_wrapper $YUGABYTE_DEPLOY_PATH"

echo "Deploy yugabyte"
"$PATH_TO_SCRIPT"/control.py -c $YUGABYTE_CONFIG --stop

"$PATH_TO_SCRIPT"/control.py -c $YUGABYTE_CONFIG --format
if [[ $? -ne 0 ]]; then
    echo "Failed to format disks"
    exit 1
fi

"$PATH_TO_SCRIPT"/control.py -c $YUGABYTE_CONFIG --deploy "$YUGABYTE_TAR"
if [[ $? -ne 0 ]]; then
    echo "Failed to deploy yugabyte"
    exit 1
fi

"$PATH_TO_SCRIPT"/control.py -c $YUGABYTE_CONFIG --start --tservers-per-host 2
if [[ $? -ne 0 ]]; then
    echo "Failed to start yugabyte"
    exit 1
fi

sleep 10s

IFS=', ' read -r -a HOSTS_LIST <<< "$YUGABYTE_HOSTS"

for index in "${!HOSTS_LIST[@]}"
do
  $debug ssh "${HOSTS_LIST[index]}" "pgrep -f 'yb-master'" > /dev/null
  if [[ "$?" -eq 1 ]]; then
    echo "ERROR: yb-master crashed on ${HOSTS_LIST[index]}"
  fi
  $debug ssh "${HOSTS_LIST[index]}" "pgrep -f 'yb-tserver'" > /dev/null
  if [[ "$?" -eq 1 ]]; then
    echo "ERROR: yb-tserver crashed on ${HOSTS_LIST[index]}"
  fi
done
