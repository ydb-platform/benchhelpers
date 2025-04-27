# GRPC Ping Microbenchmark

A simple gRPC ping microbenchmark to measure RPC latency and compatible with YDB ping service.

Consists of two tools:
* `grpc_ping_server` provided for testing purpose and to debug gRPC performance
* `grpc_ping_clinet` can be used to ping gRPC layer in YDB or with `grpc_ping_server`.

Disclaimer: this code is not well polished and is primarily written just to better understand gRPC performance and test some hypotheses.

## Dependencies

- CMake (>= 3.13)
- C++20 compatible compiler

To build you might want at least these packages:
```
sudo apt install libssl-dev libc-ares-dev zlib1g-dev libre2-dev
```
And optioally install libabsl-dev if package is available.

## Building

```bash
mkdir build
cd build
cmake [-DCMAKE_BUILD_TYPE=Release] [-DCMAKE_CXX_COMPILER=/usr/bin/clang++-18] ..
cmake --build . [-- -j N]
```

## Running client

Run it with:

```bash
./grpc_ping_client [options]
```

### Command-line Options

- `-h, --help`           Show help message
- `--host <hostname>`    Server hostname (default: localhost)
- `--port <port>`        Server port (default: 2137)
- `--inflight <N>`       Number of concurrent requests (default: 32)
- `--min-inflight <N>`   Minimum number of concurrent requests (default: 1)
- `--max-inflight <N>`   Maximum number of concurrent requests (default: 32)
- `--interval <seconds>` Benchmark duration in seconds (default: 10)
- `--warmup <seconds>`   Warmup duration in seconds (default: 1)
- `--streaming`          Use streaming RPC instead of unary RPC (default: false)

### Example

```bash
# Basic usage with fixed inflight
./grpc_ping_client --host myserver --port 2137 --inflight 64 --interval 30 --warmup 5

# Using streaming RPC
./grpc_ping_client --host myserver --port 2137 --inflight 64 --interval 30 --warmup 5 --streaming

# Using min and max inflight to test different concurrency levels
./grpc_ping_client --host myserver --port 2137 --min-inflight 1 --max-inflight 64 --interval 30 --warmup 5

# Using min and max inflight with streaming
./grpc_ping_client --host myserver --port 2137 --min-inflight 1 --max-inflight 64 --interval 30 --warmup 5 --streaming
```

## Running server

Run it with:

```bash
./grpc_ping_server [options]
```

### Command-line Options

- `-h, --help`           Show help message
- `--port <port>`        Server port (default: 2137)
- `--num-cqs <N>`        Number of completion queues (default: 1)
- `--workers-per-cq <N>` Number of worker threads per completion queue (default: 1)
- `--callbacks-per-cq <N>` Number of callbacks per completion queue (default: 100)

### Example

```bash
./grpc_ping_server --port 2137 --num-cqs 8 --workers-per-cq 2 --callbacks-per-cq 10
```