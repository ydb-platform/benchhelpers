# YDB

Инструкция по разворачиванию YDB на своих машинах.

Алгоритм развертывания в скриптах практически идентичен инструкции [Deploying a YDB cluster on virtual or bare-metal servers
](https://ydb.tech/en/docs/deploy/manual/deploy-ydb-on-premises) при выключенной аутентификации. 

## Requirements
+ Убедитесь, что из машины откуда вы запускаете, есть доступ по ssh ко всем другим машинам,
и при подключении у пользователя были права с sudo.
+ У всех машин должны быть синхронизированы часы. Можете следовать инструкциям [Synchronize clocks](https://www.digitalocean.com/community/tutorials/how-to-set-up-time-synchronization-on-ubuntu-20-04).
+ Проверьте [Prerequisites](https://ydb.tech/en/docs/deploy/manual/deploy-ydb-on-premises#requirements).

## Getting Started

### Config
Настроим конфиг [setup_config.sh](setup_config.sh):
+ `Regions` - список ваших машин, на котором запускается YDB
+ `DEPLOY_PATH` - путь по которому распаковывается YDB
+ `DEPLOY_TMP_PATH` - путь по которому будут временные файлы
+ `Disks` - диски которые будут хранилищем базы данных
> Осторожно, при запуске скрипта форматируются диски `Disks`.
+ `CONFIG_DIR` - путь до директории с конфигами `config.yaml` и `config_dynnodes.yaml` (о них чуть ниже)
+ `YDB_SETUP_PATH` - путь по которому будет устанавливаться YDB
+ `GRPC_PORT_BEGIN` - порт по которому GRPC для клиент-кластерного взаимодействия
+ `IC_PORT_BEGIN` - порт по которому Interconnect для внутрикластерного взаимодействия узлов
+ `MON_PORT_BEGIN` - порт по которому HTTP интерфейс YDB Embedded UI
> Для каждого динамического узла берется следующий по очереди порт. Поэтому сетевая конфигурация
> должна разрешать TCP соединения по портам
> + `GRPC_PORT_BEGIN...GRPC_PORT_BEGIN+DYNNODE_COUNT`
> + `IC_PORT_BEGIN...IC_PORT_BEGIN+DYNNODE_COUNT`
> + `MON_PORT_BEGIN...MON_PORT_BEGIN+DYNNODE_COUNT`
+ `DYNNODE_COUNT` - количество динамических узлов для каждой машины
+ `DYNNODE_TASKSET_CPU` - разделение ядер между динамическими узлами
+ `DATABASE_NAME` - название базы данных
+ `STORAGE_POOLS` - имя пула хранения и количество выделяемых групп хранения. 
Имя пула обычно означает тип устройств хранения данных и должно соответствовать
настройке `storage_pool_types.kind` внутри элемента `domains_config.domain` файла
конфигурации.

Чтобы настроить конфиг `config.yaml` для запуска статических узлов,
ознакомьтесь с [краткой инструкцией](https://ydb.tech/en/docs/deploy/manual/deploy-ydb-on-premises#config),
либо с [более детальной](https://ydb.tech/en/docs/deploy/configuration/config).
Конфиг `config_dynnodes.yaml` настраивается также, но
используется при создании динамических узлов.

В репозитории конфиги [config.yaml](./configs/config.yaml), [config_dynnodes.yaml](./configs/config_dynnodes.yaml) для `mirror-3dc-3nodes`.

### Start
Запуск осуществляется в несколько этапов:
1. `Stop` - Остановка YDB, если он был запущен
2. `Clean and Format disks` - Форматирование дисков `Disks` по пути `DEPLOY_PATH`/data/<disk_name>
3. `Deploy` - Распаковка пакета YDB
4. `Start static nodes` - Запуск статических узлов
5. `Init BS` - Создание базы данных
6. `Start dynnodes` - Запуск динамических узлов

```sh
cd <PATH_TO_SCRIPT>
./setup.sh --ydbd <PATH_TO_YDBD_PACKAGE> --config <PATH_TO_CONFIG>
```
+ `<PATH_TO_YDBD_PACKAGE>` - путь до архива с YDBD. Скачать можно по [ссылке](https://binaries.ydb.tech/ydbd-stable-linux-amd64.tar.gz) или выполнив команду:
```shell
wget https://binaries.ydb.tech/ydbd-stable-linux-amd64.tar.gz
```
+ `<PATH_TO_CONFIG>` - путь до конфига вида [setup_config.sh](setup_config.sh)

### Stop
```sh
cd <PATH_TO_SCRIPT>
./setup.sh -c <PATH_TO_CONFIG> --stop
```
+ `<PATH_TO_CONFIG>` - путь до конфига вида [setup_config.sh](setup_config.sh)
