#!/bin/bash

export TZ=UTC
export LC_ALL=en_US.UTF-8

execute_time_seconds=60
warmup_time_seconds=60
loader_threads=16
java_memory="2G"
log_dir="$HOME/tpcc_logs/postgres"
tpcc_path="$HOME/benchbase-postgres"

# in total, i.e. for all TPC-C instances. We will calculate per instance value below
max_connections=1000

# number of max sessions should be greater than this value
min_max_connections_per_instance=50


usage() {
    echo "Usage: $0"
    echo "    --warehouses <N> \\"
    echo "    --config <config_template> \\"
    echo "    --hosts <hosts_file> \\"
    echo "    [--run-phase-only] \\"
    echo "    [--log-dir <log_dir>] \\"
    echo "    [--time <time> --warmup <warmup>] \\"
    echo "    [--loader-threads <loader_threads>] \\"
    echo "    [--tpcc-path </path/to/Postgres/benchbase>] \\"
    echo "    [--max-connections $max_connections] \\"
    echo "    [--no-load] [--no-run] [--no-drop-create] \\"
}

log() {
    echo "`date` tpcc_postgres: $@"
}

kill_tpcc() {
    log "Killing tpcc instances"
    if [[ -z "$hosts_file" ]]; then
        log "No hosts file specified, can't kill tpcc instances"
        return
    fi

    unique_hosts=`mktemp`
    sort -u $hosts_file > $unique_hosts

    parallel-ssh -h $unique_hosts -i 'pkill -9 -f "^/bin/bash.*tpcc.sh"; pkill -9 -f "^java.*benchbase.jar -b tpcc"' &>/dev/null

    rm -f $unique_hosts
}

cleanup() {
    kill_tpcc
    exit 1
}

generate_configs() {
    generate_configs_hosts_file=$1

    # For each host in $hosts we generate config file with the following name: config.<host_num>.xml,
    # Note that host_num is line number in $generate_configs_hosts_file
    $tpcc_helper \
        -w $warehouses \
        generate-configs \
        --hosts $generate_configs_hosts_file \
        --input $config_template \
        --execute-time $execute_time_seconds \
        --warmup-time $warmup_time_seconds \
        --max-connections $max_connections_per_instance \
        --loader-threads $loader_threads

    if [ $? -ne 0 ]; then
        echo "Failed to generate configs"
        exit 1
    fi

    # now upload configs to the hosts
    node_num=1
    scp_pids=()
    for host in `cat $generate_configs_hosts_file`; do
        config="config.$node_num.xml"
        node_num=$((node_num + 1))

        scp $config ${host}:$tpcc_path/$config &
        scp_pids+=($!)
        log "Uploading config '$config' to $host, pid: ${scp_pids[-1]}"
    done

    for pid in "${scp_pids[@]}"; do
        wait $pid
        if [ $? -ne 0 ]; then
            log "Failed to upload config, pid=$pid"
            exit 1
        fi
    done

    node_num=1
    for host in `cat $generate_configs_hosts_file`; do
        config="config.$node_num.xml"
        rm $config
        node_num=$((node_num + 1))
    done
}

trap cleanup SIGINT SIGTERM

if ! which parallel-ssh >/dev/null; then
    echo "parallel-ssh not found, you should install pssh"
    exit 1
fi

for module in numpy requests; do
    if ! python3 -c "import $module" 2>/dev/null; then
        echo "Python3 module $module not found, you should install it, execute: pip3 install $module)"
        exit 1
    fi
done

while [[ "$#" > 0 ]]; do case $1 in
    --warehouses)
        warehouses=$2
        shift;;
    --config)
        config_template=$2
        shift;;
    --tpcc-path)
        tpcc_path=$2
        shift;;
    --no-run)
        no_run=1
        ;;
    --no-load)
        no_load=1
        ;;
    --no-drop-create)
        no_drop_create=1
        ;;
    --run-phase-only)
        no_load=1
        no_drop_create=1
        ;;
    --hosts)
        hosts_file=$2
        shift;;
    --time)
        execute_time_seconds=$2
        shift;;
    --warmup)
        warmup_time_seconds=$2
        shift;;
    --loader-threads)
        loader_threads=$2
        shift;;
    --java-memory)
        java_memory=$2
        shift;;
    --max-connections)
        max_connections=$2
        shift;;
    --log-dir)
        log_dir=$2
        shift;;
    --help|-h)
        usage
        exit;;
    *)
        echo "Unknown parameter passed: $1"
        usage
        exit 1;;
esac; shift; done

if [ -z "$warehouses" ]; then
    echo "Please specify the number of warehouses"
    usage
    exit 1
