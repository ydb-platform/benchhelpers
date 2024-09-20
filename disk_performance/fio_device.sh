#!/bin/bash

# based on https://cloud.google.com/compute/docs/disks/benchmarking-pd-performance

filesize=2500G

numjobs=16

ramp_time=2s
runtime=1m

bandwidth_depth=64
iops_depth=256

format=json

# NOTE: we don't check if the file is big enough for the test
multi_stream_seq_test_offset=100G
seq_test_offset=500G

ioengine=libaio
ioengine_args=

results_dir="."

usage() {
    echo "Usage: $0"
    echo "  --filename <filename> "
    echo "  [--filesize <filesize>] (default: $filesize)"
    echo "  [--numjobs <numjobs>] (default: $numjobs)"
    echo "  [--results-dir <results-dir>] (default: $results_dir)"
    echo "  [--skip-fill-disk] (default: false)"
    echo "  [--ioengine] (default $ioengine)"
    echo "  [--ioengine-args] (default NONE)"
    echo "  [--bandwidth-depth <bandwidth-depth>] (default: $bandwidth_depth)"
    echo "  [--iops-depth <iops-depth>] (default: $iops_depth)"
    echo "  [--format <format>] (default: $format)"
}

if ! which fio >/dev/null; then
    echo "fio not found, you should install it: sudo apt-get update; sudo apt-get install -y fio"
    exit 1
fi

while [[ "$#" > 0 ]]; do case $1 in
    --filename)
        filename="$2";
        shift;;
    --filesize)
        filesize="$2";
        shift;;
    --numjobs)
        numjobs="$2";
        shift;;
    --results-dir)
        results_dir="$2";
        shift;;
    --skip-fill-disk)
        skip_fill_disk=true
        ;;
    --ioengine)
        ioengine="$2";
        shift;;
    --ioengine-args)
        ioengine_args="$2";
        shift;;
    --bandwidth-depth)
        bandwidth_depth="$2";
        shift;;
    --iops-depth)
        iops_depth="$2";
        shift;;
    --format)
        format="$2";
        shift;;
    --help|-h)
        usage
        exit;;
    *)
        echo "Unknown parameter passed: $1"
        usage
        exit 1;;
esac; shift; done

if [[ -z "$filename" ]]; then
    echo "filename is required"
    usage
    exit 1
fi

if [[ ! -e "$filename" ]]; then
    echo "file $filename not exists"
    exit 1
fi

if [[ ! -d "$results_dir" ]]; then
    mkdir -p "$results_dir"
    if [[ $? -ne 0 ]]; then
        echo "failed to create results dir $results_dir"
        exit 1
    fi
fi

#
# Check if we can use the file
#

sudo lsof "$filename" 1>/dev/null 2>/dev/null
if [[ $? -eq 0 ]]; then
    echo "file $filename is opened by another process"
    exit 1
fi

# not sure if it's needed, but sanity checks are good anyway
mount | grep "$filename" 1>/dev/null 2>/dev/null
if [[ $? -eq 0 ]]; then
    echo "file $filename is mounted"
    exit 1
fi

#
# fill the disk with random data
#

if [[ -z "$skip_fill_disk" ]]; then
    sudo fio --name=fill_disk \
    --filename="$filename" --filesize=$filesize \
    --ioengine=$ioengine $ioengine_args --direct=1 --verify=0 --randrepeat=0 \
    --bs=128K \
    --rw=randwrite \
    --iodepth=64 \
    --iodepth_batch_submit=64  \
    --iodepth_batch_complete_max=64 \
    1>/dev/null 2>/dev/null

    if [[ $? -ne 0 ]]; then
        echo "fill_disk failed"
        exit 1
    fi
fi

#
# write bandwidth test
#
sudo fio --name=write_bandwidth_test \
  --filename="$filename" --filesize=$filesize \
  --time_based --ramp_time=$ramp_time --runtime=$runtime \
  --ioengine=$ioengine $ioengine_args --direct=1 --verify=0 --randrepeat=0 \
  --bs=1M \
  --iodepth=64 \
  --iodepth_batch_submit=64 \
  --iodepth_batch_complete_max=64 \
  --rw=write \
  --numjobs=$numjobs \
  --offset_increment=$multi_stream_seq_test_offset \
  --output-format=$format \
  --output="$results_dir/write_bandwidth_test.$format"

#
# write IOPS test 4K
#
sudo fio --name=write_iops_test \
  --filename="$filename" --filesize=$filesize \
  --time_based --ramp_time=$ramp_time --runtime=$runtime \
  --ioengine=$ioengine $ioengine_args --direct=1 --verify=0 --randrepeat=0 \
  --bs=4K --iodepth=256 --rw=randwrite \
  --iodepth_batch_submit=256  --iodepth_batch_complete_max=256 \
  --output-format=$format \
  --output="$results_dir/write_iops_test.$format"

