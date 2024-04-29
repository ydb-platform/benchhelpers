#!/bin/bash
#
# This script is used to generate flamegraph for a given host

export TZ=UTC
export LC_ALL=en_US.UTF-8

duration=60
frequency=199


usage() {
    echo "Usage: $0 <host> [-t <duration>] [-f <frequency>] [--only-storage] [-o <output file>]"
}

while [[ "$#" > 0 ]]; do case $1 in
    -t|--time)
        duration="$2"
        shift;;
    -o|--output)
        output="$2"
        shift;;
    -f|--frequency)
        frequency="$2"
        shift;;
    --only-storage)
        only_storage=1;;
    -h|--help)
        usage
        exit 0;;
    *)
        host="$1";;
esac; shift; done

if [[ -z "$host" ]]; then
    usage
    exit 1
fi

dt=`date +%Y%m%d_%H%M`

if [[ -z "$output" ]]; then
    short_host=`echo $host | cut -d'.' -f1`
    output="flamegraph-$short_host-$dt.svg"
fi

flamegraph_dir="FlameGraph"
this_dir=`dirname $0`
flamegraph_package="${this_dir}/FlameGraph.tar.gz"

if ssh $host "[ -d $flamegraph_dir ]"; then
    echo "$flamegraph_dir exists on $host"
else
    scp "$flamegraph_package" ${host}:
    if [[ $? -ne 0 ]]; then
        echo "Failed to copy $flamegraph_package to $host"
        exit 1
    fi

    ssh $host "tar -xzf FlameGraph.tar.gz"
    if [[ $? -ne 0 ]]; then
        echo "Failed to extract $flamegraph_package on $host"
        exit 1
    fi
fi

perf_data_file="${dt}_perf.data"
stack_collapse_script="$flamegraph_dir/stackcollapse-perf.pl"
flamegraph_script="$flamegraph_dir/flamegraph.pl"

if [[ -n "$only_storage" ]]; then
    perf_record_cmd="sudo perf record -F $frequency -o $perf_data_file -a -g -p \`pgrep -f ydbd.*static | head -1\` -- sleep $duration"
else
    perf_record_cmd="sudo perf record -F $frequency -o $perf_data_file -a -g -- sleep $duration"
fi

flamegraph_cmd="sudo perf script -i $perf_data_file | $stack_collapse_script | $flamegraph_script"

echo "Recording perf data during $duration seconds on $host: $perf_record_cmd"

ssh $host "$perf_record_cmd"
if [[ $? -ne 0 ]]; then
    echo "Failed to record perf data on $host"
    exit 1
fi

echo "Generating flamegraph: $flamegraph_cmd"
ssh $host "$flamegraph_cmd" > $output
if [[ $? -ne 0 ]]; then
    echo "Failed to generate flamegraph on $host"
    exit 1
fi

ssh $host "sudo rm -f $perf_data_file"

echo "Flamegraph: `hostname`:${output}"
