#!/bin/bash

# based on https://cloud.google.com/compute/docs/disks/benchmarking-pd-performance

results_dir="."
format=json

usage() {
    echo "Usage: $0"
    echo "  --test-dir <dir-on-fs-you-test>"
    echo "  [--results-dir <results-dir>] (default: $results_dir)"
    echo "  [--format <format>] (default: $format)"
}

if ! which fio >/dev/null; then
    echo "fio not found, you should install it: sudo apt-get update; sudo apt-get install -y fio"
    exit 1
fi

while [[ "$#" > 0 ]]; do case $1 in
    --test-dir)
        test_dir="$2";
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

if [[ -z "$test_dir" ]]; then
    echo "test-dir is not specified"
    usage
    exit 1
fi

if [[ ! -d "$test_dir" ]]; then
    echo "test-dir is not a directory"
    usage
    exit 1
fi

if [[ ! -d "$results_dir" ]]; then
    echo "results-dir is not a directory"
    usage
    exit 1
fi

# check that test_dir is writable
fiotest_dir="$test_dir/fiotest"
mkdir -p "$fiotest_dir"
if [[ ! -d "$fiotest_dir" ]]; then
    echo "Cannot create directory $fiotest_dir in test-dir"
    usage
    exit 1
fi

#
# write bandwidth test
#
sudo fio --name=write_bandwidth_test --directory=$fiotest_dir --numjobs=16 \
  --size=10G --time_based --runtime=60s --ramp_time=2s --ioengine=libaio \
  --direct=1 --verify=0 --bs=1M --iodepth=64 --rw=write \
  --iodepth_batch_submit=64 \
  --iodepth_batch_complete_max=64 \
  --output-format=$format \
  --output="$results_dir/write_bandwidth_test.$format"

#
# write IOPS test 4K
#
sudo fio --name=write_iops_test --directory=$fiotest_dir --size=10G \
  --time_based --runtime=60s --ramp_time=2s --ioengine=libaio --direct=1 \
  --verify=0 --bs=4K --iodepth=256 --rw=randwrite \
  --iodepth_batch_submit=256 --iodepth_batch_complete_max=256 \
  --output-format=$format \
  --output="$results_dir/write_iops_test.$format"

#
# write IOPS test 8K
#
sudo fio --name=write_iops_test --directory=$fiotest_dir --size=10G \
  --time_based --runtime=60s --ramp_time=2s --ioengine=libaio --direct=1 \
  --verify=0 --bs=8K --iodepth=256 --rw=randwrite \
  --iodepth_batch_submit=256 --iodepth_batch_complete_max=256 \
  --output-format=$format \
  --output="$results_dir/write_iops_test_8K.$format"

#
# read bandwidth test
#
sudo fio --name=read_bandwidth_test --directory=$fiotest_dir --numjobs=16 \
  --size=10G --time_based --runtime=60s --ramp_time=2s --ioengine=libaio \
  --direct=1 --verify=0 --bs=1M --iodepth=64 --rw=read \
  --iodepth_batch_submit=64 --iodepth_batch_complete_max=64 \
  --output-format=$format \
  --output="$results_dir/read_bandwidth_test.$format"

#
# read IOPS test 4K
#
sudo fio --name=read_iops_test --directory=$fiotest_dir --size=10G \
  --time_based --runtime=60s --ramp_time=2s --ioengine=libaio --direct=1 \
  --verify=0 --bs=4K --iodepth=256 --rw=randread \
  --iodepth_batch_submit=256 --iodepth_batch_complete_max=256 \
  --output-format=$format \
  --output="$results_dir/read_iops_test.$format"

#
# read IOPS test 8K
#
sudo fio --name=read_iops_test --directory=$fiotest_dir --size=10G \
  --time_based --runtime=60s --ramp_time=2s --ioengine=libaio --direct=1 \
  --verify=0 --bs=8K --iodepth=256 --rw=randread \
  --iodepth_batch_submit=256 --iodepth_batch_complete_max=256 \
  --output-format=$format \
  --output="$results_dir/read_iops_test_8K.$format"

if [[ "$format" == "json" ]]; then
    script_dir="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
    $script_dir/aggregate.py "$results_dir" 2>&1 | tee "$results_dir/result.txt"
fi

rm -rf "$fiotest_dir"
