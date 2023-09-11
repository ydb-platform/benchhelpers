#!/bin/bash

log_dir="$HOME/tpcc_logs/ydb"
whlist="1000 2000 3000 4000"
sleep_minutes=10

args=()

while [[ "$#" > 0 ]]; do case $1 in
    --whlist)
        whlist=$2
        shift;;
    --sleep-minutes)
        sleep_minutes=$2
        shift;;
    *)
        args+=("$1")
        ;;
esac; shift; done

mkdir -p $log_dir

for wh in $whlist; do
    dt=`date +%Y%m%d_%H%M`
    log_file="$log_dir/run_${dt}_${wh}wh.log"
    echo "Logging into $log_file"

    ./run_ydb.sh --warehouses $wh ${args[@]} 2>&1 | tee $log_file

    # we need this to make sure that the next run will start with a clean state
    # as well as have easily separable graphs
    sleep ${sleep_minutes}m
done
