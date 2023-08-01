# How to run TPC-C on YDB

## Prerequisites

The TPC-C setup involves the following components:
1. Helper scripts located in this directory. Execute them on any random machine.
2. TPC-C client, which can be executed on the same machine as the helper scripts. However, for a reasonable YDB cluster, it is recommended to have multiple machines running the TPC-C client.
3. A running YDB cluster. While it can be on the same machines where the TPC-C client is executed, we strongly advise having separate machines for the YDB cluster.

Prerequisites to run helper scripts:
1. Install pssh.
2. Install the ydb and numpy Python packages using `pip3 install ydb numpy`.
3. [Download](https://ydb.tech/en/docs/downloads/) the latest YDB CLI and place it somewhere in your PATH.
4. Prepare a file containing a list of TPC-C hosts (separated by new lines) to run the TPC-C client on. Note that if your machine has multiple cores, you can run multiple instances of TPC-C on the same machine. For example:

```
cat << EOF > tpcc.hosts
machine1.com
machine1.com
machine2.com
machine2.com
EOF
```

Prerequisites to run TPC-C client:
1. Install Java-17.
2. Install YDB's [fork](https://github.com/ydb-platform/benchbase) of benchbase into your home foler on each machine.
You have two options: build it on your own or use the prebuilt benchbase-ydb.tgz available in this directory. To install the package, follow these steps:
```
./upload_benchbase.sh --package benchbase-ydb.tgz --hosts tpcc.hosts
```

## Running the benchmark

To run the benchmark, execute the following command:

```
mkdir -p $HOME/tpcc_logs
./run_ydb.sh                                \
    --ydb-host ydb-001.com                  \
    --database /Root/testdb                 \
    --config tpcc_config_template.xml       \
    --hosts tpcc.hosts                      \
    --warehouses 1000                       \
    --warmup 1200                           \
    --time 3600                             \
    --log-dir $HOME/tpcc_logs
```

Note, that warmup and time are in seconds. By default the benchmark uses just 16 loader threads, if your machines have enough cores (and YDB cluster has enough cores), you can increase the number of threads using the `--loader-threads` flag. In our runs we usually use 128 threads per machine (1 thread per core) and 8 machines to run the benchmark (YDB cluster has 384 cores in total).

If you have already executed the benchmark, you can use the `--run-phase-only` flag to reuse existing data and skip the loading phase. This will save you time on data generation.