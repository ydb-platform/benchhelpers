# benchhelpers

In this repository, you will find scripts to deploy and evaluate the performance of the 
[YDB](https://ydb.tech/), [CockroachDB](https://www.cockroachlabs.com/), and [YugabyteDB](https://www.yugabyte.com/) databases.
Note that our deployment scripts are not suitable for production. 

These scripts were used the article [YCSB performance series](https://blog.ydb.tech/ycsb-performance-series-ydb-cockroachdb-and-yugabytedb-f25c077a382b).

### Premise:
Benchmarks has a problem with the fact that it does not create load efficiently enough. 
That is, in order to load machines with databases, one benchmark shooting machine is often not enough. 

### Decision:
With the help of our scripts, you can greatly facilitate the launch on multiple machines and collection of benchmark results.


### Benchmarks that are available now:
1. [YCSB](ycsb/README.md) (YDB, CockroachDB, YugabyteDB)
2. [TPC-C](tpcc/README.md) (YDB, CockroachDB)
