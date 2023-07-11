# benchhelpers

In this repository, you will find scripts for deploying, running, and evaluating the performance of databases [YDB](https://ydb.tech/), [CockroachDB](https://www.cockroachlabs.com/), and [YugabyteDB](https://www.yugabyte.com/).

These scripts were used in writing the article [YCSB performance series](https://blog.ydb.tech/ycsb-performance-series-ydb-cockroachdb-and-yugabytedb-f25c077a382b).

## Requirements
+ `Java13+`
+ Requirements for the selected database: [YDB](./db_installers/ydb/README.md#requirements),
[CockroachDB](./db_installers/cockroach/README.md#requirements), 
[YugabyteDB](./db_installers/yugabyte/README.md#requirements)


## Getting Started

First, you need to deploy the databases on the machines. For more details on each database:
+ [YDB](./db_installers/ydb/README.md)
+ [CockroachDB](./db_installers/cockroach/README.md)
+ [YugabyteDB](./db_installers/yugabyte/README.md)

Now you can start running the benchmark on the selected database. For best performance,
it is recommended to run only one database on each machine.

Below is the instruction for running YCSB for [YDB](#ydb), [CockroachDB](#cockroachdb), [YugabyteDB](#yugabytedb).

You can read about YCSB workloads [here](https://github.com/brianfrankcooper/YCSB/wiki/Core-Workloads).

### YDB

---

First, you need to configure the config file according to your needs. If you look into the config file [ydb.rc](./ycsb/configs/ydb.rc), you can find:
+ `TARGET` - one of the machines where YDB is running.
+ `TEST_DB` - the database on which the performance tests will be conducted.
+ `STATIC_NODE_GRPC_PORT` - GRPC port of the static node.
+ `YCSB_NODES` - list of nodes on which YCSB will be run.
+ `YCSB_NODES_COUNT` - if you want to limit the number of `YCSB_NODES` without changing the list.
+ `YCSB_TAR_PATH` - path to the YCSB package on the machine where the script will run.
+ `YCSB_DEPLOY_PATH` - path where the package and auxiliary files should be deployed on `YCSB_NODES`.

After configuring `ydb.rc` and `workload.rc` (more about it [below](#workload)), you can start YCSB:
```sh
cd <PATH_TO_BENCHHELPERS>/ycsb
./run_workloads.sh --log-dir <PATH_TO_LOG_DIR> configs/workload.rc configs/ydb.rc
```
There are also parameters for `run_workloads.sh`:
+ `--name` - for convenience, the log file names will be prefixed with `name`.
+ `--threads` - number of threads for YCSB (default - 64).
+ `--de-threads` - number of threads for YCSB for workload D and E (default - 512).
+ `--ycsb-nodes` - same as `YCSB_NODES_COUNT`, but with higher priority.


### CockroachDB

---

Just like with YDB, let's configure the config file [cockroach.rc](./ycsb/configs/cockroach.rc):

+ `TARGET` - one of the clusters where CockroachDB is running.
+ `YCSB_NODES` - list of nodes on which YCSB will be run.
+ `YCSB_NODES_COUNT` - if you want to limit the number of `YCSB_NODES` without changing the list .
+ `COCKROACH_PATH` - path to the folder with CockroachDB on `YCSB_NODES`.
+ `COCKROACH_TAR_PATH` - if `COCKROACH_PATH` is not present, the archive at this path will be unpacked in `COCKROACH_DEPLOY_PATH`.
+ `HA_PROXY_NODE` - one of the nodes where haproxy is running.
+ `COCKROACH_INIT_SLEEP_TIME_MINUTES` - sometimes export fails with CLI error, but continues in cockroach, so we continue to wait.

After configuring `cockroach.rc` and `workload.rc` (more about it [below](#workload)), you can start YCSB:
```sh
cd <PATH_TO_BENCHHELPERS>/ycsb
./run_workloads.sh --type cockroach --log-dir <PATH_TO_LOG_DIR> configs/workload.rc configs/cockroach.rc
```
You can read about additional parameters for `run_workloads.sh` in [YDB](#ydb).

### YugabyteDB

---

Let's configure the config file [yugabyte.rc](./ycsb/configs/yugabyte.rc):

+ `TARGET` - one of the clusters where YugabyteDB is running.
+ `YCSB_NODES` - list of nodes on which YCSB will be run.
+ `YCSB_NODES_COUNT` - if you want to limit the number of `YCSB_NODES` without changing the list .
+ `YU_YCSB_PATH` - path to the folder with YSCB of YugabyteDB on `YCSB_NODES`.
+ `YU_YCSB_TAR_PATH` - if `YU_YCSB_PATH` is not present, the archive at this path will be unpacked in `YU_YCSB_DEPLOY_PATH`.
+ `YU_PATH` - path to the folder with YugabyteDB on `TARGET`.

After configuring yugabyte.rc and workload.rc (more about it [below](#workload)), you can start YCSB:
```sh
cd <PATH_TO_BENCHHELPERS>/ycsb
./run_workloads.sh --type yugabyte --log-dir <PATH_TO_LOG_DIR> configs/workload.rc configs/yugabyte.rc
```
You can read about additional parameters for run_workloads.sh in [YDB](#ydb).

### Workload

---

The [workload.rc](./ycsb/configs/workload.rc) file contains the configuration for the YCSB workload. The following variables can be found in this file:
- `WORKLOADS` - a list of workloads to be executed.
- `RECORD_COUNT` - the number of records in the database at the start of the workload.
- `OP_COUNT_TOTAL` - the number of operations to be performed.
- `DISTRIBUTIONS` - what distribution should be used to select the records to operate on – uniform, zipfian, hotspot, sequential, exponential or latest
- `YCSB_THREADS` - the number of YCSB client threads.
- `YCSB_THREADS_DE` - the number of YCSB client threads for workload D and E.
- `LOAD_YCSB_THREADS` - the number of YCSB client threads when load the data.
- `KEY_ORDER` - should records be inserted in order by key (“ordered”), or in hashed order (“hashed”).
- `MAX_PARTS`, `MAX_PART_SIZE_MB`, `LOAD_DATA` - these are settings for developers and the default values are suitable in most cases.
