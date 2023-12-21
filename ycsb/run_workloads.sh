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
LOG_DIR=logs

while [[ $# -gt 0 ]]; do case $1 in
    -l|--log-dir)
        LOG_DIR=$2
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
    --ycsb-hosts)
        EXTRA_ARGS="$EXTRA_ARGS --ycsb-hosts $2"
        shift;;
    --user)
        EXTRA_ARGS="$EXTRA_ARGS --user $2"
        shift;;
    --help|-h)
        usage
        exit;;
    *)
        source_files="$source_files $1"
        ;;
esac; shift; done

this_path=`realpath $0`
this_dir=`dirname $this_path`

if [[ `echo $source_files | wc -w` != "2" ]]; then
    echo "Wrong arguments, missing source files: $@"
    usage
    exit 1
fi

for source in $source_files
do
    if [[ ! -e "$source" ]]; then
        echo "Can't source: $source"
        exit 1
    fi
    . "$source"
done

if [ ! -d "$LOG_DIR" ]; then
  if [ -a "$LOG_DIR" ]; then
    echo "$LOG_DIR is not a directory"
    exit 1
  fi
  mkdir -p "$LOG_DIR"
  echo "Directory $LOG_DIR created"
fi

dt=`date +%Y%m%d_%H%M`
log_path="$LOG_DIR/${dt}_${name}"

echo "Raw log file: ${log_path}"

$this_dir/run_workloads_impl.sh --type $TYPE $EXTRA_ARGS $source_files &> "$log_path"
$this_dir/multinode_aggegate_result.py --type $TYPE $log_path | tee ${log_path}.res

echo "Result: ${log_path}.res"
