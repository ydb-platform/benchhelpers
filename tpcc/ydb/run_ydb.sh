#!/bin/bash

export TZ=UTC
export LC_ALL=en_US.UTF-8

execute_time_seconds=60
warmup_time_seconds=60
loader_threads=16
min_shards=50
compaction_threads=10
compaction_auth=disabled
java_memory="2G"
log_dir="$HOME/tpcc_logs/ydb"
tpcc_path="$HOME/benchbase-ydb"
ydb_port=2135

# in total, i.e. for all TPC-C instances. We will calculate per instance value below
max_sessions=1000

# number of max sessions should be greater than this value
min_max_sessions_per_instance=50

usage() {
    echo "Usage: $0"
    echo "    --warehouses <N> \\"
    echo "    --config <config_template> --ydb-host <ydb_host> --database <DB> \\"
    echo "    --hosts <hosts_file> \\"
    echo "    [--ydb-port $ydb_port] \\"
    echo "    [--secure] \\"
    echo "    [--viewer-url http://ydb-host:8765] \\"
    echo "    [--compaction-threads <compaction_threads>] \\"
    echo "    [--compaction-auth <Disabled,OAuth,Login>] \\"
    echo "    [--skip-compaction] \\"
    echo "    [--run-phase-only] \\"
    echo "    [--log-dir <log_dir>] \\"
    echo "    [--time <time> --warmup <warmup>] \\"
    echo "    [--loader-threads <loader_threads>] \\"
    echo "    [--tpcc-path </path/to/YDB/benchbase>] \\"
    echo "    [--max-sessions $max_sessions] \\"
    echo "    [--min-shards $min_shards] \\"
    echo "    [--no-load] [--no-run] [--no-drop-create] \\"
    echo "    [--with-flames] [--with-perf-stat] [--with-psi] \\"
}

log() {
    echo "`date` tpcc_ydb: $@"
}

kill_tpcc() {
    log "Killing tpcc instances and clean previous results"
    if [[ -z "$hosts_file" ]]; then
        log "No hosts file specified, can't kill tpcc instances"
        return
    fi

    unique_hosts=`mktemp`
    sort -u $hosts_file > $unique_hosts

    parallel-ssh -h $unique_hosts -i 'pkill -9 -f "^/bin/bash.*tpcc.sh"; pkill -9 -f "^java.*benchbase.jar -b tpcc"' &>/dev/null
    parallel-ssh -h $unique_hosts -i "cd $tpcc_path && rm -rf results_*" &>/dev/null

    rm -f $unique_hosts
}

cleanup() {
    kill_tpcc
    exit 1
}

trap cleanup SIGINT SIGTERM

if ! which parallel-ssh >/dev/null; then
    echo "parallel-ssh not found, you should install pssh"
    exit 1
fi

if ! which ydb >/dev/null; then
    echo "ydb CLI not found, you might want to download it from ydb.tech and put to any dir in $PATH"
    exit 1
fi

for module in ydb numpy requests; do
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
    --ydb-host)
        ydb_host=$2
        shift;;
    --ydb-port)
        ydb_port=$2
        shift;;
    --secure)
        use_grpcs=1
        ;;
    --database)
        database=$2
        shift;;
    --min-shards)
        min_shards=$2
        shift;;
    --viewer-url)
        viewer_url=$2
        shift;;
    --compaction-threads)
        compaction_threads=$2
        shift;;
    --compaction-auth)
        compaction_auth=$2
        shift;;
    --skip-compaction)
        skip_compaction=1
        ;;
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
    --max-sessions)
        max_sessions=$2
        shift;;
    --with-flames)
        with_flames=1
        ;;
    --with-perf-stat)
        with_perf_stat=1
        ;;
    --with-psi)
        sample_psi=1
        ;;
    --log-dir)
        log_dir=$2
        shift;;
    --virtual-threads)
        virtual_threads=1
        ;;
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

if [ -z "$ydb_host" ]; then
    echo "Please specify the ydb_host"
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

# we need this hack to not force
# user accept manually cluster hosts
for host in `cat "$hosts_file" | sort -u`; do
    ssh -o StrictHostKeyChecking=no $host &>/dev/null &
done

this_dir=`dirname $0`
this_path=`readlink -f $this_dir`
tpcc_helper="$this_path/tpcc_helper.py"
table_full_compact="$this_path/table_full_compact.py"
flame_script="$this_path/../../flamegraph/flamegraph.sh"

