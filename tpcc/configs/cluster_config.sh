# Cluster configs https://www.cockroachlabs.com/docs/v23.1/cluster-settings

CONCURRENCY_LIMIT="1024"
SNAPSHOT_REBALANCE_MAX_RATE="'32 MiB'"
SNAPSHOT_RECOVERY_MAX_RATE="'32 MiB'"
AUTOMATIC_COLLECTION_ENABLED=true
BACKFILLER_MAX_BUFFER_SIZE="'512 MiB'"
MIN_WAL_SYNC_INTERVAL="'0us'"
RANGE_MERGE_QUEUE_ENABLED=true

# GC TTL https://www.cockroachlabs.com/docs/v23.1/configure-replication-zones#gc-ttlseconds

GC_TTLSECONDS=14400
