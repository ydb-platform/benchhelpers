# How to run TPC-C on Oracle Database

## Prerequisites

The TPC-C setup involves the following components:

1. Helper scripts located in this directory. Execute them on any random machine.
2. TPC-C clients, which can be executed on the same machine as the helper scripts. However, for a reasonable PostgreSQL cluster, it is recommended to have multiple machines running the TPC-C client. Please, check hardware [requirements](https://github.com/ydb-platform/tpcc#hardware-requirements) for TPC-C clients.
3. A running PostgreSQL cluster. While it can be on the same machines where the TPC-C client is executed, we strongly advise having separate machines for the PostgreSQL cluster.
4. Prepared file containing a list of TPC-C hosts (separated by new lines) to run the TPC-C client on. Note that if your machine has multiple cores, you can run multiple instances of TPC-C on the same machine. For example:

```
cat << EOF > tpcc.hosts
machine1.com
machine1.com
machine2.com
machine2.com
EOF
```

Until the end of this section we provide a detailed description of the prerequisites and how to install them manually.

Prerequisites to run helper scripts:
1. Install pssh.
2. Install libpq-dev (`sudo apt-get install libpq-dev`)
3. Install the psycopg2, numpy and requests Python packages using `pip3 install ydb psycopg2 numpy requests`.
4. To generate (if needed) and save your SSH keys:
```
../../common/copy_ssh_keys.sh --hosts tpcc.hosts
exec -l $SHELL
```
4. Copy the tpcc_config_template.xml file to tpcc_config.xml and edit url, user, password.

Prerequisites to run TPC-C client:
1. Install Java 21.
2. Install YDB's [fork](https://github.com/ydb-platform/tpcc) (`vanilla` branch) of benchbase into your home folder on each machine.
You have two options: build it on your own or use the prebuilt package. Here you can find prebuilt [benchbase-postgres.tgz](https://storage.yandexcloud.net/ydb-benchmark-builds/benchbase-postgres.tgz).


To install the package, execute the following:
```
benchhelpers/tpcc/ydb/upload_benchbase.sh --package benchbase-postgres.tgz --hosts tpcc.hosts
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

Note, that warmup and time are in seconds. By default the benchmark uses just 16 loader threads. The `--max-connections` flag controls the total number of connections to the database, i.e. if you run TPC-C on `n` machines, each machine will have `max-connections / n` connections to the database.
The `--java-memory` flag controls the amount of memory allocated to the Java process running the TPC-C client. Keep in mind, that if you start multiple instances of TPC-C on the same machine, you need to adjust the `--java-memory` flag accordingly.

If you have already executed the benchmark, you can use the `--run-phase-only` flag to reuse existing data and skip the loading phase. This will save you time on data generation. Also just to load the data and skip benchmark execution, use the `--no-run` flag.