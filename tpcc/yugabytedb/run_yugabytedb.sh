#!/bin/bash

export TZ=UTC
export LC_ALL=en_US.UTF-8

execute_time_seconds=120
warmup_time_seconds=60

loader_threads=32

java_memory="32G"

log_dir="$HOME/tpcc_logs/yugabytedb"

default_yb_path=/opt/yugabyte
default_compaction_timeout=3600

max_sessions=3000

# intentionally relative
tpcc_path="tpcc"

usage() {
    echo "Usage: $0"
    echo "    --warehouses <N> \\"
    echo "    --yb-nodes <comma_separated_YB_nodes> \\"
    echo "    --hosts <tpcc_hosts_file> \\"
    echo "    [--run-phase-only] \\"
    echo "    [--log-dir <log_dir>] \\"
    echo "    [--time <time> --warmup <warmup>] \\"
    echo "    [--loader-threads <loader_threads>] \\"
    echo "    [--tpcc-path </path/to/YB/tpcc>] \\"
    echo "    [--max-sessions $max_sessions] \\"
    echo "    [--no-load] [--no-run] [--no-drop-create] \\"
}

log() {
    echo "`date` tpcc_yugabytedb: $@"
}

kill_tpcc() {
    log "Killing tpcc instances and clean previous results"
    if [[ -z "$hosts_file" ]]; then
        log "No hosts file specified, can't kill tpcc instances"
        return
    fi

    parallel-ssh -h $unique_hosts -i 'pkill -9 -f "tpccbenchmark"; pkill -9 -f "^java.*com.oltpbenchmark.DBWorkload"' &>/dev/null
    parallel-ssh -h $unique_hosts -i "cd $tpcc_path && rm -rf results_*" &>/dev/null
}

cleanup() {
    if [[ -n "$unique_hosts" ]]; then
        rm -f $unique_hosts
    fi
    kill_tpcc
    exit 1
}

trap cleanup SIGINT SIGTERM

if ! which parallel-ssh >/dev/null; then
    echo "parallel-ssh not found, you should install pssh"
    exit 1
fi

while [[ "$#" > 0 ]]; do case $1 in
    --warehouses)
        warehouses=$2
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
    --compact)
        need_compaction=1
        ;;
    --hosts)
        hosts_file=$2
        shift;;
    --yb-nodes)
        yb_nodes=$2
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
    --max-sessions)
        max_sessions=$2
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

if [ -z "$hosts_file" ]; then
    echo "Please specify the hosts file"
    usage
    exit 1
fi

if [ ! -r "$hosts_file" ]; then
    echo "Hosts file '$hosts_file' not found"
    exit 1
fi

if [ -z "$yb_nodes" ]; then
    echo "Please specify YugabyteDB nodes (comma separated)"
    usage
    exit 1
fi

unique_hosts=`mktemp`
sort -u $hosts_file > $unique_hosts

single_tpcc_host=`head -n 1 $hosts_file`

ssh_user=`whoami`
if `echo $single_tpcc_host | grep -q "@"`; then
    ssh_user=`echo $single_tpcc_host | cut -d@ -f1`
    log "Using ssh user: $ssh_user"
fi

# we need this hack to not force
# user accept manually cluster hosts
for host in `cat "$hosts_file" | sort -u`; do
    ssh -o StrictHostKeyChecking=no $host &>/dev/null &
done

this_dir=`dirname $0`
this_path=`readlink -f $this_dir`

cd $this_path
if [[ $? -ne 0 ]]; then
    echo "Can't change directory to $this_path"
    exit 1
fi

mkdir -p $log_dir
if [[ ! -e $log_dir ]]; then
    echo "Failed to create $log_dir directory"
    exit 1
fi

dt=`date +%Y%m%d_%H%M`
results_dir="${log_dir:-.}/results_${dt}_${warehouses}wh"
log "Result dir: $results_dir"
if [ ! -e $results_dir ]; then
    mkdir $results_dir
    if [ $? -ne 0 ]; then
        echo "Failed to create $results_dir directory"
        exit 1
    fi
