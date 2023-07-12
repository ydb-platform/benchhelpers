# benchhelpers

In this repository you will find scripts that will help with the deployment, launch and performance evaluation of databases
[YDB](https://ydb.tech/), [CockroachDB](https://www.cockroachlabs.com/), and [YugabyteDB](https://www.yugabyte.com/).

These scripts were used the article [YCSB performance series](https://blog.ydb.tech/ycsb-performance-series-ydb-cockroachdb-and-yugabytedb-f25c077a382b).

### Premise:
YCSB has a problem with the fact that it does not create load efficiently enough. 
That is, in order to load machines with databases, one YCSB shooting machine is often not enough. 

### Decision:
With the help of our scripts, you can greatly facilitate the launch and collection of YCSB benchmark results.

## Requirements
+ Installed `Java13+` on machines where YCSB will be run.
+ Installed `parallel-ssh` on machine where scripts will be run. We worked with `parallel-ssh` on version `2.3.4`.
+ Requirements for the selected database: [YDB](./db_installers/ydb/README.md#requirements),
[CockroachDB](./db_installers/cockroach/README.md#requirements), 
[YugabyteDB](./db_installers/yugabyte/README.md#requirements)


## Getting Started

First, you need to deploy one of the databases on the machines. For more details on each database:
+ [YDB](./db_installers/ydb/README.md)
+ [CockroachDB](./db_installers/cockroach/README.md)
+ [YugabyteDB](./db_installers/yugabyte/README.md)

Now you can start running the benchmark on the selected database. For best performance,
it is recommended to run only one database on each server.

Below is the instruction for running YCSB for [YDB](#ydb), [CockroachDB](#cockroachdb), [YugabyteDB](#yugabytedb),
but first you need to familiarize yourself with [workload YCSB](#workload).

### Workload

---

You can read about YCSB workloads [here](https://github.com/brianfrankcooper/YCSB/wiki/Core-Workloads).

We should have two config files to run YCSB: `<DB_NAME>.rc` and `workload.rc`.

The [workload.rc](./ycsb/configs/workload.rc) file contains the configuration for the YCSB workload. The following variables can be found in this file:
- `WORKLOADS` - a list of workloads to be executed.
- `RECORD_COUNT` - the number of records in the database at the start of the workload.
- `OP_COUNT_TOTAL` - the number of operations to be performed.
- `DISTRIBUTIONS` - what distribution should be used to select the records to operate on – uniform, zipfian, hotspot, sequential, exponential or latest
- `YCSB_THREADS` - the number of YCSB client threads (default - 64).
- `YCSB_THREADS_DE` - the number of YCSB client threads for workload D and E (default - 512).
- `LOAD_YCSB_THREADS` - the number of YCSB client threads when load the data.
- `KEY_ORDER` - should records be inserted in order by key (“ordered”), or in hashed order (“hashed”).
- `MAX_PARTS`, `MAX_PART_SIZE_MB`, `LOAD_DATA` - these are settings for developers and the default values are suitable in most cases.


### YDB

---

Next, you need to configure the YDB config file according to your needs.

Suppose that on `ydb_host1.com` YDB is running.
We want to shoot at her YCSB from machines `ycsb-host1.com`, `ycsb-host2.com`, etc.

If you look into the config file [ydb.rc](./ycsb/configs/ydb.rc), you can find:
+ `TARGET` - one of the hosts where YDB is running (that is, `ydb_host1.com`).
+ `DATABASE_NAME` - the database on which the performance tests will be conducted.
+ `STATIC_NODE_GRPC_PORT` - GRPC port of the static node.
+ `YCSB_HOSTS` - list of hosts on which YCSB will be run (this is, `ycsb-host1.com`, `ycsb-host2.com`, etc).
+ `YCSB_HOSTS_COUNT` - if you want to limit the number of `YCSB_HOSTS` without changing the list.
+ `YCSB_PATH` - path to the folder with YCSB on `YCSB_HOSTS`.
+ `YCSB_TAR_PATH` - if `YCSB_PATH` is not present, the archive at this path will be unpacked in `YCSB_DEPLOY_PATH`.

After configuring `ydb.rc` and `workload.rc` (more about it [above](#workload)), you can start YCSB:
```sh
cd <PATH_TO_BENCHHELPERS>/ycsb
./run_workloads.sh --log-dir <PATH_TO_LOG_DIR> configs/workload.rc configs/ydb.rc
```
There are also parameters for `run_workloads.sh`:
+ `--name` - for convenience, the log file names will be suffixed with `<NAME>`.
+ `--threads` - same as `YCSB_THREADS` from [workload](#workload), but with higher priority (overwrites the value from the config).
+ `--de-threads` - same as `YCSB_THREADS_DE` from [workload](#workload), but with higher priority (overwrites the value from the config).
+ `--ycsb-hosts` - same as `YCSB_HOSTS_COUNT`, but with higher priority (overwrites the value from the config).

### CockroachDB

---

Just like with YDB, let's configure the config file [cockroach.rc](./ycsb/configs/cockroach.rc):

+ `TARGET` - one of the hosts where CockroachDB is running.
+ `YCSB_HOSTS` - list of hosts on which YCSB will be run.
+ `YCSB_HOSTS_COUNT` - if you want to limit the number of `YCSB_HOSTS` without changing the list .
+ `COCKROACH_PATH` - path to the folder with CockroachDB on `YCSB_HOSTS`.
+ `COCKROACH_TAR_PATH` - if `COCKROACH_PATH` is not present, the archive at this path will be unpacked in `COCKROACH_DEPLOY_PATH`.
+ `HA_PROXY_HOST` - one of the hosts where haproxy is running.
+ `COCKROACH_INIT_SLEEP_TIME_MINUTES` - sometimes export fails with CLI error, but continues in cockroach, so we continue to wait.

After configuring `cockroach.rc` and `workload.rc` (more about it [above](#workload)), you can start YCSB:
```sh
cd <PATH_TO_BENCHHELPERS>/ycsb
./run_workloads.sh --type cockroach --log-dir <PATH_TO_LOG_DIR> configs/workload.rc configs/cockroach.rc
```
You can read about additional parameters for `run_workloads.sh` in [YDB](#ydb).

### YugabyteDB

---

Let's configure the config file [yugabyte.rc](./ycsb/configs/yugabyte.rc):

+ `TARGET` - one of the hosts where YugabyteDB is running.
+ `YCSB_HOSTS` - list of hosts on which YCSB will be run.
+ `YCSB_HOSTS_COUNT` - if you want to limit the number of `YCSB_HOSTS` without changing the list .
+ `YU_YCSB_PATH` - path to the folder with YSCB of YugabyteDB on `YCSB_HOSTS`.
+ `YU_YCSB_TAR_PATH` - if `YU_YCSB_PATH` is not present, the archive at this path will be unpacked in `YU_YCSB_DEPLOY_PATH`.
+ `YU_PATH` - path to the folder with YugabyteDB on `TARGET`.

After configuring `yugabyte.rc` and `workload.rc` (more about it [above](#workload)), you can start YCSB on [YCQL](https://docs.yugabyte.com/preview/explore/ycql-language/):
```sh
cd <PATH_TO_BENCHHELPERS>/ycsb
./run_workloads.sh --type yugabyte --log-dir <PATH_TO_LOG_DIR> configs/workload.rc configs/yugabyte.rc
```
You can read about additional parameters for `run_workloads.sh` in [YDB](#ydb).

If you want to perform a benchmark on [YSQL](https://docs.yugabyte.com/preview/explore/ysql-language-features/),
then change the `--type` parameter to `yugabyteSQL`.
