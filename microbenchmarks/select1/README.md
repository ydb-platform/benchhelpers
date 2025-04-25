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

This will create a fat JAR file in the `target` directory named `select1-benchmark-1.0-SNAPSHOT-jar-with-dependencies.jar`.

## Usage

Run the benchmark with:

```bash
java -jar target/select1-benchmark-1.0-SNAPSHOT-jar-with-dependencies.jar [--jdbc-url <url>] [--min-inflight <min>] [--max-inflight <max>] [--interval <seconds>] [--format <format>]
```

or using legacy connection parameters:

```bash
java -jar target/select1-benchmark-1.0-SNAPSHOT-jar-with-dependencies.jar [--host <hostname>] [--port <port>] [--user <username>] [--password <password>] [--min-inflight <min>] [--max-inflight <max>] [--interval <seconds>] [--format <format>]
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
- `--max-inflight` or `-M`: Maximum number of concurrent connections (default: 128)
- `--interval` or `-i`: Duration in seconds to run each inflight level (default: 10)
- `--format` or `-f`: Output format: human or csv (default: human)

### Examples

Using JDBC URL (recommended):
```bash
# PostgreSQL with human-readable output
java -jar target/select1-benchmark-1.0-SNAPSHOT-jar-with-dependencies.jar --jdbc-url "jdbc:postgresql://localhost:5432/postgres?user=postgres&password=postgres"

# YDB with CSV output
java -jar target/select1-benchmark-1.0-SNAPSHOT-jar-with-dependencies.jar --jdbc-url "jdbc:ydb:grpc://localhost:2135/local" --format csv
```

Using legacy connection parameters:
```bash
java -jar target/select1-benchmark-1.0-SNAPSHOT-jar-with-dependencies.jar --host localhost --port 5432 --user postgres --password postgres
```

Full options:
```bash
java -jar target/select1-benchmark-1.0-SNAPSHOT-jar-with-dependencies.jar --jdbc-url "jdbc:postgresql://localhost:5432/postgres?user=postgres&password=postgres" --min-inflight 1 --max-inflight 32 --interval 30 --format csv
```

## Output Formats

### Human-readable Format (default)
```
Benchmark Results
==================================================
Inflight  RPS           P50 (µs)    P90 (µs)    P99 (µs)    P99.9 (µs)
--------------------------------------------------
1         1000.50       100.25       200.75       500.00       1000.00
2         2000.75       150.50       300.25       750.00       1500.00
4         4000.25       200.75       400.50       1000.00      2000.00
==================================================
```

### CSV Format
```
What,1,2,4
Latency p50 (µs),100.25,150.50,200.75
Latency p90 (µs),200.75,300.25,400.50
Latency p99 (µs),500.00,750.00,1000.00
Latency p99.9 (µs),1000.00,1500.00,2000.00
Throughput (RPS),1000.50,2000.75,4000.25
``` 