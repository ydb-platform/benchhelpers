#!/bin/bash

# python3.8 doesn't accept MSK/MSD and I don't yet know how to fix it
export TZ=UTC

# another source of possible issues
export LC_ALL=en_US.UTF-8

usage() {
      echo "Usage: run_workloads.sh --log-dir ~/ydb_ycsb_logs --name test_feature_x [--type cockroach|ydb|yugabyte|yugabyteSQL|postgresql] workload.rc cluster.rc"
}

if ! command -v parallel-ssh &> /dev/null
then
    echo "`parallel-ssh` could not be found in your PATH"
    exit 1
fi

TYPE=ydb
EXTRA_ARGS=

while [[ "$#" > 0 ]]; do case $1 in
    -l|--log-dir)
        log_dir=$2
        shift;;
    --name)
        name=$2
        shift;;
    --type)
        TYPE=$2
        shift;;
    --threads)
        EXTRA_ARGS="$EXTRA_ARGS --threads $2"
        shift;;
    --de-threads)
        EXTRA_ARGS="$EXTRA_ARGS --de-threads $2"
        shift;;
    --ycsb-nodes)
        EXTRA_ARGS="$EXTRA_ARGS --ycsb-nodes $2"
        shift;;
    --help|-h)
        usage
        exit;;
    *)
        source_files="$source_files $1"
        ;;
esac; shift; done

if [[ `echo $source_files | wc -w` != "2" ]]; then
    echo "Wrong arguments, missing source files: $@"
    usage
    exit 1
fi

for source in $@; do
    if [[ ! -e "$source" ]]; then
        echo "Can't source: $source"
        exit 1
    fi
done

dt=`date +%Y%m%d_%H%M`
log_path="$log_dir/${dt}_${name}"

echo "Raw log file: ${log_path}"

./run_workloads_impl.sh --type $TYPE $EXTRA_ARGS $source_files &> "$log_path"

./multinode_aggegate_result.py --type $TYPE $log_path | tee ${log_path}.res

echo "Result: ${log_path}.res"
