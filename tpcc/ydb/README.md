# How to run TPC-C on YDB

## Prerequisites

The TPC-C setup involves the following components:
1. Helper scripts located in this directory. Execute them on any random machine.
2. TPC-C clients, which can be executed on the same machine as the helper scripts. However, for a reasonable YDB cluster, it is recommended to have multiple machines running the TPC-C clients. Please, check hardware [requirements](https://github.com/ydb-platform/tpcc#hardware-requirements) for TPC-C clients.
3. A running YDB cluster. While it can be on the same machines where the TPC-C client is executed, we strongly advise having separate machines for the YDB cluster.
4. Prepared file containing a list of TPC-C hosts (separated by new lines) to run the TPC-C client on. Note that if your machine has multiple cores, you can run multiple instances of TPC-C on the same machine. For example:

```
cat << EOF > tpcc.hosts
machine1.com
machine1.com
machine2.com
machine2.com
EOF
```

For a regular installation to install all the dependencies and TPC-C, you can use the following command:
```
./setup_tpcc_nodes.sh --hosts tpcc.hosts
exec -l $SHELL
```

Until the end of this section we provide a detailed description of the prerequisites and how to install them manually.

Prerequisites to run helper scripts:
1. Install pssh.
2. Install the ydb, numpy and requests Python packages using `pip3 install ydb numpy requests`.
3. [Download](https://ydb.tech/en/docs/downloads/) the latest YDB CLI and place it somewhere in your PATH.
4. To generate (if needed) and save your SSH keys:
```
../../common/copy_ssh_keys.sh --hosts tpcc.hosts
exec -l $SHELL
```

Prerequisites to run TPC-C client:
1. Install Java 21. You can use `../../common/install_java21.sh --hosts tpcc.hosts`
2. Install YDB's [fork](https://github.com/ydb-platform/tpcc) of benchbase into your home folder on each machine.
You have two options: build it on your own or use the prebuilt package. Here you can find prebuilt [benchbase-postgres.tgz](https://storage.yandexcloud.net/ydb-benchmark-builds/benchbase-ydb.tgz).

To install the package, execute the following (note, that if you don't specify the package, the script will download the latest from the internet):
```
./upload_benchbase.sh --hosts tpcc.hosts [--package benchbase-ydb.tgz]
```

[Here](https://github.com/ydb-platform/ydb-jdbc-driver/#authentication-modes) you can find description of the authentication. Usually you will either use anonymous authentication in case of self deployed YDB, or provide a service account key file using the `saFile=file:` jdbc url parameter in [tpcc_config_template.xml](https://github.com/ydb-platform/benchhelpers/blob/108cb4ca3efc89dee7866b4bb8fca1a59ad265a8/tpcc/ydb/tpcc_config_template.xml#L7), when run managed YDB. Also, these scripts use `ydb` CLI and python script using `ydb` SDK. In case of managed YDB and service account key, you must export `YDB_SERVICE_ACCOUNT_KEY_FILE_CREDENTIALS` and `SA_KEY_FILE` before running the scripts.

## Running the benchmark

To run the benchmark, execute the following command:

```
mkdir -p $HOME/tpcc_logs
./run_ydb.sh                                        \
    --ydb-host ydb-001.com                          \
    --database /Root/testdb                         \
    --config tpcc_config_template.xml               \
    --hosts tpcc.hosts                              \
    --warehouses 1000                               \
    --warmup 1200                                   \
    --time 3600                                     \
    --java-memory <ACCORDING_HARDWARE_REQUIREMENTS> \
    --log-dir $HOME/tpcc_logs
```

Note, that warmup and time are in seconds. By default the benchmark uses just 16 loader threads, if your machines have enough cores (as well as YDB cluster), you can increase the number of threads using the `--loader-threads` flag. In our runs we usually use 128 threads per machine (1 thread per core) and 8 machines to run the benchmark (YDB cluster has 384 cores in total).

If you have already executed the benchmark, you can use the `--run-phase-only` flag to reuse existing data and skip the loading phase. This will save you time on data generation. Also just to load the data and skip the execution, use the `--no-run` flag. Usually it is convinient to load the data, check the monitoring metrics and then run the benchmark.