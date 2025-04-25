# GRPC Ping Microbenchmark

A simple gRPC ping microbenchmark to measure RPC latency (compatible with YDB ping service).

## Dependencies

- CMake (>= 3.13)
- C++20 compatible compiler
- Protocol Buffers
- gRPC

## Building

```bash
mkdir build
cd build
cmake [-DCMAKE_BUILD_TYPE=Release] [-DCMAKE_CXX_COMPILER=/usr/bin/clang++-18] ..
cmake --build .
```

## Running

Run it with:

```bash
./grpc_ping_client [options]
```

### Command-line Options

- `-h, --help`           Show help message
- `--host <hostname>`    Server hostname (default: localhost)
- `--port <port>`        Server port (default: 2137)
- `--inflight <N>`       Number of concurrent requests (default: 32)
- `--interval <seconds>` Benchmark duration in seconds (default: 10)
- `--warmup <seconds>`   Warmup duration in seconds (default: 1)

### Example

```bash
./grpc_ping_client --host myserver --port 2137 --inflight 64 --interval 30 --warmup 5
```
