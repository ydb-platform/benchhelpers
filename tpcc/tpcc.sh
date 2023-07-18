#!/bin/bash

set -e

debug=

if ! command -v parallel-ssh &> /dev/null
then
    echo "'parallel-ssh' could not be found in your PATH. You can install it using the command: 'sudo apt install pssh'."
    exit 1
fi

import_dataset=1

while [[ $# -gt 0 ]]; do case $1 in
    --config)
        TPCC_CONFIG=$2
        shift;;
    --cluster-config)
        CLUSTER_CONFIG=$2
        shift;;
    --without-import)
        import_dataset=0;;
esac; shift; done

if [[ ! -e "$TPCC_CONFIG" ]]; then
  echo "No cockroach config in path: $TPCC_CONFIG"
  exit 1
fi

source "$TPCC_CONFIG"


echo "Deploy"
$debug parallel-ssh -H "$TPCC_HOSTS" -t 0 "sudo mkdir -p $COCKROACH_DEPLOY_PATH"
$debug parallel-scp -H "$TPCC_HOSTS" -t 0 "$COCKROACH_TAR" "~"
$debug parallel-ssh -H "$TPCC_HOSTS" -t 0 "sudo tar -xzf ~/$(basename "$COCKROACH_TAR") --strip-component=1 -C $COCKROACH_DEPLOY_PATH; \
                                          rm -f $(basename "$COCKROACH_TAR")"


COCKROACH_HOST=$(echo "$COCKROACH_HOSTS" | tr ' ' '\n' | head -1)
IFS=', ' read -r -a TPCC_LIST <<< "$TPCC_HOSTS"

LOAD_DATASET_ARGS="--warehouses=$WAREHOUSES"


if [[ -e "$CLUSTER_CONFIG" ]]; then
    echo "Configure TPC-C importing"

    source "$CLUSTER_CONFIG"

    cluster_config="SET CLUSTER SETTING kv.dist_sender.concurrency_limit = $CONCURRENCY_LIMIT;
                    SET CLUSTER SETTING kv.snapshot_rebalance.max_rate = $SNAPSHOT_REBALANCE_MAX_RATE;
                    SET CLUSTER SETTING kv.snapshot_recovery.max_rate = $SNAPSHOT_RECOVERY_MAX_RATE;
                    SET CLUSTER SETTING sql.stats.automatic_collection.enabled = $AUTOMATIC_COLLECTION_ENABLED;
                    SET CLUSTER SETTING schemachanger.backfiller.max_buffer_size = $BACKFILLER_MAX_BUFFER_SIZE;
                    SET CLUSTER SETTING rocksdb.min_wal_sync_interval = $MIN_WAL_SYNC_INTERVAL;
                    SET CLUSTER SETTING kv.range_merge.queue_enabled = $RANGE_MERGE_QUEUE_ENABLED;
                    ALTER RANGE default CONFIGURE ZONE USING gc.ttlseconds = $GC_TTLSECONDS;"

    $debug parallel-ssh -H "$COCKROACH_HOST" "echo \"$cluster_config\" | sudo tee query.sql; \
                                              sudo $PATH_TO_COCKROACH/cockroach sql --insecure --file query.sql --host=$COCKROACH_HOST
                                              sudo rm -f query.sql"

    LOAD_DATASET_ARGS="$LOAD_DATASET_ARGS --replicate-static-columns --partition-strategy=leases"
fi


if [ $import_dataset -eq 1 ]; then
  echo "Drop TPC-C tables"
  drop_tables="DROP table tpcc.warehouse cascade; \
               DROP table tpcc.item cascade; \
               DROP table tpcc.stock cascade; \
               DROP table tpcc.district cascade; \
               DROP table tpcc.customer cascade; \
               DROP table tpcc.history cascade; \
               DROP table tpcc.order cascade; \
               DROP table tpcc.new_order cascade; \
               DROP table tpcc.order_line cascade;"

  $debug parallel-ssh -H "$COCKROACH_HOST" "$PATH_TO_COCKROACH/cockroach sql --insecure --execute=\"$drop_tables\" --host=$COCKROACH_HOST; echo DONE"

  echo "Import the TPC-C dataset"
  $debug parallel-ssh -t 0 -P -H "${TPCC_LIST[0]}" "$COCKROACH_DEPLOY_PATH/cockroach workload fixtures import tpcc \
                                  $LOAD_DATASET_ARGS \
                                  'postgres://root@$COCKROACH_HOST:$COCKROACH_LISTEN_PORT?sslmode=disable'"
  $debug sleep 5s
fi


COCKROACH_ADDRS=$(echo "$COCKROACH_HOSTS" | tr ' ' '\n' | sed "s#.*#postgres://root@&:$COCKROACH_LISTEN_PORT?sslmode=disable#" | tr '\n' ' ')

echo "Run TPC-C"
$debug parallel-ssh -t 0 -H "$TPCC_HOSTS" "cd $COCKROACH_DEPLOY_PATH; ulimit -n 500000 && sudo ./cockroach workload run tpcc \
                                                                        --warehouses=$WAREHOUSES \
                                                                        --ramp=$RAMP \
                                                                        --duration=$DURATION \
                                                                        --histograms=workload.histogram.ndjson \
                                                                        $COCKROACH_ADDRS"
$debug sleep 5s

echo "Collect results"
for index in "${!TPCC_LIST[@]}"
do
    $debug parallel-ssh -H "${TPCC_LIST[index]}" "sudo mv $COCKROACH_DEPLOY_PATH/workload.histogram.ndjson $COCKROACH_DEPLOY_PATH/workload$index.histogram.ndjson;"
    $debug scp "${TPCC_LIST[index]}":"$COCKROACH_DEPLOY_PATH"/workload"$index".histogram.ndjson .
    $debug scp ./workload"$index".histogram.ndjson "${TPCC_LIST[0]}":~
    $debug parallel-ssh -H "${TPCC_LIST[0]}" "sudo mv workload$index.histogram.ndjson $COCKROACH_DEPLOY_PATH"
done

$debug parallel-scp -t 0 -H "${TPCC_LIST[0]}" "$WORKLOAD_PATH/workload" "~"
$debug parallel-ssh -t 0 -P -H "${TPCC_LIST[0]}" "sudo mv ~/workload $COCKROACH_DEPLOY_PATH;  \
                                                  sudo $COCKROACH_DEPLOY_PATH/workload debug tpcc-merge-results \
                                                      --warehouses=$((WAREHOUSES*${#TPCC_LIST[@]})) \
                                                      $COCKROACH_DEPLOY_PATH/workload*.histogram.ndjson"