fi

host_count=`wc -l $hosts_file | awk '{print $1}'`
warehouses_per_host=`expr $warehouses / $host_count`

parallel-ssh -h $unique_hosts -i "test -e $tpcc_path/tpccbenchmark || (echo $tpcc_path/tpccbenchmark does not exist && exit 1)"
if [ $? -ne 0 ]; then
    echo "$tpcc_path/tpccbenchmark not found on some/all hosts"
    exit 1
fi

# Update java memory
log "Updating java memory to $java_memory"
parallel-ssh -h $unique_hosts "sed -i 's/memory='[0-9]\+G'/memory=${java_memory}G/' $tpcc_path/tpccbenchmark"
if [ $? -ne 0 ]; then
    echo "Failed to update java memory"
    exit 1
fi

# drop-create database/tables/etc
if [ -z "$no_drop_create" ]; then
    log "Drop existing tables if exists"

    ssh $single_tpcc_host "cd $tpcc_path && ./tpccbenchmark --nodes=$yb_nodes --clear=true" \
        > $results_dir/drop.log 2>&1
    if [[ $? -ne 0 ]]; then
        log "Failed to drop tables"
        exit 1
    fi

    log "Creating tables"

    ssh $single_tpcc_host "cd $tpcc_path && ./tpccbenchmark --nodes=$yb_nodes --create=true" \
        > $results_dir/create.log 2>&1
    if [[ $? -ne 0 ]]; then
        log "Failed to create tables"
        exit 1
    fi
fi

if [ -z "$no_load" ]; then
    load_start=$SECONDS
    log "Loading data"

    pids=()
    host_num=1
    start_warehouse_id=1

    for host in `cat $hosts_file`; do
        cmd="./tpccbenchmark --load=true --nodes=$yb_nodes --warehouses $warehouses_per_host \
            --start-warehouse-id $start_warehouse_id --total-warehouses=$warehouses \
            --loaderthreads $loader_threads"

        log "Running tpcc initial load on $host (host_num=$host_num): $cmd"

        ssh $host "cd $tpcc_path && $cmd" \
            > $results_dir/$host.$host_num.load.log 2>&1 &
        pids+=($!)

        host_num=`expr $host_num + 1`
        start_warehouse_id=`expr $start_warehouse_id + $warehouses_per_host`
    done

    for pid in "${pids[@]}"; do
        wait $pid
        if [ $? -ne 0 ]; then
            log "Failed to load data on some/all hosts"
            exit 1
        fi
    done

    elapsed=$(( SECONDS - load_start ))
    log "Loading done in $elapsed seconds"

    fk_enable_start=$SECONDS
    log "Enabling foreign keys"
    ssh $single_tpcc_host "cd $tpcc_path && ./tpccbenchmark --nodes=$yb_nodes --enable-foreign-keys=true" \
        > $results_dir/enable_fk.log 2>&1
    if [[ $? -ne 0 ]]; then
        log "Failed to enable foreign keys"
        exit 1
    fi
    elapsed=$(( SECONDS - fk_enable_start ))
    log "Enabling foreign keys done in $elapsed seconds"

    need_compaction=1
fi

if [[ -n "$need_compaction" ]]; then
    log "Compacting tables"
    compaction_started=$SECONDS
    yb_node=`echo $yb_nodes | cut -d, -f1`

    pids=()
    for table in warehouse district customer history new_order oorder order_line item stock idx_customer_name idx_order; do
        log "Starting to compact table $table"
        ssh $ssh_user@$yb_node \
            "$default_yb_path/bin/yb-admin -master_addresses $yb_nodes compact_table ysql.yugabyte $table $default_compaction_timeout" \
            > $results_dir/compact_$table.log 2>&1 &
        pids+=($!)
    done

    for pid in "${pids[@]}"; do
        wait $pid
        if [ $? -ne 0 ]; then
            log "Possibly failed to compact data on some/all hosts"
        fi
    done

    elapsed=$(( SECONDS - compaction_started ))
    log "Compacting tables done in $elapsed seconds"

    if [[ -n "$load_start" ]]; then
        total_time=$(( SECONDS - load_start ))
        log "Total load time: $total_time seconds"
    fi
