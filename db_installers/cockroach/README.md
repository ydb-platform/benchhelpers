# Cockroach

This guide explains how to deploy CockroachDB on your machines.

> Note: This is not a production deployment method. There are scripts that will allow you to run CockroachDB and test as easily as possible.

## Requirements
+ Make sure you have SSH access to all other machines from the machine where you are running the script, and the user has sudo privileges.
+ All machines must have synchronized clocks. You can follow the instructions in [Synchronize clocks](https://www.cockroachlabs.com/docs/v23.1/deploy-cockroachdb-on-premises-insecure#step-1-synchronize-clocks).
+ `Python 3.x` should be installed.

## Getting Started

### Configuration
Set up the [cluster_config.py](cluster_config.py) file:
+ `Regions` - a list of your machines where CockroachDB will be deployed.
+ `DEPLOY_PATH` - the path where CockroachDB will be unpacked.
+ `DEPLOY_TMP_PATH` - the path for temporary files.
+ `HA_PROXY_HOSTS` - the machines where [HAProxy load balancer](https://www.haproxy.com/) will be deployed.
+ `HA_PROXY_SETUP_PATH` - the path to the HAProxy binary. If HAProxy is already in the `PATH` on `HA_PROXY_HOSTS`, this can be left empty.
+ `LISTEN_PORT` - the port to be used by other nodes for communication. Your network configuration should allow TCP communication on this port.
+ `HTTP_PORT` - the port for the DB Console. Your network configuration should allow TCP communication on this port.
+ `Disks` - the disks that will store the database.
> Note: the `Disks` will be formatted when the script is run.
+ `Cores` - the number of cores allocated for CockroachDB.
+ `CacheSizeGB` - the size of cache allocated for CockroachDB.
+ `SqlMemorySizeGB` - the size of memory allocated for SQL CockroachDB.
+ `INIT_PER_DISK` - CockroachDB explicitly [asks not to do this](https://www.cockroachlabs.com/docs/v23.1/deploy-cockroachdb-on-premises-insecure#before-you-begin:~:text=Run%20each%20node,a%20Node.), but if you are not concerned about data
loss in the event of a machine failure and want CockroachDB to show better performance, you can assign it
a value of 1. In this case, `LISTEN_PORT` and `HTTP_PORT` will be incremented for each disk, so TCP communication
should be allowed on each of these ports. Additionally, `Cores`, `CacheSizeGB`, and `SqlMemorySizeGB` will be divided
among each node on each machine.

### Start
The launch is performed in several stages:
1. `Stop` - Stop CockroachDB if it is running.
2. `Format` - Format the `Disks` at the `DEPLOY_PATH`/data/<disk_name> path.
3. `Deploy` - Unpack the CockroachDB package.
4. `Start CockroachDB` - Start CockroachDB.
5. `Start HAProxy` - Start HAProxy.

```sh
cd <PATH_TO_SCRIPT>
./setup.sh --package <PATH_TO_COCKROACH_PACKAGE> --config <PATH_TO_CONFIG> --ha-bin <PATH_TO_HAPROXY_BIN>
```
+ `<PATH_TO_COCKROACH_PACKAGE>` - the path to the CockroachDB archive. You can download it from the link `https://binaries.cockroachdb.com/cockroach-<VERSION>.linux-<ARCHITECTURE>.tgz`, where
    - `<ARCHITECTURE>` - amd64 for Intel, arm64 for ARM;
    - `<VERSION>` - the version of CockroachDB.
+ `<PATH_TO_CONFIG>` - the path to the configuration file, for example [cluster_config.py](cluster_config.py).
+ `<PATH_TO_HAPROXY_BIN>` - the path to the HAProxy binary. We conducted performance tests with version 2.4.19, so please use that version or a newer one.

For check access to the built-in web interface, open in the browser the `http://<COCKROACH_HOST>:HTTP_PORT` URL, 
where `<COCKROACH_HOST>` is the FQDN of the server running any CockroachDB node.


### Stop
```sh
cd <PATH_TO_SCRIPT>
./control.py -c <PATH_TO_CONFIG> --stop
```
+ `<PATH_TO_CONFIG>` - the path to the configuration file, for example [cluster_config.py](cluster_config.py).
