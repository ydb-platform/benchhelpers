# TPC-C for CockroachDB

This folder contains the TPC-C launch script for CockroachDB.
The script is a generalization for the Small, Medium, and Large scales in 
[Performance Benchmarking with TPC-C](https://www.cockroachlabs.com/docs/v23.1/performance-benchmarking-with-tpcc-large),
except for the [Partition the database](https://www.cockroachlabs.com/docs/v23.1/performance-benchmarking-with-tpcc-large#step-5-partition-the-database) step.

It is assumed that you already know about the principles of [TPC-C](https://en.wikipedia.org/wiki/TPC-C).


## Requirements
+ Installed `parallel-ssh` on machine where scripts will be run. We worked with `parallel-ssh` on version 2.3.4.
+ Running CockroachDB. You can see the setup instructions [here](../db_installers/cockroach/README.md).

## Getting Started

### Configuration

First, you need to configure [tpcc_config.sh](configs/tpcc_config.sh).

+ `COCKROACH_HOSTS` - a list of hosts on which CockroachDB is running.
+ `COCKROACH_LISTEN_PORT` - the port for inter-node communication.
+ `PATH_TO_COCKROACH` - the path to CockroachDB on `COCKROACH_HOSTS`.
+ `COCKROACH_TAR` - the path to the CockroachDB archive on the machine where the script will be run.
+ `COCKROACH_DEPLOY_PATH` - the path to deploy CockroachDB on `TPCC_HOSTS`.
+ `TPCC_HOSTS` - a list of hosts on which TPC-C will be run.
+ `WAREHOUSES` - the number of TPC-C warehouses.
+ `RAMP` - the duration over which to ramp up load.
+ `DURATION` - the duration to run, with a required time unit suffix.

If you want to speed up the process of importing a large dataset, you can 
configure [cluster_config.sh](configs/cluster_config.sh). Initially, the variables 
in the file are assigned default values. For more information, you can refer to 
[this link](https://www.cockroachlabs.com/docs/v23.1/cluster-settings). You can
also see how they were configured during the testing of CockroachDB
[here](https://www.cockroachlabs.com/docs/v23.1/performance-benchmarking-with-tpcc-large#step-3-configure-the-cluster).

### Start

```shell
cd <PATH_TO_SCRIPT>
./tpcc.sh --config configs/tpcc_config.sh [--cluster-config configs/cluster_config.sh | --without-import]
```
`--cluster-config` and `--without-import` are optional.
