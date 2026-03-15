# Select1 Benchmark Tool

This tool micro benchmarks jdbc supported DBMS performance by running SELECT 1 queries with different concurrency levels.

For the list of databases check `profiles` in `pom.xml`. At this moment tested with YDB and PostgreSQL only.

## Requirements

- Java 11 or later
- Maven 3.6 or later
- Database running

## Building

To build the project, run:

```bash
mvn clean package -P [ydb|postgres|...]
```

This will create a fat JAR file in the `target` directory named `select1-benchmark-1.0-SNAPSHOT.jar`.

## Usage

Run the benchmark with:

```bash
java -jar target/select1-benchmark-1.0-SNAPSHOT.jar [--jdbc-url <url>] [--min-inflight <min>] [--max-inflight <max>] [--interval <seconds>] [--format <format>] [--linear] [--use-concurrency-limits] [--limiter-initial-limit <limit>] [--limiter-name <name>] [--limiter-algorithm <vegas|balanced-vegas|balanced-vegas2|balanced-vegas3|gradient|aimd>]
```

or using legacy connection parameters:

```bash
java -jar target/select1-benchmark-1.0-SNAPSHOT.jar [--host <hostname>] [--port <port>] [--user <username>] [--password <password>] [--min-inflight <min>] [--max-inflight <max>] [--interval <seconds>] [--format <format>] [--linear] [--use-concurrency-limits] [--limiter-initial-limit <limit>] [--limiter-name <name>] [--limiter-algorithm <vegas|balanced-vegas|balanced-vegas2|balanced-vegas3|gradient|aimd>]
```

### Arguments

#### Connection Options
- `--jdbc-url` or `-j`: JDBC connection URL (overrides host/port/user/password options)
- `--host` or `-h`: Database server hostname (default: localhost)
- `--port` or `-p`: Database server port (default: 5432)
- `--user` or `-u`: Database username (default: postgres)
- `--password` or `-w`: Database password (default: postgres)

#### Benchmark Options
- `--min-inflight` or `-m`: Minimum number of concurrent connections (default: 1)
- `--max-inflight` or `-M`: Maximum number of concurrent connections (default: 64)
- `--interval` or `-i`: Duration in seconds to run each inflight level (default: 5)
- `--format` or `-f`: Output format: human or csv (default: human)
- `--linear` or `-l`: Use linear inflight growth (`+1`) instead of exponential growth (`*2`)

#### Client-side Concurrency Limiter Options
- `--use-concurrency-limits` or `-c`: Enable Netflix client-side concurrency limiter for each query (blocking mode)
- `--limiter-initial-limit`: Initial limiter value when limiter is enabled (default: 16)
- `--limiter-name`: Limiter name used in limiter metrics (default: `select1-query`)
- `--limiter-algorithm`: Limiter algorithm when limiter is enabled: `vegas`, `balanced-vegas`, `balanced-vegas2`, `balanced-vegas3`, `gradient`, or `aimd` (default: `vegas`)

### Examples

