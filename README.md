# benchhelpers

В этом репозитории вы найдете скрипты для развертывания, запуска и дальнейшей
оценки производительности баз данных [YDB](https://ydb.tech/), [CockroachDB](https://www.cockroachlabs.com/) и [YugabyteDB](https://www.yugabyte.com/).

Эти скрипты использовались при написании статьи [YCSB performance series](https://blog.ydb.tech/ycsb-performance-series-ydb-cockroachdb-and-yugabytedb-f25c077a382b).

## Instruction

Для начала нужно развернуть базы данных на машинах. Подробнее для каждой базы данных:
+ [YDB](./db_installers/ydb/README.md)
+ [CockroachDB](./db_installers/cockroach/README.md)
+ [YugabyteDB](./db_installers/yugabyte/README.md)

Теперь можно начать запускать бенчмарк на выбранной базе данных. Для лучшей работы
рекомендуется запускать на каждой машине только одну базу данных.

Ниже идет инструкция по запуску YCSB для [YDB](#ydb), [CockroachDB](#cockroachdb), [YugabyteDB](#yugabytedb).

Про рабочие нагрузки (workload) YCSB можно прочитать [здесь](https://github.com/brianfrankcooper/YCSB/wiki/Core-Workloads).

### YDB

---

Для начала нужно настроить правильный для себя конфиг. Если заглянуть в конфиг [ydb.rc](./ycsb/configs/ydb.rc), то
можно найти:
+ `TARGET` - один из кластеров на котором запущена YDB
+ `TEST_DB` - база данных на котором будет проходить тесты производительности
+ `STATIC_NODE_GRPC_PORT` - GRPC порт статической ноды
+ `YCSB_NODES` - список нод на которых будет запускаться YCSB
+ `YCSB_NODES_COUNT` - если хотите ограничить количество `YCSB_NODES` без изменений списка
+ `YCSB_TAR_PATH` - путь до пакета YCSB на компьютере, где запускается скрипт
+ `YCSB_DEPLOY_PATH` - путь, где должна развернуться пакет и вспомогательные файлы на `YCSB_NODES`

После настройки `ydb.rc` и `workload.rc` (про него [ниже](#workload)) можно запустить YCSB:
```sh
cd <PATH_TO_BENCHHELPERS>/ycsb
./run_workloads.sh --log-dir <PATH_TO_LOG_DIR> configs/workload.rc configs/ydb.rc
```
Также для `run_workloads.sh` есть параметры:
+ `--name` - для удобства названия файлов с логами будет сопровождаться с `name`
+ `--threads` - количество потоков для YCSB (default - 64)
+ `--de-threads` - количество потоков для YCSB при workload D и E (default - 512)
+ `--ycsb-nodes` - то же самое, что и `YCSB_NODES_COUNT`, но с большим приоритетом


### CockroachDB

---

Также как и с YDB настроим конфиг [cockroach.rc](./ycsb/configs/cockroach.rc):

+ `TARGET` - один из кластеров на котором запущена CockroachDB
+ `YCSB_NODES` - список нод на которых будет запускаться YCSB
+ `YCSB_NODES_COUNT` - если хотите ограничить количество `YCSB_NODES` без изменений списка 
+ `COCKROACH` - путь до папки с CockroachDB в `YCSB_NODES`
+ `COCKROACH_TAR_PATH` - при отсутствии `COCKROACH`, архив по этому пути будет распаковываться в `COCKROACH_DEPLOY_PATH`
+ `HA_PROXY_NODE` - один из нодов на котором запущен haproxy
+ `COCKROACH_INIT_SLEEP_TIME_MINUTES` - иногда экспорт завершается с ошибкой CLI, но продолжается в cockroach, поэтому продолжаем ждать

После настройки `cockroach.rc` и `workload.rc` (про него [ниже](#workload)) можно запустить YCSB:
```sh
cd <PATH_TO_BENCHHELPERS>/ycsb
./run_workloads.sh --type cockroach --log-dir <PATH_TO_LOG_DIR> configs/workload.rc configs/cockroach.rc
```
Про дополнительные параметры `run_workloads.sh` можно почитать в [YDB](#ydb).

### YugabyteDB

---

Настроим конфиг [yugabyte.rc](./ycsb/configs/yugabyte.rc):

+ `TARGET` - один из кластеров на котором запущена YugabyteDB
+ `YCSB_NODES` - список нод на которых будет запускаться YCSB
+ `YCSB_NODES_COUNT` - если хотите ограничить количество `YCSB_NODES` без изменений списка 
+ `YU_YCSB_PATH` - путь до бинарного файла cockroach на `YCSB_NODES`
+ `YU_YCSB_TAR_PATH` - при отсутствии `YU_YCSB_PATH`, архив по этому пути будет распаковываться в `YU_YCSB_DEPLOY_PATH`
+ `YU_PATH` - путь до папки с YugabyteDB в `TARGET`

После настройки `yugabyte.rc` и `workload.rc` (про него [ниже](#workload)) можно запустить YCSB:
```sh
cd <PATH_TO_BENCHHELPERS>/ycsb
./run_workloads.sh --type yugabyte --log-dir <PATH_TO_LOG_DIR> configs/workload.rc configs/yugabyte.rc
```
Про дополнительные параметры `run_workloads.sh` можно почитать в [YDB](#ydb).

### Workload



