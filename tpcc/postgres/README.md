# How to run TPC-C on PostgreSQL

## Prerequisites

The TPC-C setup involves the following components:
1. Helper scripts located in this directory. Execute them on any random machine.
2. TPC-C client, which can be executed on the same machine as the helper scripts. However, for a reasonable PostgreSQL cluster, it is recommended to have multiple machines running the TPC-C client.
3. A running PostgreSQL cluster. While it can be on the same machines where the TPC-C client is executed, we strongly advise having separate machines for the PostgreSQL cluster.

Prerequisites to run helper scripts:
1. Install pssh.
2. Install the ydb, numpy and requests Python packages using `pip3 install ydb numpy requests`.
3. Prepare a file containing a list of TPC-C hosts (separated by new lines) to run the TPC-C client on. Note that if your machine has multiple cores, you can run multiple instances of TPC-C on the same machine. For example:
```
cat << EOF > tpcc.hosts
machine1.com
machine1.com
machine2.com
machine2.com
EOF
```

4. Copy the tpcc_config_template.xml file to tpcc_config.xml and edit url, user, password.

Prerequisites to run TPC-C client:
1. Install Java-17.
2. Install YDB's [fork](https://github.com/ydb-platform/benchbase) (`postgres`` branch) of benchbase into your home foler on each machine.

To install the package, execute the following:
```
./upload_benchbase.sh --package benchbase-postgres.tgz --hosts tpcc.hosts
```

## Running the benchmark

To run the benchmark, execute the following command:

```
mkdir -p $HOME/tpcc_logs
./run_postgres.sh               \
    --warehouses 1000           \
    --config ~/tpcc_config.xml  \
    --hosts ~/tpcc.hosts        \
    --time 7200                 \
    --warmup 1200               \
    --java-memory 30G           \
    --max-connections 1000
```

Note, that warmup and time are in seconds. By default the benchmark uses just 16 loader threads.

If you have already executed the benchmark, you can use the `--run-phase-only` flag to reuse existing data and skip the loading phase. This will save you time on data generation. Also just to load the data and skip benchmark execution, use the `--no-run` flag.