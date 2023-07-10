#!/bin/bash

debug=

log() {
    echo "`date`: $@"
}

load_data() {
    what=$1

    if [ "$TYPE" = "yugabyte" ]; then
        log "Prepare YCSB keyspace"
        log "$YU_PATH/bin/ycqlsh -f $YU_PATH/yugabyte_cql.sql $TARGET"
        $debug parallel-ssh -H "$TARGET" -p 1 "$YU_PATH/bin/ycqlsh -f $YU_PATH/yugabyte_cql.sql $TARGET"
    fi

    if [ "$TYPE" = "yugabyteSQL" ]; then
        log "Prepare YCSB table"
        $debug parallel-ssh -H "$TARGET" -p 1 "$YU_PATH/bin/ysqlsh -f $YU_PATH/yugabyte_sql.sql -h $TARGET"
    fi

    start_ts=`date +%s`
    cmd=`eval echo "$cmd_init_template"`
    log "Loading data: $cmd"
    $debug ssh $load_node "$cmd"
    status=$?

    if [ "$TYPE" = "cockroach" ]; then
        # sometimes export finishes with CLI error, but continues in cockroach
        # we need to wait
        if [[ $status -ne 0 ]]; then
            ts=`date +%s`
            delta="$(($COCKROACH_INIT_SLEEP_TIME_MINUTES-($ts-$start_ts)/60))"
            log "Sleeping more minutes: $delta"
            $debug sleep ${delta}m
        fi

        log "Setting MVCC"
        $debug ssh $load_node "$COCKROACH sql --insecure --host $HA_PROXY_NODE --execute 'ALTER TABLE ycsb.usertable CONFIGURE ZONE USING gc.ttlseconds = 600;'"
    fi

    log "Finished loading data"
}

run_workload () {
    what=$1
    threads=$2
    host_list=$3
    c=`echo "$host_list" | wc -w`

    log "$distribution workload $what from $c ycsb instances started on `date +%s`"

    cmd=`eval echo "$cmd_run_template"`
    log "$cmd"
    $debug parallel-ssh -i -t 0 -H "$host_list" -p 30 "$cmd"

    log "done"
}

run_workloads () {
    host_list="$1"
    distribution="$2"
    c=`echo "$host_list" | wc -w`

    for workload in $WORKLOADS; do
        $debug sleep $SLEEP_TIME
        run_workload $workload $YCSB_THREADS "$host_list"
    done
}

usage () {
    echo "run_workloads_impl.sh [--type ydb|cockroach|yugabyte|yugabyteSQL|postgresql] [--threads N] [--ycsb-nodes N] workload.rc cluster.rc"
}

##### MAIN

if ! command -v parallel-ssh &> /dev/null
then
    echo "`parallel-ssh` could not be found in your PATH"
    exit 1
fi

TYPE=ydb

while [[ $# -gt 0 ]]; do case $1 in
    --type)
        TYPE=$2
        shift;;
    --threads)
        THREADS=$2
        shift;;
    --de-threads)
        DE_THREADS=$2
        shift;;
    --ycsb-nodes)
        YCSB_NODES_COUNT_OVERWRITE=$2
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

for source in $source_files; do
    if [[ ! -s "$source" ]]; then
        echo "Can't source file: $source"
        exit 1
    fi
    log "Source $source"
    . "$source"
done

PATH_TO_SCRIPT=$(dirname "$0")

