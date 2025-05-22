#!/bin/bash

export TZ=UTC
export LC_ALL=en_US.UTF-8

if [[ -x ./venv/bin/activate ]]; then
    source ./venv/bin/activate
else
    echo "No venv found"
fi


execute_time_seconds=300
warmup_time_seconds=60

loader_threads=16

compaction_threads=10

java_memory="2G"

log_dir="$HOME/tpcc_logs/ydb"
tpcc_path="$HOME/benchbase-ydb"

ydb_port=2135

# Re-defined below, if not configured via command line
viewer_url=""

# in total, i.e. for all TPC-C instances. We will calculate per instance value below
max_sessions=1000

# number of max sessions should be greater than this value
min_max_sessions_per_instance=50

perf_measure_user=$USER

usage() {
    echo "Usage: $0"
    echo "    --warehouses <N> \\"
    echo "    --config <config_template> --ydb-host <ydb_host> --database <DB> \\"
    echo "    --hosts <hosts_file> \\"
    echo "    [--ydb-port $ydb_port] \\"
    echo "    [--secure] \\"
    echo "    [--viewer-url http://ydb-host:8765] \\"
    echo "    [--compaction-threads <compaction_threads>] \\"
    echo "    [--skip-compaction] \\"
    echo "    [--run-phase-only] \\"
    echo "    [--log-dir <log_dir>] \\"
    echo "    [--time <time> --warmup <warmup>] \\"
    echo "    [--loader-threads <loader_threads>] \\"
    echo "    [--tpcc-path </path/to/YDB/benchbase>] \\"
    echo "    [--max-sessions $max_sessions] \\"
    echo "    [--no-load] [--no-run] [--no-drop-create] \\"
    echo "    [--with-flames] [--with-perf-stat] [--with-psi] \\"
    echo "    [--perf-measure-user <user>] \\"
}

log() {
    echo "`date '+%Y-%m-%d %H:%M UTC'` tpcc_ydb: $@"
}

kill_tpcc() {
    log "Killing tpcc instances and clean previous results"
    if [[ -z "$hosts_file" ]]; then
        log "No hosts file specified, can't kill tpcc instances"
        return
    fi

    parallel-ssh -h $unique_hosts -i 'pkill -9 -f "^/bin/bash.*tpcc.sh"; pkill -9 -f "^java.*benchbase.jar -b tpcc"' &>/dev/null
    parallel-ssh -h $unique_hosts -i "cd $tpcc_path && rm -rf results_*" &>/dev/null
}

cleanup() {
    if [[ -n "$unique_hosts" ]]; then
        rm -f $unique_hosts
    fi
    kill_tpcc
    exit 1
}

# not real function, because depends on env
run_compaction() {
    compaction_start=$SECONDS
    log "Compacting tables"
    for table in oorder district item warehouse customer order_line new_order stock history; do
        $table_full_compact --all \
            --viewer-url "$viewer_url" \
            $compaction_auth_args \
            --threads $compaction_threads \
            ${database}/${table} 1>/dev/null
        if [[ $? -ne 0 ]]; then
            log "Failed to compact table $table"
            exit 1
        fi
    done

    elapsed=$(( SECONDS - compaction_start ))
    log "Compaction done in $elapsed seconds"
}

trap cleanup SIGINT SIGTERM

if ! which parallel-ssh >/dev/null; then
    echo "parallel-ssh not found, you should install pssh"
    exit 1
fi

if ! command -v ydb >/dev/null; then
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
    --ydb-host-user)
        ydb_host_user=$2
        shift;;
    --ydb-bin-path)
        ydb_bin_path=$2
        shift;;
    --database)
        database=$2
        shift;;
    --secure)
        use_grpcs=1
        ;;
    --disable-fast-log)
        disable_fast_log=1
        ;;
    --token-file)
        token_file_path=$2
        shift;;
    --sa-key-file)
        sa_key_file_path=$2
        shift;;
    --ca-file)
        ca_file_path=$2
        shift;;
    --viewer-url)
        viewer_url=$2
        shift;;
    --compaction-threads)
        compaction_threads=$2
        shift;;
    --skip-compaction)
        skip_compaction=1
        ;;
    --compact)
        compact_tables=1
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
        only_run=1
        ;;
    --only-run)
        only_run=1
        ;;
    --run-only)
        only_run=1
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
    --perf-measure-user)
        perf_measure_user=$2
        shift;;
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

if [ -n "$only_run" ]; then
    no_load=1
    no_drop_create=1
fi

if [ -n "$no_load" ]; then
    no_drop_create=1
fi

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

unique_hosts=`mktemp`
sort -u $hosts_file > $unique_hosts

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

