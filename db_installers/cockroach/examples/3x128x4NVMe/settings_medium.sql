SET CLUSTER SETTING rocksdb.ingest_backpressure.l0_file_count_threshold = 100;
SET CLUSTER SETTING schemachanger.backfiller.max_buffer_size = '5 GiB';
SET CLUSTER SETTING kv.snapshot_rebalance.max_rate = '128 MiB';
SET CLUSTER SETTING rocksdb.min_wal_sync_interval = '500us';
SET CLUSTER SETTING kv.range_merge.queue_enabled = false;
ALTER RANGE default CONFIGURE ZONE USING gc.ttlseconds = 600;