if [ "$TYPE" == "yugabyte" ]; then

  if [ ! -n "$YU_DB_CONFIG" ]; then
    echo "Missing path to db.properties. Please, declare a variable YU_DB_CONFIG."
    exit 1
  fi

  if [ ! -n "$YU_YCSB_TAR_PATH" ] && [ ! -n "$YU_YCSB_PATH" ]; then
    echo "Missing path to YCSB. Please, declare a variable YU_YCSB_PATH, or YU_YCSB_TAR_PATH for deploy."
    exit 1
  fi

  parallel-ssh -H "$YCSB_NODES" "sudo mkdir -p $YU_YCSB_DEPLOY_PATH;"

  parallel-scp -H "$TARGET" "$PATH_TO_SCRIPT/sources/yugabyte/yugabyte_cql.sql" "~"
  parallel-scp -H "$TARGET" "$PATH_TO_SCRIPT/sources/yugabyte/yugabyte_sql.sql" "~"
  parallel-ssh -H "$TARGET" "sudo mv ~/yugabyte_cql.sql $YU_PATH; \
                             sudo mv ~/yugabyte_sql.sql $YU_PATH"

  db_config_name=$(basename "$YU_DB_CONFIG")
  parallel-scp -H "$YCSB_NODES" "$YU_DB_CONFIG" "~"
  parallel-ssh -H "$YCSB_NODES" "sudo mv ~/$db_config_name $YU_YCSB_DEPLOY_PATH/db.properties"

  if [ -n "$YU_YCSB_TAR_PATH" ]; then
    tar_name=$(basename "$YU_YCSB_TAR_PATH")
    parallel-scp -H "$YCSB_NODES" "$YU_YCSB_TAR_PATH" "~"
    log "sudo tar -xzvf $YU_YCSB_DEPLOY_PATH/$tar_name -C $YU_YCSB_DEPLOY_PATH"
    parallel-ssh -H "$YCSB_NODES" "sudo mv ~/$tar_name $YU_YCSB_DEPLOY_PATH; \
                                   sudo tar -xzf $YU_YCSB_DEPLOY_PATH/$tar_name --strip-components=1 -C $YU_YCSB_DEPLOY_PATH; \
                                   sudo rm -f $YU_YCSB_DEPLOY_PATH/$tar_name"
    YU_YCSB_PATH=$YU_YCSB_DEPLOY_PATH
  fi
fi


if [ "$TYPE" == "ydb" ]; then
  if [ -n "$YCSB_TAR_PATH" ]; then
    parallel-ssh -H "$YCSB_NODES" "sudo mkdir -p $YCSB_DEPLOY_PATH"
    tar_name=$(basename "$YCSB_TAR_PATH")
    parallel-scp -H "$YCSB_NODES" "$YCSB_TAR_PATH" "~"
    log "sudo tar -xzvf $YCSB_DEPLOY_PATH/$tar_name -C $YCSB_DEPLOY_PATH"
    parallel-ssh -H "$YCSB_NODES" "sudo mv ~/$tar_name $YCSB_DEPLOY_PATH; \
                                   sudo tar -xzf $YCSB_DEPLOY_PATH/$tar_name --strip-components=1 -C $YCSB_DEPLOY_PATH; \
                                   sudo rm -f $YCSB_DEPLOY_PATH/$tar_name"
    YCSB_PATH=$YCSB_DEPLOY_PATH
  fi
fi

if [ -n "$THREADS" ]; then
    YCSB_THREADS=$THREADS
fi

if [ -z $YCSB_THREADS ]; then
    YCSB_THREADS=64
fi

if [ -n "$DE_THREADS" ]; then
    YCSB_THREADS_DE=$DE_THREADS
fi

if [ -z $YCSB_THREADS_DE ]; then
    YCSB_THREADS_DE=512
fi

if [ -n "$YCSB_NODES_COUNT_OVERWRITE" ]; then
    YCSB_NODES_COUNT=$YCSB_NODES_COUNT_OVERWRITE
fi

if [[ -z "$COCKROACH_INIT_SLEEP_TIME_MINUTES" ]]; then
    # assume 6M rows per minute
    COCKROACH_INIT_SLEEP_TIME_MINUTES=$(($RECORD_COUNT / 6000000 + 1))
fi

hosts="$YCSB_NODES"

# we need this hack  to not force
# user accept manually cluster hosts
for host in $hosts; do
    $debug ssh -o StrictHostKeyChecking=no $host &>/dev/null &
done

# on this node we run "ycsb load"
load_node=`echo "$hosts" | head -1`

ycsb_workloads="$YCSB_PATH/workloads"

if [ -z "$LOAD_YCSB_THREADS" ]; then
    LOAD_YCSB_THREADS=$YCSB_THREADS
fi

