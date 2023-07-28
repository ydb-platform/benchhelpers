# TPC-C for CockroachDB

This folder contains the TPC-C launch script for CockroachDB.
The script is a generalization for the Small, Medium, and Large scales in 
[Performance Benchmarking with TPC-C](https://www.cockroachlabs.com/docs/v23.1/performance-benchmarking-with-tpcc-large),
except for the [Partition the database](https://www.cockroachlabs.com/docs/v23.1/performance-benchmarking-with-tpcc-large#step-5-partition-the-database) step.

It is assumed that you already know about the principles of [TPC-C](https://en.wikipedia.org/wiki/TPC-C).


## Requirements
+ Installed `parallel-ssh` on machine where scripts will be run. We worked with `parallel-ssh` on version 2.3.4.
+ Running CockroachDB. You can see the setup instructions [here](../../db_installers/cockroach/README.md).

## Getting Started

### Configuration

First, you need to configure [tpcc_config.sh](configs/tpcc_config.cfg).

+ `COCKROACH_HOSTS` - a list of hosts on which CockroachDB is running. It is recommended to 
specify a HAProxy host for load balancing instead of CockroachDB hosts.
+ `COCKROACH_LISTEN_PORT` - the port for inter-node communication.
+ `PATH_TO_COCKROACH` - the path to CockroachDB on `COCKROACH_HOSTS`.
+ `COCKROACH_TAR` - the path to the CockroachDB archive on the machine where the script will be run.
+ `COCKROACH_DEPLOY_PATH` - the path to deploy CockroachDB on `TPCC_HOSTS`.
+ `WORKLOAD_PATH` - the path to directory with binary file `workload`on the machine where the script will be run.
You can view instructions for building from source 
[here](https://wiki.crdb.io/wiki/spaces/CRDB/pages/181338446/Getting+and+building+CockroachDB+from+source)(according to CockroachDB, the page is currently timing out) 
so you can run `./dev build workload`. 
+ `TPCC_HOSTS` - a list of hosts on which TPC-C will be run.
+ `WAREHOUSES` - the number of TPC-C warehouses for each instance.
+ `RAMP` - the duration over which to ramp up load.
+ `DURATION` - the duration to run, with a required time unit suffix.

If you want to speed up the process of importing a large dataset, you can 
configure [cluster_config.sh](configs/cluster_config.cfg). Initially, the variables 
in the file are assigned default values. For more information, you can refer to 
[this link](https://www.cockroachlabs.com/docs/v23.1/cluster-settings). You can
also see how they were configured during the testing of CockroachDB
[here](https://www.cockroachlabs.com/docs/v23.1/performance-benchmarking-with-tpcc-large#step-3-configure-the-cluster).

### Start
The launch is performed in several stages:
1. Deploy
2. Configure TPC-C importing
3. Import the TPC-C dataset (and Drop TPC-C tables)
4. Run TPC-C
5. Collect results


```shell
cd <PATH_TO_SCRIPT>
./tpcc.sh --config configs/tpcc_config.cfg [--cluster-config configs/cluster_config.cfg | ... ]
```
All parameters except `--config` are optional.
By default, the script executes all the stages, but by adding a parameter, you can control which stages are executed.
+ `--cluster-config` - without this parameter, there will be no stage 2.
+ `--without-import` - with this parameter, there will be no stage 3.
+ `--only-import` - with this parameter, there will be no stage 4 and 5.
+ `--collect-result` - with this parameter, there will be only stage 5.

#### Example to run:

If you want to run on a different numbers of warehouses, then you can first
load the data with the largest number of warehouses, then run TPC-C in turn. 
* Import the TPC-C dataset with 20k warehouses
```shell
cd <PATH_TO_SCRIPT>
./tpcc.sh --config configs/tpcc_config_20k.cfg --only-import
```
* Run TPC-C with 4k warehouses
```shell
./tpcc.sh --config configs/tpcc_config_4k.cfg --without-import
```
* Run TPC-C with 10k warehouses
```shell
./tpcc.sh --config configs/tpcc_config_10k.cfg --without-import
```
* Run TPC-C with 20k warehouses
```shell
./tpcc.sh --config configs/tpcc_config_20k.cfg --without-import
```
