# ydb-tpcc-tcpdump-parse

A quick and dirty tool to parse a recorded TCP dump and calculate total/client/server NewOrder latencies.

YDB reports two transaction latencies: server side and client side. Client side metric is not purely client side:
* currently some server latency is accounted as client: at least server's GRPC and network stack.
* it includes network time.

If we get TCP dump (on the server), then this tool shows pure server and client+network time. If dump it produced
on the client side, then server time will include network, while "client+network" will show only client time.

## Build

Some dependencies:
```
sudo apt-get install protobuf-compiler libprotobuf-dev libnghttp2-dev
```

```bash
mkdir build
cd build

export CC=clang-18
export CXX=clang++-18

cmake ..
make
```

## Run

```bash
./ydb_tpcc_tcpdump_parse -n 400000 --skip 35 ~/tpcc.samle.pcap
```