Using JDBC URL (recommended):
```bash
# PostgreSQL with human-readable output
java -jar target/select1-benchmark-1.0-SNAPSHOT.jar --jdbc-url "jdbc:postgresql://localhost:5432/postgres?user=postgres&password=postgres"

# YDB with CSV output
java -jar target/select1-benchmark-1.0-SNAPSHOT.jar --jdbc-url "jdbc:ydb:grpc://localhost:2135/local" --format csv

# PostgreSQL with client-side concurrency limits enabled
java -jar target/select1-benchmark-1.0-SNAPSHOT.jar \
  --jdbc-url "jdbc:postgresql://localhost:5432/postgres?user=postgres&password=postgres" \
  --min-inflight 1 --max-inflight 64 --interval 10 \
  --use-concurrency-limits --limiter-algorithm vegas \
  --limiter-initial-limit 16 --limiter-name pg-select1 \
  --format human

# YDB with gradient limiter
java -jar target/select1-benchmark-1.0-SNAPSHOT.jar \
  --jdbc-url "jdbc:ydb:grpc://localhost:2135/Root/db1" \
  --min-inflight 8 --max-inflight 128 --interval 60 \
  --use-concurrency-limits --limiter-algorithm gradient \
  --limiter-initial-limit 16 --limiter-name ydb-select1 \
  --format human

# YDB with balanced vegas limiter (better p50 vs tail tradeoff)
java -jar target/select1-benchmark-1.0-SNAPSHOT.jar \
  --jdbc-url "jdbc:ydb:grpc://localhost:2135/Root/db1" \
  --min-inflight 8 --max-inflight 128 --interval 60 \
  --use-concurrency-limits --limiter-algorithm balanced-vegas \
  --limiter-initial-limit 16 --limiter-name ydb-select1 \
  --format human

# YDB with balanced vegas2 limiter (more permissive than balanced-vegas)
java -jar target/select1-benchmark-1.0-SNAPSHOT.jar \
  --jdbc-url "jdbc:ydb:grpc://localhost:2135/Root/db1" \
  --min-inflight 8 --max-inflight 128 --interval 60 \
  --use-concurrency-limits --limiter-algorithm balanced-vegas2 \
  --limiter-initial-limit 16 --limiter-name ydb-select1 \
  --format human

# YDB with balanced vegas3 limiter (aggressive growth profile)
java -jar target/select1-benchmark-1.0-SNAPSHOT.jar \
  --jdbc-url "jdbc:ydb:grpc://localhost:2135/Root/db1" \
  --min-inflight 8 --max-inflight 128 --interval 60 \
  --use-concurrency-limits --limiter-algorithm balanced-vegas3 \
  --limiter-initial-limit 16 --limiter-name ydb-select1 \
  --format human
```

Using legacy connection parameters:
```bash
java -jar target/select1-benchmark-1.0-SNAPSHOT.jar --host localhost --port 5432 --user postgres --password postgres
```

Full options:
```bash
java -jar target/select1-benchmark-1.0-SNAPSHOT.jar --jdbc-url "jdbc:postgresql://localhost:5432/postgres?user=postgres&password=postgres" --min-inflight 1 --max-inflight 32 --interval 30 --format csv
```

## Output Formats

Latency fields:
- `Pure latency`: measured around `SELECT 1` execution only.
- `Full latency`: measured from before limiter acquire to query completion (`wait-for-slot + query`).

### Human-readable Format (default)
```
Benchmark Results
=======================================================================================================================================================================
Inflight  RPS           Pure P50 (µs)  Pure P90 (µs)  Pure P99 (µs)  Pure P99.9 (µs)  Full P50 (µs)  Full P90 (µs)  Full P99 (µs)  Full P99.9 (µs)
-----------------------------------------------------------------------------------------------------------------------------------------------------------------------
1         1000.50       100.25         200.75         500.00         1000.00          100.40         201.10         501.20         1001.50
2         2000.75       150.50         300.25         750.00         1500.00          180.90         420.30         980.10         1800.25
4         4000.25       200.75         400.50         1000.00        2000.00          260.80         530.60         1200.70        2200.90
=======================================================================================================================================================================
```

### CSV Format
```
What,1,2,4
Pure latency p50 (µs),100.25,150.50,200.75
Pure latency p90 (µs),200.75,300.25,400.50
Pure latency p99 (µs),500.00,750.00,1000.00
Pure latency p99.9 (µs),1000.00,1500.00,2000.00
Full latency p50 (µs),100.40,180.90,260.80
Full latency p90 (µs),201.10,420.30,530.60
Full latency p99 (µs),501.20,980.10,1200.70
Full latency p99.9 (µs),1001.50,1800.25,2200.90
Throughput (RPS),1000.50,2000.75,4000.25
```