cd $this_path
if [[ $? -ne 0 ]]; then
    echo "Can't change directory to $this_path"
    exit 1
fi

host_count=`wc -l $hosts_file | awk '{print $1}'`
presplit_shards_count=`expr $loader_threads \* $host_count`
if [[ $presplit_shards_count -lt $min_shards ]]; then
    presplit_shards_count=$min_shards
fi

if [[ -z "$use_grpcs" ]]; then
    endpoint="grpc://$ydb_host:$ydb_port"
else
    endpoint="grpcs://$ydb_host:$ydb_port"
fi

if [ -z "$database" ]; then
    echo "Please specify the database"
    usage
    exit 1
fi

if [[ -z "$viewer_url" ]]; then
    viewer_url="http://$ydb_host:8765"
fi

if [ -z "$viewer_url" ]; then
    echo "Please specify the viewer url"
    usage
    exit 1
fi

tpcc_script="$tpcc_path/scripts/tpcc.sh"
parallel-ssh -h $hosts_file -i 'test -e $tpcc_script || (echo tpcc.sh does not exist && exit 1)'
if [ $? -ne 0 ]; then
    echo "$tpcc_script not found on some/all hosts, install benchbase (check our build and README)"
    rm -f $unique_hosts
    exit 1
fi

kill_tpcc

if [ -z "$no_drop_create" ]; then
    log "Drop existing tables if exists and create new ones"
    $tpcc_helper --endpoint $endpoint --database $database \
        -w $warehouses -n $host_count --shard-count $presplit_shards_count $args \
        create
    if [[ $? -ne 0 ]]; then
        log "Failed to create tables"
        exit 1
    fi
fi

if [[ -n "$no_load" && -n "$no_run" ]]; then
    exit 0
fi

max_sessions_per_instance=$(( $max_sessions / $host_count ))
if [[ $max_sessions_per_instance -lt $min_max_sessions_per_instance ]]; then
    max_sessions=$min_max_sessions_per_instance
fi

log "Generating TPC-C configs and uploading to the hosts"

# For each host in $hosts we generate config file with the following name: config.<host_num>.xml,
# Note that host_num is line number in $hosts_file
$tpcc_helper \
    -w $warehouses \
    --shard-count $presplit_shards_count \
    generate-configs \
    --hosts $hosts_file \
    --input $config_template \
    --execute-time $execute_time_seconds \
    --warmup-time $warmup_time_seconds \
    --max-sessions $max_sessions_per_instance \
    --loader-threads $loader_threads \
    --ydb-host $ydb_host \
    --database $database

if [ $? -ne 0 ]; then
    echo "Failed to generate configs"
    exit 1
fi

# now upload configs to the hosts
node_num=1
scp_pids=()
for host in `cat $hosts_file`; do
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
for host in `cat $hosts_file`; do
    config="config.$node_num.xml"
    rm $config
    node_num=$((node_num + 1))