host_count=`cat $hosts_file | grep -v '^$' | wc -l | awk '{print $1}'`

if [ -z "$database" ]; then
    echo "Please specify the database"
    usage
    exit 1
fi

if [[ -z "$viewer_url" ]]; then
    viewer_url="http://$ydb_host:8765"
fi

tpcc_script="$tpcc_path/scripts/tpcc.sh"
parallel-ssh -h $hosts_file -i 'test -e $tpcc_script || (echo tpcc.sh does not exist && exit 1)'
if [ $? -ne 0 ]; then
    echo "$tpcc_script not found on some/all hosts, install benchbase (check our build and README)"
    exit 1
fi

gen_config_args=

if [[ -n "$ca_file_path" ]]; then
    if [[ ! -r "$ca_file_path" ]]; then
        echo "CA file not found: $ca_file_path"
        exit 1
    fi
    log "Using CA file: $ca_file_path, enforcing secure connection"
    use_grpcs=1

    parallel-scp -h $unique_hosts $ca_file_path $tpcc_path/ &>/dev/null
    if [[ $? -ne 0 ]]; then
        log "Failed to copy $ca_file_path file to the tpcc hosts"
        exit 1
    fi

    gen_config_args="$gen_config_args --ca-file `basename $ca_file_path`"
fi

if [[ -z "$YDB_ANONYMOUS_CREDENTIALS" ]]; then
    if [[ -n "$token_file_path" ]]; then
        if [[ ! -r "$token_file_path" ]]; then
            echo "Token file not found: $token_file_path"
            exit 1
        fi
        log "Using token file: $token_file_path"
        export YDB_ACCESS_TOKEN_CREDENTIALS=`cat $token_file_path`
        export YDB_TOKEN="$YDB_ACCESS_TOKEN_CREDENTIALS"
        export YDB_TOKEN_FILE="$token_file_path"

        parallel-scp -h $unique_hosts $token_file_path $tpcc_path/ &>/dev/null
        if [[ $? -ne 0 ]]; then
            log "Failed to copy $token_file_path file to the tpcc hosts"
            exit 1
        fi

        # TODO: not yet supported
        skip_compaction=1
    elif [[ -n "$sa_key_file_path" ]]; then
        if [[ ! -r "$sa_key_file_path" ]]; then
            echo "Service account key file not found: $sa_key_file_path"
            exit 1
        fi
        log "Using service account key file: $sa_key_file_path"
        export SA_KEY_FILE="$sa_key_file_path"
        export YDB_SERVICE_ACCOUNT_KEY_FILE_CREDENTIALS="$SA_KEY_FILE"

        parallel-scp -h $unique_hosts $sa_key_file_path $tpcc_path/ &>/dev/null
        if [[ $? -ne 0 ]]; then
            log "Failed to copy $sa_key_file_path file to the tpcc hosts"
            exit 1
        fi

        # TODO: not yet supported
        skip_compaction=1
    elif [[ -n "$YDB_USER" && -n "$YDB_PASSWORD" ]]; then
        log "Using static creds from environment, YDB_USER: $YDB_USER, YDB_PASSWORD: ***"
        # TODO: not yet supported
        skip_compaction=1
    else
        log "Using anonymous access"
        export YDB_ANONYMOUS_CREDENTIALS=1
    fi
fi

if [[ -z "$use_grpcs" ]]; then
    endpoint="grpc://$ydb_host:$ydb_port"
else
    endpoint="grpcs://$ydb_host:$ydb_port"
    gen_config_args="$gen_config_args --secure"
fi

# Unfortunately, ydb CLI can't show the version and we try to get it via HTTP. No auth yet.
version_info=`curl -s $ydb_host:8765/ver --connect-timeout 5 --max-time 10 | egrep '(Git|Arc) info:' -A 13`
if [[ $? -ne 0 ]]; then
    log "Failed to get YDB version via HTTP"
else
    log "YDB version info:"
    echo "$version_info"
fi

kill_tpcc

if [ -z "$no_drop_create" ]; then
    log "Drop existing tables if exists and create new ones"
    $tpcc_helper --endpoint $endpoint --database $database \
        -w $warehouses -n $host_count \
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

total_loader_threads=$(( $loader_threads * $host_count ))
if [[ $total_loader_threads -gt $warehouses ]]; then
    loader_threads=$(( $warehouses / $host_count + 1 ))
    log "Reducing loader threads to $loader_threads per instance"
fi

log "Generating TPC-C configs and uploading to the hosts"

