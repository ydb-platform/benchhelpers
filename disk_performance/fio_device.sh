#!/bin/bash

# Initially based on recommendations from https://cloud.google.com/compute/docs/disks/benchmarking-pd-performance
# Adjusted to measure a steady (aged) performance and variance

filesize=2500G

ramp_time=10s
runtime=1m

bandwidth_depth=64
iops_depth=256
latency_depth=1

format=json
run_type=normal
run_count=10

multi_stream_seq_test_offset=100G

ioengine=libaio
ioengine_args=

results_dir="$(date +%Y%m%d_%H%M)_results"
fill_disk=false

usage() {
    echo "Usage: $0"
    echo "  --filename <filename> "
    echo "  [--filesize <filesize>] (default: $filesize)"
    echo "  [--results-dir <results-dir>] (default: YYYYMMDD_HHMM_results)"
    echo "  [--fill-disk] (default: false)"
    echo "  [--ioengine] (default $ioengine)"
    echo "  [--ioengine-args] (default NONE)"
    echo "  [--ramp-time <ramp-time>] (default: $ramp_time)"
    echo "  [--runtime <runtime>] (default: $runtime)"
    echo "  [--run-type <smoke|normal|long>] (default: $run_type)"
    echo "  [--run-count <run-count>] (default: $run_count)"
    echo "  [--format <format>] (default: $format)"
}

size_to_bytes() {
    local size="$1"
    local value unit multiplier

    if [[ "$size" =~ ^([0-9]+)$ ]]; then
        echo "${BASH_REMATCH[1]}"
        return 0
    fi

    if [[ "$size" =~ ^([0-9]+)([KkMmGgTtPp])$ ]]; then
        value="${BASH_REMATCH[1]}"
        unit="${BASH_REMATCH[2]}"
        case "$unit" in
            [Kk]) multiplier=$((1024));;
            [Mm]) multiplier=$((1024**2));;
            [Gg]) multiplier=$((1024**3));;
            [Tt]) multiplier=$((1024**4));;
            [Pp]) multiplier=$((1024**5));;
            *)
                return 1
                ;;
        esac
        echo $((value * multiplier))
        return 0
    fi

    return 1
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
    --fill-disk)
        fill_disk=true
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

if ! [[ "$run_count" =~ ^[1-9][0-9]*$ ]]; then
    echo "run-count must be a positive integer, got: $run_count"
    exit 1
fi

fill_size_percent=100
case "$run_type" in
    smoke)
        ramp_time=2s
        runtime=10s
        fill_size_percent=5
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

filesize_bytes="$(size_to_bytes "$filesize")"
if [[ $? -ne 0 || -z "$filesize_bytes" ]]; then
    echo "unsupported filesize format: $filesize (expected like 2500G, 4K, 1048576)"
    exit 1
fi

multi_stream_offset_bytes="$(size_to_bytes "$multi_stream_seq_test_offset")"
if [[ $? -ne 0 || -z "$multi_stream_offset_bytes" ]]; then
    echo "unsupported multi_stream_seq_test_offset format: $multi_stream_seq_test_offset"
    exit 1
fi

if (( filesize_bytes > target_size_bytes )); then
    echo "filesize ($filesize) exceeds target size ($target_size_bytes bytes)"
    exit 1
fi

required_span_bytes=$((filesize_bytes + (numjobs - 1) * multi_stream_offset_bytes))
if (( required_span_bytes > target_size_bytes )); then
    echo "multistream layout does not fit target size: required $required_span_bytes bytes, available $target_size_bytes bytes"
    echo "adjust --filesize, --numjobs, or --offset_increment base (multi_stream_seq_test_offset)"
    exit 1
fi

#
# fill the disk with random data
#

if [[ "$fill_disk" == "true" ]]; then
    echo "Filling disk (preconditioning)..."
    script_dir="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
    bash "$script_dir/fill_disk.sh" \
        --filename "$filename" \
        --size-percent "$fill_size_percent"
    if [[ $? -ne 0 ]]; then
        echo "fill_disk failed"
        exit 1
    fi
else
    if [[ -b "$filename" ]]; then
        echo "Skipping fill; running blkdiscard on $filename..."
        sudo blkdiscard "$filename"
        if [[ $? -ne 0 ]]; then
            echo "blkdiscard failed"
            exit 1
        fi
    else
        echo "Skipping fill; blkdiscard requires a block device (got: $filename)"
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
    --filename="$filename" --filesize=$filesize \
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
    --filename="$filename" --filesize=$filesize \
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
    --filename="$filename" --filesize=$filesize \
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
    --filename="$filename" --filesize=$filesize \
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
    --filename="$filename" --filesize=$filesize \
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
    --filename="$filename" --filesize=$filesize \
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
    --filename="$filename" --filesize=$filesize \
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
    --filename="$filename" --filesize=$filesize \
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
    --filename="$filename" --filesize=$filesize \
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
    --filename="$filename" --filesize=$filesize \
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
    $script_dir/aggregate.py "$results_dir" 2>&1 | tee "$results_dir/result.txt"
fi