fi

if [ -z "$config_template" ]; then
    echo "Please specify the config template file"
    usage
    exit 1
fi

if [ ! -r "$config_template" ]; then
    echo "Config file '$config' not found"
    exit 1
fi

if [[ "$config_template" != /* ]]; then
    config_template=`pwd`/$config_template
fi

if [ -z "$hosts_file" ]; then
    echo "Please specify the hosts file"
    usage
    exit 1
fi

if [ ! -r "$hosts_file" ]; then
    echo "Hosts file '$hosts_file' not found"
    exit 1
fi

this_dir=`dirname $0`
this_path=`readlink -f $this_dir`
tpcc_helper="$this_path/tpcc_postgres_helper.py"
tpcc_ydb_helper="$this_path/../ydb/tpcc_helper.py"

cd $this_path
if [[ $? -ne 0 ]]; then
    echo "Can't change directory to $this_path"
    exit 1
fi

host_count=`wc -l $hosts_file | awk '{print $1}'`

tpcc_script="$tpcc_path/scripts/tpcc.sh"
parallel-ssh -h $hosts_file -i 'test -e $tpcc_script || (echo tpcc.sh does not exist && exit 1)'
if [ $? -ne 0 ]; then
    echo "$tpcc_script not found on some/all hosts, install benchbase (check our build and README)"
    rm -f $unique_hosts
    exit 1
fi

kill_tpcc

if [[ -n "$no_load" && -n "$no_run" ]]; then
    exit 0
fi

max_connections_per_instance=$(( $max_connections / $host_count ))
if [[ $max_connections_per_instance -lt $min_max_connections_per_instance ]]; then
    max_connections=$min_max_connections_per_instance
fi

dt=`date +%Y%m%d_%H%M`

mkdir -p $log_dir
if [[ ! -e $log_dir ]]; then
    echo "Failed to create $log_dir directory"
    exit 1
fi

results_dir="${log_dir:-.}/results_${dt}_${warehouses}wh"
log "Result dir: $results_dir"

if [ ! -e $results_dir ]; then
    mkdir $results_dir
    if [ $? -ne 0 ]; then
        echo "Failed to create $results_dir directory"
        exit 1
    fi
fi

if [ -z "$no_load" ]; then
    load_start=$SECONDS
    log "Loading data"

    single_hosts=`mktemp`
    head -1 $hosts_file > $single_hosts

    # hack to load everything from one instance
    generate_configs $single_hosts
    rm -f $single_hosts

    if [ -z "$no_drop_create" ]; then
        args="--create=true"
    else
        args="--create=false"
    fi

    args="$args --load=true --execute=false"

    host=`head -1 $hosts_file`
    ssh $host "cd $tpcc_path && ./scripts/tpcc.sh --memory $java_memory -c config.1.xml $args" \
        &> $results_dir/$host.load.log

    elapsed=$(( SECONDS - load_start ))
    log "Loading data done in $elapsed seconds"

    # we have some issue with reporting OK and having index available
    # also above we changed min partitions, so we need to wait a bit

    if [[ $warehouses -ge 15000 ]]; then
        log "Sleeping 20m after loading the data"
        sleep 20m
    else
        log "Sleeping 5m after loading the data"
        sleep 5m
    fi
fi

if [ -n "$no_run" ]; then
    exit 0
fi

for host in `cat $hosts_file`; do
    mkdir -p "$results_dir/$host"
done

log "Generating TPC-C configs and uploading to the hosts"
generate_configs $hosts_file

log "Running benchmark"

pids=()
host_num=1
for host in `cat $hosts_file`; do
    config="config.$host_num.xml"
    log "Running tpcc on $host (config $config)"

    args=`$tpcc_helper \
        -w $warehouses \
        -n $host_count \
        get-start-args \
        --node-num $host_num`

    ssh $host "cd $tpcc_path && rm -rf "results_${host_num}" && ./scripts/tpcc.sh --memory $java_memory -d results_${host_num} -c $config $args" \
        > $results_dir/$host/$host_num.run.log 2>&1 &
    pids+=($!)
    host_num=`expr $host_num + 1`
done

for pid in "${pids[@]}"; do
    wait $pid
    if [ $? -ne 0 ]; then
        echo "Failed to run on some/all hosts"
        exit 1
    fi
done

log "Running benchmark done, copying results from the hosts"

for host in `cat $hosts_file | sort -u`; do
    host_results="$results_dir/$host"
    cd "$host_results"
    scp -r $host:$tpcc_path/results* ./
    cd -
done

log "Aggregating total result"

$tpcc_ydb_helper aggregate $results_dir