# For each host in $hosts we generate config file with the following name: config.<host_num>.xml,
# Note that host_num is line number in $hosts_file
$tpcc_helper \
    -w $warehouses \
    generate-configs \
    $gen_config_args \
    --hosts $hosts_file \
    --input $config_template \
    --execute-time $execute_time_seconds \
    --warmup-time $warmup_time_seconds \
    --max-sessions $max_sessions_per_instance \
    --loader-threads $loader_threads \
    --ydb-host $ydb_host \
    --database $database \

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

if [[ -n "$disable_fast_log" ]]; then
    # TODO: support auth. Now, the problem is that we have remote YDBD binary and have to copy tokens/env
    # to the remote host. If we had a local YDBD binary, that would be easy, because the env and tokens were set
    if [[ -z $YDB_ANONYMOUS_CREDENTIALS ]]; then
        echo "At this moment fast log off is not supported with non-anonymous access"
        exit 1
    fi

    if [[ -z "$ydb_host_user" ]]; then
        echo "Please specify the ydb_host_user"
        usage
        exit 1
    fi

    if [[ -z "$ydb_bin_path" ]]; then
        echo "Please specify the ydb_bin_path"
        usage
        exit 1
    fi

    scheme_cmd=`mktemp`
    cat $this_path/fast_log_off.txt | sed "s:WORKING_DIR_PATH:$database:" > $scheme_cmd

    scp $scheme_cmd $ydb_host_user@$ydb_host:
    if [[ $? -ne 0 ]]; then
        log "Failed to upload fast_log_off.txt to $ydb_host_user@$ydb_host"
        rm $scheme_cmd
        exit 1
    fi

    scheme_cmd_name=`basename $scheme_cmd`
    ydb_scheme_cmd="$ydb_bin_path -s $endpoint db schema execute $scheme_cmd_name"
    ssh $ydb_host_user@$ydb_host "$ydb_scheme_cmd"
    if [[ $? -ne 0 ]]; then
        log "Failed to execute fast log off scheme"
        ssh $ydb_host_user@$ydb_host "rm $scheme_cmd_name"
        rm $scheme_cmd
        exit 1
    fi

    ssh $ydb_host_user@$ydb_host "rm $scheme_cmd_name"
    rm $scheme_cmd

    log "Fast log off scheme executed"
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

    $tpcc_helper --endpoint $endpoint --database $database \
        -w $warehouses -n $host_count \
        enable-split-by-load
    if [[ $? -ne 0 ]]; then
        log "Failed to enable split by load"
        exit 1
    fi

    if [[ -z "$skip_compaction" ]]; then
        run_compaction
        skip_compaction=1
    fi

    # When we use bulk upsert, we have to add index after loading the data,
    # otherwise we create the index when create the table
    index_start=$SECONDS
    log "Started async index build"
    $tpcc_helper --endpoint $endpoint --database $database \
        -w $warehouses -n $host_count \
        index

    if [[ $? -ne 0 ]]; then
        log "Failed to start index build"
        exit 1
    fi

    log "Waiting for index build to finish"
    $tpcc_helper --endpoint $endpoint --database $database \
        -w $warehouses -n $host_count \
        wait-index

    elapsed=$(( SECONDS - index_start ))
    log "Built index in $elapsed seconds"

    # we have some issue with reporting OK and having index available
    log "Sleeping 5m after building index"
    sleep 5m
fi

if [[ -n "$compact_tables" && -z "$skip_compaction" ]]; then
    run_compaction
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

    if [[ -n "$virtual_threads" ]]; then
        args="$args --virtual-threads"
    fi

    ssh $host "cd $tpcc_path && ./scripts/tpcc.sh --memory $java_memory -d results_${host_num} -c $config $args" \
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
            $flame_script "$perf_measure_user@$flame_host" -o $svg
        done
    else
        log "Skipped flamegraph, no $flame_script"
    fi
fi

if [[ -n "$sample_psi" ]]; then
    ydb_hosts=`curl -s "$viewer_url/counters/hosts?dynamic_only=1" | sed 's/:[0-9]\+$//' | sort -u`
    for sample_host in $ydb_hosts; do
        log "Sampling pressure on $sample_host"
        ssh "$perf_measure_user@$sample_host" "tail /proc/pressure/*"
    done
fi

if [[ -n "$with_perf_stat" ]]; then
    ydb_hosts=`curl -s "$viewer_url/counters/hosts?dynamic_only=1" | sed 's/:[0-9]\+$//' | sort -u`
    for perf_host in $ydb_hosts; do
        log "Running perf stat on $perf_host"
        ssh "$perf_measure_user@$perf_host" 'sudo perf stat -a -d -d -d -- sleep 10'
    done
fi

log "Running benchmark main phase remaining time is approximately $execute_time_seconds seconds"

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
    scp -r $host:$tpcc_path/results_* ./
    cd -
done

log "Aggregating total result"

$tpcc_helper aggregate $results_dir