#
# write IOPS test 8K
#
sudo fio --name=write_iops_test \
  --filename="$filename" --filesize=$filesize \
  --time_based --ramp_time=$ramp_time --runtime=$runtime \
  --ioengine=$ioengine $ioengine_args --direct=1 --verify=0 --randrepeat=0 \
  --bs=8K --iodepth=256 --rw=randwrite \
  --iodepth_batch_submit=256  --iodepth_batch_complete_max=256 \
  --output-format=$format \
  --output="$results_dir/write_iops_test_8K.$format"

#
# write latency test 4K
#
sudo fio --name=write_latency_test \
  --filename="$filename" --filesize=$filesize \
  --time_based --ramp_time=$ramp_time --runtime=$runtime \
  --ioengine=$ioengine $ioengine_args --direct=1 --verify=0 --randrepeat=0 \
  --bs=4K --iodepth=4 --rw=randwrite --iodepth_batch_submit=4  \
  --iodepth_batch_complete_max=4 \
  --output-format=$format \
  --output="$results_dir/write_latency_test.$format"

#
# write latency test 8K
#
sudo fio --name=write_latency_test \
  --filename="$filename" --filesize=$filesize \
  --time_based --ramp_time=$ramp_time --runtime=$runtime \
  --ioengine=$ioengine $ioengine_args --direct=1 --verify=0 --randrepeat=0 \
  --bs=8K --iodepth=4 --rw=randwrite --iodepth_batch_submit=4  \
  --iodepth_batch_complete_max=4 \
  --output-format=$format \
  --output="$results_dir/write_latency_test_8K.$format"

#
# read bandwidth test
#
sudo fio --name=read_bandwidth_test \
  --filename="$filename" --filesize=$filesize \
  --time_based --ramp_time=$ramp_time --runtime=$runtime \
  --ioengine=$ioengine $ioengine_args --direct=1 --verify=0 --randrepeat=0 \
  --bs=1M --iodepth=64 --rw=read --numjobs=$numjobs --offset_increment=100G \
  --iodepth_batch_submit=64  --iodepth_batch_complete_max=64 \
  --output-format=$format \
  --output="$results_dir/read_bandwidth_test.$format"

#
# read IOPS test 4K
#
sudo fio --name=read_iops_test \
  --filename="$filename" --filesize=$filesize \
  --time_based --ramp_time=$ramp_time --runtime=$runtime \
  --ioengine=$ioengine $ioengine_args --direct=1 --verify=0 --randrepeat=0 \
  --bs=4K --iodepth=256 --rw=randread \
  --iodepth_batch_submit=256  --iodepth_batch_complete_max=256 \
  --output-format=$format \
  --output="$results_dir/read_iops_test.$format"

#
# read IOPS test 8K
#
sudo fio --name=read_iops_test \
  --filename="$filename" --filesize=$filesize \
  --time_based --ramp_time=$ramp_time --runtime=$runtime \
  --ioengine=$ioengine $ioengine_args --direct=1 --verify=0 --randrepeat=0 \
  --bs=8K --iodepth=256 --rw=randread \
  --iodepth_batch_submit=256  --iodepth_batch_complete_max=256 \
  --output-format=$format \
  --output="$results_dir/read_iops_test_8K.$format"

#
# read latency test 4K
#
sudo fio --name=read_latency_test \
  --filename="$filename" --filesize=$filesize \
  --time_based --ramp_time=$ramp_time --runtime=$runtime \
  --ioengine=$ioengine $ioengine_args --direct=1 --verify=0 --randrepeat=0 \
  --bs=4K --iodepth=4 --rw=randread \
  --iodepth_batch_submit=4  --iodepth_batch_complete_max=4 \
  --output-format=$format \
  --output="$results_dir/read_latency_test.$format"

#
# read latency test 8K
#
sudo fio --name=read_latency_test \
  --filename="$filename" --filesize=$filesize \
  --time_based --ramp_time=$ramp_time --runtime=$runtime \
  --ioengine=$ioengine $ioengine_args --direct=1 --verify=0 --randrepeat=0 \
  --bs=8K --iodepth=4 --rw=randread \
  --iodepth_batch_submit=4  --iodepth_batch_complete_max=4 \
  --output-format=$format \
  --output="$results_dir/read_latency_test_8K.$format"

if [[ "$format" == "json" ]]; then
    script_dir="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
    $script_dir/aggregate.py "$results_dir" 2>&1 | tee "$results_dir/result.txt"
fi
