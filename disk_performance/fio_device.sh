#!/bin/bash

# Initially based on recommendations from https://cloud.google.com/compute/docs/disks/benchmarking-pd-performance
# Adjusted to measure a steady (aged) performance and variance

size_percent=100

ramp_time=10s
runtime=1m

bandwidth_depth=64
iops_depth=256
latency_depth=1

format=json
run_type=normal
run_count=10

multi_stream_seq_test_offset=100G

ioengine=io_uring
ioengine_args="--hipri=1 --sqthread_poll=1"
use_aio=false

results_dir="$(date +%Y%m%d_%H%M)_results"
clean_device=false
prefix=""

usage() {
    echo "Usage: $0"
    echo "  --filename <filename> "
    echo "  [--size-percent <size-percent>] (default: $size_percent)"
    echo "  [--results-dir <results-dir>] (default: YYYYMMDD_HHMM_results)"
    echo "  [--clean-device] (default: false; skip precondition and do blkdiscard only)"
    echo "  [--ioengine] (default $ioengine)"
    echo "  [--ioengine-args] (default: $ioengine_args)"
    echo "  [--use-aio] (default: false)"
    echo "  [--ramp-time <ramp-time>] (default: $ramp_time)"
    echo "  [--runtime <runtime>] (default: $runtime)"
    echo "  [--run-type <smoke|normal|long>] (default: $run_type)"
    echo "  [--run-count <run-count>] (default: $run_count)"
    echo "  [--format <format>] (default: $format)"
    echo "  [--prefix <prefix>] (default: empty)"
}

if ! which fio >/dev/null; then
    echo "fio not found, you should install it: sudo apt-get update; sudo apt-get install -y fio"
    exit 1
fi

while [[ "$#" > 0 ]]; do case $1 in
    --filename)
        filename="$2";
        shift;;
    --size-percent)
        size_percent="$2";
        shift;;
    --results-dir)
        results_dir="$2";
        shift;;
    --clean-device)
        clean_device=true
        ;;
    --use-aio)
        use_aio=true
        ;;
    --ioengine)
        ioengine="$2";
        shift;;
    --ioengine-args)
        ioengine_args="$2";
        shift;;
    --ramp-time)
        ramp_time="$2";
        shift;;
    --runtime)
        runtime="$2";
        shift;;
    --run-type)
        run_type="$2";
        shift;;
    --run-count)
        run_count="$2";
        shift;;
    --format)
        format="$2";
        shift;;
    --prefix)
        prefix="$2";
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

if ! [[ "$size_percent" =~ ^[0-9]+$ ]] || (( size_percent < 1 || size_percent > 100 )); then
    echo "size-percent must be an integer in [1,100], got: $size_percent"
    exit 1
fi

if ! [[ "$run_count" =~ ^[1-9][0-9]*$ ]]; then
    echo "run-count must be a positive integer, got: $run_count"
    exit 1
fi

case "$run_type" in
    smoke)
        ramp_time=2s
        runtime=10s
        size_percent=5
        ;;
    normal)
        # Use explicitly provided/default values.
        ;;
    long)
        ramp_time=60s
        runtime=10m
        ;;
    *)
        echo "invalid --run-type: $run_type (expected smoke, normal, or long)"
        exit 1
        ;;
esac

if ! [[ "$ramp_time" =~ ^[0-9]+[smhd]$ ]]; then
    echo "invalid --ramp-time: $ramp_time (expected like 5s, 1m)"
    exit 1
fi

if ! [[ "$runtime" =~ ^[0-9]+[smhd]$ ]]; then
    echo "invalid --runtime: $runtime (expected like 15s, 1m)"
    exit 1
fi

if [[ -d "$results_dir" ]] && [[ -n "$(ls -A "$results_dir" 2>/dev/null)" ]]; then
    echo "results dir '$results_dir' already exists and is not empty; refusing to overwrite"
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

# Validate test layout against the backing target size.
if [[ -b "$filename" ]]; then
    target_size_bytes="$(sudo blockdev --getsize64 "$filename" 2>/dev/null)"
    if [[ $? -ne 0 || -z "$target_size_bytes" ]]; then
        echo "failed to get block device size for $filename"
        exit 1
    fi
elif [[ -f "$filename" ]]; then
    target_size_bytes="$(stat -c%s "$filename" 2>/dev/null)"
    if [[ $? -ne 0 || -z "$target_size_bytes" ]]; then
        echo "failed to get file size for $filename"
        exit 1
    fi
else
    echo "filename must be a block device or regular file: $filename"
    exit 1
