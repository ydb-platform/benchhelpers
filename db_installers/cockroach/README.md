# Cockroach

Инструкция по разворачиванию CockroachDB на своих машинах.

На странице [Deploy CockroachDB On-Premises](https://www.cockroachlabs.com/docs/v23.1/deploy-cockroachdb-on-premises-insecure#step-5-set-up-load-balancing)
практически описан алгоритм работы скриптов, кроме шага [Synchronize clocks](https://www.cockroachlabs.com/docs/v23.1/deploy-cockroachdb-on-premises-insecure#step-1-synchronize-clocks).

## Requirements
+ Убедитесь, что из машины откуда вы запускаете, есть доступ по ssh ко всем другим машинам,
и при подключении у пользователя были права с sudo.
+ У всех машин должны быть синхронизированы часы. Можете следовать инструкциям [Synchronize clocks](https://www.cockroachlabs.com/docs/v23.1/deploy-cockroachdb-on-premises-insecure#step-1-synchronize-clocks).
+ Установлен `python3.x`

## Getting Started

### Config
Настроим конфиг [cluster_config.py](cluster_config.py):
+ `Regions` - список ваших машин, на котором запускается CockroachDB
+ `DEPLOY_PATH` - путь по которому распаковывается CockroachDB
+ `DEPLOY_TMP_PATH` - путь по которому будут временные файлы
+ `HA_PROXY_NODES` - машины на котором будет запускаться [Распределитель нагрузки HAProxy](https://www.haproxy.com/)
+ `HA_PROXY_SETUP_PATH` - путь по которому бинарник HAProxy. При наличии `haproxy` в PATH на `HA_PROXY_NODES`, можно оставить пустым.
+ `LISTEN_PORT` - порт, которые следует указать другим нодам для использования. Ваша сетевая конфигурация должна разрешать TCP-связь по этому порту
+ `HTTP_PORT` - порт по которому будет DB Console. Ваша сетевая конфигурация должна разрешать TCP-связь по этому порту
+ `Disks` - диски которые будут хранилищем базы данных
> Осторожно, при запуске скрипта форматируются диски `Disks`.
+ `Cores` - выделяемое количество ядер для CockroachDB
+ `CacheSizeGB` - выделяемое размер кеша для CockroachDB
+ `SqlMemorySizeGB` - выделяемое размер памяти для SQl CockroachDB
+ `INIT_PER_DISK` - на [странице с документацией](https://www.cockroachlabs.com/docs/v23.1/deploy-cockroachdb-on-premises-insecure#before-you-begin:~:text=Run%20each%20node,a%20Node.) 
CockroachDB явно просят этого не делать, но если вы не беспокоитесь о том, что потеряете данные при сбое машины, и хотите, чтобы
CockroachDB показал лучшую производительность, то можете присвоить 1. При этом `LISTEN_PORT` и `HTTP_PORT` будут инкрементироваться
для каждого диска, а значит по каждому такому порт должна разрешаться TCP-связь. Также `Cores`, `CacheSizeGB` и `SqlMemorySizeGB` 
будут разделены между каждой нодой на каждой машине.

### Start
Запуск осущетствляется в несколько этапов:
1. `Stop` - Остановка CockroachDB, если он был запущен
2. `Clean` - Очистка дисков `Disks`
3. `Format` - Форматирование дисков `Disks` по пути `DEPLOY_PATH`/data/<disk_name>
4. `Deploy` - Распаковка пакета CockroachDB
5. `Start CockroachDB` - Запуск CockroachDB
6. `Start HAProxy` - Запуск HAProxy

```sh
cd <PATH_TO_SCRIPT>
./setup.sh --package <PATH_TO_COCKROACH_PACKAGE> --config <PATH_TO_CONFIG> --ha-bin <PATH_TO_HAPROXY_BIN>
```
+ `<PATH_TO_COCKROACH_PACKAGE>` - путь до архива с CockroachDB. Скачать можно по ссылке `https://binaries.cockroachdb.com/cockroach-<VERSION>.linux-<ARCHITECTURE>.tgz`, 
где \
`<ARCHITECTURE>` - `amd64` для Intel, `arm64` для ARM;\
`<VERSION>` - версия CockroachDB.
+ `<PATH_TO_CONFIG>` - путь до конфига вида [cluster_config.py](cluster_config.py)
+ `<PATH_TO_HAPROXY_BIN>` - путь до бинарного файла HAProxy. Тесты на производительность
мы проводили с версией `2.4.19`, поэтому просим использовать ее или версию старше.

### Stop
```sh
cd <PATH_TO_SCRIPT>
./control.py -c <PATH_TO_CONFIG> --stop
```
+ `<PATH_TO_CONFIG>` - путь до конфига вида [cluster_config.py](cluster_config.py)
