#!/bin/bash

filesize=2500G

ramp_time=2s
runtime=1m

iodepth=16

function run_fio {
    echo "-------------------------------------------------"
    echo "Running fio test: $ioengine $ioengine_args"
    echo "-------------------------------------------------"

    echo "write latency test 8K"
    sudo fio --name=write_latency_test \
    --filename="$filename" --filesize=$filesize \
    --time_based --ramp_time=$ramp_time --runtime=$runtime \
    --ioengine=$ioengine $ioengine_args --direct=1 --verify=0 --randrepeat=0 \
    --bs=8K --iodepth=$iodepth --rw=randwrite --iodepth_batch_submit=$iodepth  \
    --iodepth_batch_complete_max=$iodepth \
    --percentile_list="50:90:95:99:99.9"

    echo "read latency test 8K"
    sudo fio --name=read_latency_test \
    --filename="$filename" --filesize=$filesize \
    --time_based --ramp_time=$ramp_time --runtime=$runtime \
    --ioengine=$ioengine $ioengine_args --direct=1 --verify=0 --randrepeat=0 \
    --bs=8K --iodepth=$iodepth --rw=randread \
    --iodepth_batch_submit=$iodepth  --iodepth_batch_complete_max=$iodepth \
    --percentile_list="50:90:95:99:99.9"
}

while [[ "$#" > 0 ]]; do case $1 in
    --filename)
        filename="$2";
        shift;;
    *)
        echo "Unknown parameter passed: $1"
        usage
        exit 1;;
esac; shift; done


ioengine=libaio
ioengine_args=
run_fio

ioengine=io_uring
run_fio

ioengine=io_uring
ioengine_args="--hipri"
run_fio

ioengine=io_uring
ioengine_args="--sqthread_poll"
run_fio
