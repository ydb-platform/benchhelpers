# YDB

This guide explains how to deploy YDB on your machines.

The deployment algorithm in the scripts is almost identical to the instructions in [Deploying a YDB cluster on virtual or bare-metal servers](https://ydb.tech/en/docs/deploy/manual/deploy-ydb-on-premises) with authentication disabled.

## Requirements
+ Make sure you have SSH access to all other machines from the machine where you are running the script, and the user has sudo privileges.
+ All machines must have synchronized clocks. You can follow the instructions in [Synchronize clocks](https://www.digitalocean.com/community/tutorials/how-to-set-up-time-synchronization-on-ubuntu-20-04).
+ Check the [Prerequisites](https://ydb.tech/en/docs/deploy/manual/deploy-ydb-on-premises#requirements).


## Getting Started

### Configuration
Set up the [setup_config.sh](setup_config.sh) file:
+ `HOSTS` - a list of your machines where YDB will be deployed.
+ `Disks` - the disks that will store the database.
> Note: the `Disks` will be formatted when the script is run.
+ `CONFIG_DIR` - the path to the directory with the `config.yaml` and `config_dynnodes.yaml` files (details below).
+ `YDB_SETUP_PATH` - the path where YDB will be installed.
+ `GRPC_PORT_BEGIN` - the GRPC port for client-cluster interaction.
+ `IC_PORT_BEGIN` - the Interconnect port for intra-cluster node interaction.
+ `MON_PORT_BEGIN` - the port for HTTP interface of YDB Embedded UI.
> For each dynamic node, the next port in line is taken. Therefore, the network configuration
> must allow TCP connections on ports
> + `GRPC_PORT_BEGIN...GRPC_PORT_BEGIN+DYNNODE_COUNT`
> + `IC_PORT_BEGIN...IC_PORT_BEGIN+DYNNODE_COUNT`
> + `MON_PORT_BEGIN...MON_PORT_BEGIN+DYNNODE_COUNT`
+ `DYNNODE_COUNT` - the number of dynamic nodes for each machine.
+ `DYNNODE_TASKSET_CPU` - the distribution of cores among dynamic nodes.
+ `DATABASE_NAME` - the name of the database.
+ `STORAGE_POOLS` - the name of the storage pool and the number of storage groups allocated.
The pool name usually means the type of data storage devices and must match the
`storage_pool_types.kind` setting inside the `domains_config.domain` element of the configuration file.

To configure `config.yaml` for static node deployment,
see the [quick guide](https://ydb.tech/en/docs/deploy/manual/deploy-ydb-on-premises#config) or the [detailed guide](https://ydb.tech/en/docs/deploy/configuration/config).
`config_dynnodes.yaml` is configured in a similar way, but
is used for creating dynamic nodes.

The repository contains [config.yaml](./configs/config.yaml) and [config_dynnodes.yaml](./configs/config_dynnodes.yaml) files for `mirror-3dc-3nodes`.

### Start
The launch is performed in several stages:
1. `Stop` - Stop YDB if it is running.
2. `Clean and Format disks` - Format the `Disks` at the `DEPLOY_PATH`/data/<disk_name> path.
3. `Deploy` - Unpack the YDB package.
4. `Start static nodes` - Start the static nodes.
5. `Init BS` - Create the database.
6. `Start dynnodes` - Start the dynamic nodes.

```sh
cd <PATH_TO_SCRIPT>
./setup.sh --ydbd <PATH_TO_YDBD_PACKAGE> --config <PATH_TO_CONFIG>
```
+ `<PATH_TO_YDBD_PACKAGE>` - the path to the YDBD archive. You can download it from the [link](https://binaries.ydb.tech/ydbd-stable-linux-amd64.tar.gz) or execute the command:
```shell
wget https://binaries.ydb.tech/ydbd-stable-linux-amd64.tar.gz
```
+ `<PATH_TO_CONFIG>` - the path to the configuration file, for example [setup_config.sh](setup_config.sh).

### Stop
```sh
cd <PATH_TO_SCRIPT>
./setup.sh -c <PATH_TO_CONFIG> --stop
```
+ `<PATH_TO_CONFIG>` - the path to the configuration file, for example [setup_config.sh](setup_config.sh).