if [ "$KEY_ORDER" = "ordered" ]; then
    INSERT_HASH="false"
else
    INSERT_HASH="true"
fi

if [ "$TYPE" = "ydb" ]; then
    # note that we should use ycsb.sh, because it will source user's profile/bashrc
    # which possibly contain Java setup
    cmd_init_template='YDB_ANONYMOUS_CREDENTIALS=1 $YCSB_PATH/bin/ycsb.sh load ydb -P $YCSB_PATH/workloads/workload${what} -p dsn=grpc://${TARGET}:${STATIC_NODE_GRPC_PORT}${TEST_DB} -p dropOnInit=true -p splitByLoad=true -p recordcount=$RECORD_COUNT -p import=true -p insertorder=$KEY_ORDER -p maxparts=$MAX_PARTS -p maxpartsizeMB=$MAX_PART_SIZE_MB'
    cmd_run_template='YDB_ANONYMOUS_CREDENTIALS=1 $YCSB_PATH/bin/ycsb.sh run ydb -P $YCSB_PATH/workloads/workload${what} -p dsn=grpc://${TARGET}:${STATIC_NODE_GRPC_PORT}${TEST_DB} -threads $threads -p insertorder=$KEY_ORDER -p recordcount=$RECORD_COUNT -p operationcount=$OP_COUNT -p requestdistribution=$distribution -p maxexecutiontime=$MAX_EXECUTION_TIME_SECONDS'
elif [ "$TYPE" = "ydbu" ]; then
    # note that we should use ycsb.sh, because it will source user's profile/bashrc
    # which possibly contain Java setup
    cmd_init_template='YDB_ANONYMOUS_CREDENTIALS=1 $YCSB_PATH/bin/ycsb.sh load ydb -P $YCSB_PATH/workloads/workload${what} -p dsn=grpc://${TARGET}:${STATIC_NODE_GRPC_PORT}${TEST_DB} -p dropOnInit=true -p splitByLoad=true -p recordcount=$RECORD_COUNT -p import=true -p insertorder=$KEY_ORDER -p maxparts=$MAX_PARTS -p maxpartsizeMB=$MAX_PART_SIZE_MB'
    cmd_run_template='YDB_ANONYMOUS_CREDENTIALS=1 $YCSB_PATH/bin/ycsb.sh run ydb -P $YCSB_PATH/workloads/workload${what} -p dsn=grpc://${TARGET}:${STATIC_NODE_GRPC_PORT}${TEST_DB} -threads $threads -p insertorder=$KEY_ORDER -p recordcount=$RECORD_COUNT -p operationcount=$OP_COUNT -p requestdistribution=$distribution -p maxexecutiontime=$MAX_EXECUTION_TIME_SECONDS -p forceUpdate=true'
elif [ "$TYPE" = "cockroach" ]; then
    cmd_init_template='$COCKROACH workload init ycsb --data-loader=IMPORT --drop --insert-count $RECORD_COUNT --insert-hash=$INSERT_HASH "postgresql://root@$HA_PROXY_NODE:26257?sslmode=disable" --concurrency $LOAD_YCSB_THREADS --workload $what'
    cmd_run_template='sh -c \"2\>\&1 $COCKROACH workload run ycsb --workload $what --request-distribution $distribution --insert-count $RECORD_COUNT --max-ops $OP_COUNT --insert-hash=$INSERT_HASH --display-every 10001s "postgresql://root@$HA_PROXY_NODE:26257?sslmode=disable" --concurrency $threads --duration ${MAX_EXECUTION_TIME_SECONDS}s \"'
elif [ "$TYPE" = "yugabyte" ]; then
    cmd_init_template='$YU_YCSB_PATH/bin/ycsb.sh load yugabyteCQL -P $YU_YCSB_PATH/db.properties -P $YU_YCSB_PATH/workloads/workload${what} -p recordcount=$RECORD_COUNT -p insertorder=$KEY_ORDER -p threadcount=$LOAD_YCSB_THREADS'
    cmd_run_template='$YU_YCSB_PATH/bin/ycsb.sh run yugabyteCQL -P $YU_YCSB_PATH/db.properties -P $YU_YCSB_PATH/workloads/workload${what} -p threadcount=$threads -p insertorder=$KEY_ORDER -p recordcount=$RECORD_COUNT -p operationcount=$OP_COUNT -p requestdistribution=$distribution -p maxexecutiontime=$MAX_EXECUTION_TIME_SECONDS'