fi
test_size_bytes=$((target_size_bytes * size_percent / 100))
if (( test_size_bytes <= 0 )); then
    echo "calculated test size is zero bytes for size-percent=$size_percent and target size=$target_size_bytes"
    exit 1
fi

if [[ "$use_aio" == "true" ]]; then
    ioengine=libaio
    ioengine_args=""
fi

#
# fill the disk with random data
#

if [[ "$clean_device" == "true" ]]; then
    if [[ -b "$filename" ]]; then
        echo "Skipping precondition; running blkdiscard on $filename..."
        sudo blkdiscard "$filename"
        if [[ $? -ne 0 ]]; then
            echo "blkdiscard failed"
            exit 1
        fi
    else
        echo "Skipping precondition; blkdiscard requires a block device (got: $filename)"
    fi
else
    echo "Filling disk (preconditioning)..."
    script_dir="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
    bash "$script_dir/precondition.sh" \
        --filename "$filename" \
        --size-percent "$size_percent"
    if [[ $? -ne 0 ]]; then
        echo "precondition failed"
        exit 1
    fi
fi

percentile_list="10:50:90:95:99:99.9"

for run_id in $(seq 1 "$run_count"); do
  echo "Running iteration $run_id/$run_count"
  run_results_dir="$results_dir/$run_id"
  mkdir -p "$run_results_dir" || exit 1

  #
  # write bandwidth test
  #
  echo "Running test: write_bandwidth_test"
  sudo fio --name=write_bandwidth_test \
    --filename="$filename" --size="${size_percent}%" \
    --time_based --ramp_time=$ramp_time --runtime=$runtime \
    --ioengine=$ioengine $ioengine_args --direct=1 --verify=0 --randrepeat=0 \
    --bs=1M \
    --iodepth=$bandwidth_depth \
    --iodepth_batch_submit=$bandwidth_depth \
    --iodepth_batch_complete_max=$bandwidth_depth \
    --rw=write \
    --numjobs=1 \
    --offset_increment=$multi_stream_seq_test_offset \
    --percentile_list=$percentile_list \
    --output-format=$format \
    --output="$run_results_dir/write_bandwidth_test.$format" \
    1>/dev/null

  #
  # write IOPS test 4K
  #
  echo "Running test: write_iops_test_4K"
  sudo fio --name=write_iops_test \
    --filename="$filename" --size="${size_percent}%" \
    --time_based --ramp_time=$ramp_time --runtime=$runtime \
    --ioengine=$ioengine $ioengine_args --direct=1 --verify=0 --randrepeat=0 \
    --bs=4K --iodepth=$iops_depth --rw=randwrite --numjobs=1 \
    --iodepth_batch_submit=$iops_depth  --iodepth_batch_complete_max=$iops_depth \
    --percentile_list=$percentile_list \
    --output-format=$format \
    --output="$run_results_dir/write_iops_test.$format" \
    1>/dev/null

  #
  # write IOPS test 8K
  #
  echo "Running test: write_iops_test_8K"
  sudo fio --name=write_iops_test \
    --filename="$filename" --size="${size_percent}%" \
    --time_based --ramp_time=$ramp_time --runtime=$runtime \
    --ioengine=$ioengine $ioengine_args --direct=1 --verify=0 --randrepeat=0 \
    --bs=8K --iodepth=$iops_depth --rw=randwrite --numjobs=1 \
    --iodepth_batch_submit=$iops_depth  --iodepth_batch_complete_max=$iops_depth \
    --percentile_list=$percentile_list \
    --output-format=$format \
    --output="$run_results_dir/write_iops_test_8K.$format" \
    1>/dev/null

  #
  # write latency test 4K
  #
  echo "Running test: write_latency_test_4K"
  sudo fio --name=write_latency_test \
    --filename="$filename" --size="${size_percent}%" \
    --time_based --ramp_time=$ramp_time --runtime=$runtime \
    --ioengine=$ioengine $ioengine_args --direct=1 --verify=0 --randrepeat=0 \
    --bs=4K --iodepth=$latency_depth --rw=randwrite --numjobs=1 --iodepth_batch_submit=$latency_depth  \
    --iodepth_batch_complete_max=$latency_depth \
    --percentile_list=$percentile_list \
    --output-format=$format \
    --output="$run_results_dir/write_latency_test.$format" \
    1>/dev/null

  #
  # write latency test 8K
  #
  echo "Running test: write_latency_test_8K"
  sudo fio --name=write_latency_test \
    --filename="$filename" --size="${size_percent}%" \
    --time_based --ramp_time=$ramp_time --runtime=$runtime \
    --ioengine=$ioengine $ioengine_args --direct=1 --verify=0 --randrepeat=0 \
    --bs=8K --iodepth=$latency_depth --rw=randwrite --numjobs=1 --iodepth_batch_submit=$latency_depth  \
    --iodepth_batch_complete_max=$latency_depth \
    --percentile_list=$percentile_list \
    --output-format=$format \
    --output="$run_results_dir/write_latency_test_8K.$format" \
    1>/dev/null

  #
  # read bandwidth test
  #
  echo "Running test: read_bandwidth_test"
  sudo fio --name=read_bandwidth_test \
    --filename="$filename" --size="${size_percent}%" \
    --time_based --ramp_time=$ramp_time --runtime=$runtime \
    --ioengine=$ioengine $ioengine_args --direct=1 --verify=0 --randrepeat=0 \
    --bs=1M --iodepth=$bandwidth_depth --rw=read --numjobs=1 --offset_increment=$multi_stream_seq_test_offset \
    --iodepth_batch_submit=$bandwidth_depth  --iodepth_batch_complete_max=$bandwidth_depth \
    --percentile_list=$percentile_list \
    --output-format=$format \
    --output="$run_results_dir/read_bandwidth_test.$format" \
    1>/dev/null

  #
  # read IOPS test 4K
  #
  echo "Running test: read_iops_test_4K"
  sudo fio --name=read_iops_test \
    --filename="$filename" --size="${size_percent}%" \
    --time_based --ramp_time=$ramp_time --runtime=$runtime \
    --ioengine=$ioengine $ioengine_args --direct=1 --verify=0 --randrepeat=0 \
    --bs=4K --iodepth=$iops_depth --rw=randread --numjobs=1 \
    --iodepth_batch_submit=$iops_depth  --iodepth_batch_complete_max=$iops_depth \
    --percentile_list=$percentile_list \
    --output-format=$format \
    --output="$run_results_dir/read_iops_test.$format" \
    1>/dev/null

  #
  # read IOPS test 8K
  #
  echo "Running test: read_iops_test_8K"
  sudo fio --name=read_iops_test \
    --filename="$filename" --size="${size_percent}%" \
    --time_based --ramp_time=$ramp_time --runtime=$runtime \
    --ioengine=$ioengine $ioengine_args --direct=1 --verify=0 --randrepeat=0 \
    --bs=8K --iodepth=$iops_depth --rw=randread --numjobs=1 \
    --iodepth_batch_submit=$iops_depth  --iodepth_batch_complete_max=$iops_depth \
    --percentile_list=$percentile_list \
    --output-format=$format \
    --output="$run_results_dir/read_iops_test_8K.$format" \
    1>/dev/null

  #
  # read latency test 4K
  #
  echo "Running test: read_latency_test_4K"
  sudo fio --name=read_latency_test \
    --filename="$filename" --size="${size_percent}%" \
    --time_based --ramp_time=$ramp_time --runtime=$runtime \
    --ioengine=$ioengine $ioengine_args --direct=1 --verify=0 --randrepeat=0 \
    --bs=4K --iodepth=$latency_depth --rw=randread --numjobs=1 \
    --iodepth_batch_submit=$latency_depth  --iodepth_batch_complete_max=$latency_depth \
    --percentile_list=$percentile_list \
    --output-format=$format \
    --output="$run_results_dir/read_latency_test.$format" \
    1>/dev/null

  #
  # read latency test 8K
  #
  echo "Running test: read_latency_test_8K"
  sudo fio --name=read_latency_test \
    --filename="$filename" --size="${size_percent}%" \
    --time_based --ramp_time=$ramp_time --runtime=$runtime \
    --ioengine=$ioengine $ioengine_args --direct=1 --verify=0 --randrepeat=0 \
    --bs=8K --iodepth=$latency_depth --rw=randread --numjobs=1 \
    --iodepth_batch_submit=$latency_depth  --iodepth_batch_complete_max=$latency_depth \
    --percentile_list=$percentile_list \
    --output-format=$format \
    --output="$run_results_dir/read_latency_test_8K.$format" \
    1>/dev/null
done

if [[ "$format" == "json" ]]; then
    script_dir="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
    aggregate_cmd=("$script_dir/aggregate.py" "$results_dir" "--plot")
    if [[ -n "$prefix" ]]; then
        aggregate_cmd+=("--prefix" "$prefix")
    fi
    "${aggregate_cmd[@]}" 2>&1 | tee "$results_dir/result.txt"
fi