done

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

    pids=()
    host_num=1
    for host in `cat $hosts_file`; do
        config="config.$host_num.xml"
        args=`$tpcc_helper \
            -w $warehouses \
            -n $host_count \
            --shard-count $presplit_shards_count \
            get-load-args \
            --node-num $host_num`

        if [[ -n "$virtual_threads" ]]; then
            args="$args --virtual-threads"
        fi

        log "Running tpcc initial load on $host (host_num=$host_num) with args: $args"

        ssh $host "cd $tpcc_path && ./scripts/tpcc.sh --memory $java_memory -c $config $args" \
            > $results_dir/$host.$host_num.load.log 2>&1 &
        pids+=($!)

        host_num=`expr $host_num + 1`
    done

    for pid in "${pids[@]}"; do
        wait $pid
        if [ $? -ne 0 ]; then
            log "Failed to load data on some/all hosts"
            exit 1
        fi
    done

    elapsed=$(( SECONDS - load_start ))
    log "Loading data done in $elapsed seconds"

    log "Altering tables"
    alter_start=$SECONDS
    $tpcc_helper \
        --endpoint $endpoint --database $database \
        -w $warehouses --shard-count $min_shards \
        update-min-partitions

    if [[ $? -ne 0 ]]; then
        log "Failed to alter tables"
        exit 1
    fi

    elapsed=$(( SECONDS - alter_start ))
    log "Altered tables in $elapsed seconds"

    if [[ -z "$skip_compaction" ]]; then
        compaction_start=$SECONDS
        log "Compacting tables"
        for table in oorder district item warehouse customer order_line new_order stock history; do
            $table_full_compact --all --viewer-url "$viewer_url" --auth $compaction_auth --threads $compaction_threads ${database}/${table} 1>/dev/null
            if [[ $? -ne 0 ]]; then
                log "Failed to compact table $table"
                exit 1
            fi
        done

        elapsed=$(( SECONDS - compaction_start ))
        log "Compaction done in $elapsed seconds"
    fi

    # When we use bulk upsert, we have to add index after loading the data,
    # otherwise we create the index when create the table
    index_start=$SECONDS
    log "Started async index build"
    $tpcc_helper --endpoint $endpoint --database $database \
        -w $warehouses -n $host_count --shard-count $min_shards \
        index

    if [[ $? -ne 0 ]]; then
        log "Failed to start index build"
        exit 1
    fi

    log "Waiting for index build to finish"
    $tpcc_helper --endpoint $endpoint --database $database \
        -w $warehouses -n $host_count --shard-count $min_shards \
        wait-index

    elapsed=$(( SECONDS - index_start ))
    log "Built index in $elapsed seconds"

    # we have some issue with reporting OK and having index available
    # also above we changed min partitions, so we need to wait a bit

    if [[ $warehouses -ge 15000 ]]; then
        log "Sleeping 20m after altering min partitions and building index"
        sleep 20m
    else
        log "Sleeping 5m after altering min partitions and building index"
        sleep 5m
    fi
fi

if [ -n "$no_run" ]; then
    exit 0
fi

for host in `cat $hosts_file`; do
    mkdir -p "$results_dir/$host"
done

log "Running benchmark"

pids=()
host_num=1
for host in `cat $hosts_file`; do
    config="config.$host_num.xml"
    log "Running tpcc on $host (config $config)"
    args=`$tpcc_helper \
        -w $warehouses \
        -n $host_count \
        --shard-count $min_shards \
        get-start-args \
        --node-num $host_num`

    if [[ -n "$virtual_threads" ]]; then
        args="$args --virtual-threads"
    fi

    ssh $host "cd $tpcc_path && rm -rf "results_${host_num}" && ./scripts/tpcc.sh --memory $java_memory -d results_${host_num} -c $config $args" \
        > $results_dir/$host/$host_num.run.log 2>&1 &
    pids+=($!)
    host_num=`expr $host_num + 1`
done

# TODO: sleep in case of flames or pressure sampling
# Should 'busy-sleep' with pid status check
log "Sleeping while TPCC warms up for $warmup_time_seconds seconds"
sleep $warmup_time_seconds

if [[ -n "$with_flames" ]]; then
    if [[ -e $flame_script ]]; then
        ydb_hosts=`curl -s "$viewer_url/counters/hosts?dynamic_only=1" | sed 's/:[0-9]\+$//' | sort -u`
        for flame_host in $ydb_hosts; do
            short_host=`echo $flame_host | cut -d'.' -f1`
            ts=`date +%Y%m%d_%H%M_%S`
            svg="$results_dir/flamegraph_$ts_$short_host.svg"

            log "Running flamegraph on $flame_host"
            $flame_script $flame_host -o $svg
        done
    else
        log "Skipped flamegraph, no $flame_script"
    fi
fi

if [[ -n "$sample_psi" ]]; then
    ydb_hosts=`curl -s "$viewer_url/counters/hosts?dynamic_only=1" | sed 's/:[0-9]\+$//' | sort -u`
    for sample_host in $ydb_hosts; do
        log "Sampling pressure on $sample_host"
        ssh $sample_host "tail /proc/pressure/*"
    done
fi

if [[ -n "$with_perf_stat" ]]; then
    ydb_hosts=`curl -s "$viewer_url/counters/hosts?dynamic_only=1" | sed 's/:[0-9]\+$//' | sort -u`
    for perf_host in $ydb_hosts; do
        log "Running perf stat on $perf_host"
        ssh $perf_host 'sudo perf stat -a -d -d -d -- sleep 10'
    done
fi

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

$tpcc_helper aggregate $results_dir