elif [ "$TYPE" = "yugabyteSQL" ]; then
    cmd_init_template='$YU_YCSB_PATH/bin/ycsb.sh load yugabyteSQL -P $YU_YCSB_PATH/db.properties -P $YU_YCSB_PATH/workloads/workload${what} -p recordcount=$RECORD_COUNT -p insertorder=$KEY_ORDER -p threadcount=$LOAD_YCSB_THREADS'
    cmd_run_template='$YU_YCSB_PATH/bin/ycsb.sh run yugabyteSQL -P $YU_YCSB_PATH/db.properties -P $YU_YCSB_PATH/workloads/workload${what} -p threadcount=$threads -p insertorder=$KEY_ORDER -p recordcount=$RECORD_COUNT -p operationcount=$OP_COUNT -p requestdistribution=$distribution -p maxexecutiontime=$MAX_EXECUTION_TIME_SECONDS'
elif [ "$TYPE" = "postgresql" ]; then
    cmd_init_template='$GO_YCSB_PATH/bin/go-ycsb load postgresql -p pg.host=$PG_HOST -p pg.port=$PG_PORT -p pg.user=$PG_USER -p pg.password=$PG_PASSWORD -p pg.db=$PG_DB -p pg.sslmode=verify-full -P $GO_YCSB_PATH/workloads/workload${what} -p recordcount=$RECORD_COUNT -p insertorder=$KEY_ORDER -p threadcount=$LOAD_YCSB_THREADS -p dropdata=true --interval 600'
    cmd_run_template='$GO_YCSB_PATH/bin/go-ycsb run postgresql -p pg.host=$PG_HOST -p pg.port=$PG_PORT -p pg.user=$PG_USER -p pg.password=$PG_PASSWORD -p pg.db=$PG_DB -p pg.sslmode=verify-full -P $GO_YCSB_PATH/workloads/workload${what} --threads $threads -p insertorder=$KEY_ORDER -p recordcount=$RECORD_COUNT -p operationcount=$OP_COUNT -p requestdistribution=$distribution --interval 600 -p maxexecutiontime=$MAX_EXECUTION_TIME_SECONDS'
else
    log "Unknown type: $TYPE"
    exit 1
fi

need_load=1

if [[ -n "$WORKLOADS" ]]; then
    for distribution in $DISTRIBUTIONS; do
        need_load=
        # load initial data
        if [[ ! -z "$LOAD_DATA" ]]; then
            log "load workload $LOAD_DATA data for $distribution"
            load_data $LOAD_DATA
        fi

        running_hosts=`echo "$hosts" | head -$YCSB_NODES_COUNT | tr '\n' ' '`
        OP_COUNT=`expr $OP_COUNT_TOTAL / $YCSB_NODES_COUNT + 1`
        run_workloads "$running_hosts" $distribution
    done
fi

if [[ -n $RUN_WORKLOAD_D ]]; then
    running_hosts=`echo "$hosts" | head -1 | tr '\n' ' '`
    distribution=latest
    if [[ -n "$need_load" ]]; then
        $debug sleep $SLEEP_TIME
        load_data d
    fi

    need_load=1

    $debug sleep $SLEEP_TIME
    OP_COUNT=$OP_COUNT_TOTAL
    run_workload d $YCSB_THREADS_DE "$running_hosts"
fi

if [[ -n $RUN_WORKLOAD_E ]]; then
    running_hosts=`echo "$hosts" | head -1 | tr '\n' ' '`
    distribution=zipfian
    if [[ -n "$need_load" ]]; then
        $debug sleep $SLEEP_TIME
        load_data e
    fi
    $debug sleep $SLEEP_TIME
    OP_COUNT=$OP_COUNT_E
    run_workload e $YCSB_THREADS_DE "$running_hosts"
fi