fi

if [ -n "$no_run" ]; then
    exit 0
fi

for host in `cat $hosts_file`; do
    mkdir -p "$results_dir/$host"
done

log "Cleaning up previous results"
for host in `cat $hosts_file`; do
    ssh $host "cd $tpcc_path && rm -rf results_*"
done

log "Running benchmark"

log "Adjusting run time to $execute_time_seconds seconds"
parallel-ssh -h $unique_hosts \
    "sed -i 's:<runtime>[0-9]*</runtime>:<runtime>$execute_time_seconds</runtime>:' $tpcc_path/config/workload_all.xml"

# this is a quick hack to be able to run multiple instances of tpccbenchmark on the same host:
# it seems that tpccbenchmark will use same file for results, thus we need to run each instance
# in a separate directory
for host in `cat $hosts_file`; do
    ssh $host "rm -rf ${tpcc_path}_*;"
done

host_num=1
for host in `cat $hosts_file`; do
    ssh $host "cp -r $tpcc_path ${tpcc_path}_${host_num}"
    host_num=`expr $host_num + 1`
done

pids=()
host_num=1
start_warehouse_id=1
current_warmup_time_seconds=$warmup_time_seconds
current_delay_seconds=0
single_instance_warmup_time_seconds=`expr $warmup_time_seconds / $host_count`
sessions_per_host=`expr $max_sessions / $host_count`

for host in `cat $hosts_file`; do
    cmd="./tpccbenchmark --execute=true \
        --nodes=$yb_nodes \
        --warehouses $warehouses_per_host \
        --num-connections $sessions_per_host\
        --start-warehouse-id $start_warehouse_id \
        --total-warehouses=$warehouses \
        --warmup-time-secs=$current_warmup_time_seconds \
        --initial-delay-secs=$current_delay_seconds"

    log "Running tpcc on $host (host_num=$host_num): $cmd"

    ssh $host "cd ${tpcc_path}_${host_num} && $cmd" \
        > $results_dir/$host.$host_num.run.log 2>&1 &
    pids+=($!)

    host_num=`expr $host_num + 1`
    start_warehouse_id=`expr $start_warehouse_id + $warehouses_per_host`
    current_warmup_time_seconds=`expr $current_warmup_time_seconds - $single_instance_warmup_time_seconds`
    current_delay_seconds=`expr $current_delay_seconds + $single_instance_warmup_time_seconds`
done

for pid in "${pids[@]}"; do
    wait $pid
    if [ $? -ne 0 ]; then
        log "Failed to load data on some/all hosts"
        exit 1
    fi
done

log "Collecting results"

mkdir $results_dir/raw_results

host_num=1
for host in `cat $hosts_file`; do
    scp $host:${tpcc_path}_${host_num}/results/oltpbench.csv $results_dir/raw_results/$host_num.csv
    host_num=`expr $host_num + 1`
done

remote_temp_dir=`ssh $single_tpcc_host mktemp -d`
if [[ -z "$remote_temp_dir" ]]; then
    log "Failed to create remote temp dir on $single_tpcc_host"
    exit 1
fi

scp $results_dir/raw_results/*csv $single_tpcc_host:$remote_temp_dir/
ssh $single_tpcc_host \
    "cd $tpcc_path && ./tpccbenchmark --merge-results=true --dir=$remote_temp_dir --warehouses=$warehouses" \
    | tee $results_dir/aggregate.log 2>&1

if [[ -n "$remote_temp_dir" ]]; then
    ssh $single_tpcc_host "rm -rf $remote_temp_dir"
fi
