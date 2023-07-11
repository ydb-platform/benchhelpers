# YugabyteDB

Инструкция по разворачиванию YugabyteDB на своих машинах.

## Requirements
+ Убедитесь, что из машины откуда вы запускаете, есть доступ по ssh ко всем другим машинам,
и при подключении у пользователя были права с sudo.
+ У всех машин должны быть синхронизированы часы. Можете следовать инструкциям [Synchronize clocks](https://www.digitalocean.com/community/tutorials/how-to-set-up-time-synchronization-on-ubuntu-20-04).
+ Проверьте [Prerequisites](https://docs.yugabyte.com/preview/deploy/manual-deployment/install-software/#prerequisites)
со страницы YugabyteDB.

## Getting Started

### Config
Настроим конфиг [cluster_config.py](cluster_config.py):
+ `Regions` - список ваших машин, на котором запускается YugabyteDB
+ `LOCAL_IP` - словарь, где ключ - машина, а значение - локальное IP 
+ `DEPLOY_PATH` - путь по которому распаковывается YugabyteDB
+ `DEPLOY_TMP_PATH` - путь по которому будут временные файлы
+ `LISTEN_PORT_MASTER`, `LISTEN_PORT_SERVER`, `PSQL_PORT`,
`CQL_PORT`, `REDIS_WEBSERVER_PORT`, `MASTER_WEBSERVER_PORT`, 
`SERVER_WEBSERVER_PORT`, `CQL_WEBSERVER_PORT`, `PSQL_WEBSERVER_PORT` -
про эти порты можно прочитать на странице [Default ports](https://docs.yugabyte.com/preview/reference/configuration/default-ports/) 
+ `Disks` - диски которые будут хранилищем базы данных
> Осторожно, при запуске скрипта форматируются диски `Disks`.
+ ~~`INIT_PER_DISK`~~ - YugabyteDB нельзя запускать на каждом диске (пока что)

### Start
Запуск осущетствляется в несколько этапов:
1. `Stop` - Остановка YugabyteDB, если он был запущен
2. `Clean` - Очистка дисков `Disks`
3. `Format` - Форматирование дисков `Disks` по пути `DEPLOY_PATH`/data/<disk_name>
4. `Deploy` - Распаковка пакета YugabyteDB
5. `Start` - Запуск YugabyteDB

```sh
cd <PATH_TO_SCRIPT>
./setup.sh --package <PATH_TO_YUGABYTE_PACKAGE> --config <PATH_TO_CONFIG>
```
+ `<PATH_TO_YUGABYTE_PACKAGE>` - путь до архива с YugabyteDB. Скачать можно по ссылке [Releases](https://docs.yugabyte.com/preview/releases/)
+ `<PATH_TO_CONFIG>` - путь до конфига вида [cluster_config.py](cluster_config.py)

### Stop
```sh
cd <PATH_TO_SCRIPT>
./control.py -c <PATH_TO_CONFIG> --stop
```
+ `<PATH_TO_CONFIG>` - путь до конфига вида [cluster_config.py](cluster_config.py)
