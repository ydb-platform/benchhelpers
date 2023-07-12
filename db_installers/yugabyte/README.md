# YugabyteDB

This guide explains how to deploy YugabyteDB on your machines.

## Requirements
+ Make sure you have SSH access to all other machines from the machine where you are running the script, and the user has sudo privileges.
+ All machines must have synchronized clocks. You can follow the instructions in [Synchronize clocks](https://www.digitalocean.com/community/tutorials/how-to-set-up-time-synchronization-on-ubuntu-20-04).
+ Check the [Prerequisites](https://docs.yugabyte.com/preview/deploy/manual-deployment/install-software/#prerequisites) on the YugabyteDB page.

## Getting Started

### Configuration
Set up the [cluster_config.py](cluster_config.py) file:
+ `Regions` - a list of your machines where YugabyteDB will be deployed.
+ `LOCAL_IP` - a dictionary where the key is the external IP of machine and the value is the local IP.
+ `DEPLOY_PATH` - the path where YugabyteDB will be unpacked.
+ `DEPLOY_TMP_PATH` - the path for temporary files.
+ `LISTEN_PORT_MASTER`, `LISTEN_PORT_SERVER`, `PSQL_PORT`,
`CQL_PORT`, `REDIS_WEBSERVER_PORT`, `MASTER_WEBSERVER_PORT`, 
`SERVER_WEBSERVER_PORT`, `CQL_WEBSERVER_PORT`, `PSQL_WEBSERVER_PORT` -
you can read about these ports on the [Default ports](https://docs.yugabyte.com/preview/reference/configuration/default-ports/) page.
+ `Disks` - the disks that will store the database.
> Note: the `Disks` will be formatted when the script is run.
+ ~~`INIT_PER_DISK`~~ - YugabyteDB cannot be run on each disk (for now).

### Start
The launch is performed in several stages:
1. `Stop` - Stop YugabyteDB if it is running.
2. `Format` - Format the `Disks` at the `DEPLOY_PATH`/data/<disk_name> path.
3. `Deploy` - Unpack the YugabyteDB package.
4. `Start` - Start YugabyteDB.

```sh
cd <PATH_TO_SCRIPT>
./setup.sh --package <PATH_TO_YUGABYTE_PACKAGE> --config <PATH_TO_CONFIG>
```
+ `<PATH_TO_YUGABYTE_PACKAGE>` - the path to the YugabyteDB archive. You can download it from the [Releases](https://docs.yugabyte.com/preview/releases/).
+ `<PATH_TO_CONFIG>` - the path to the configuration file, for example [cluster_config.py](cluster_config.py).

For check access to the built-in web interface, open in the browser the `http://<YUGABYTE_HOST>:<MASTER_WEBSERVER_PORT or SERVER_WEBSERVER_PORT>` URL, 
where `<YUGABYTE_HOST>` is the FQDN of the server running any YugabyteDB node.

### Stop
```sh
cd <PATH_TO_SCRIPT>
./control.py -c <PATH_TO_CONFIG> --stop
```
+ <PATH_TO_CONFIG> - the path to the configuration file, for example [cluster_config.py](cluster_config.py).
