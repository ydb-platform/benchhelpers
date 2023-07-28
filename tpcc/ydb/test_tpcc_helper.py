#!/usr/bin/env python3

import unittest
from tpcc_helper import HostConfig

class TestHostConfig(unittest.TestCase):
    def test_start_from(self):
        warehouses = 29
        node_count = 5
        shard_count = 15

        config = HostConfig(warehouses, node_count, shard_count, 1)
        self.assertEqual(config.node_num, 1)
        self.assertEqual(config.start_warehouse, 1)

        node_num = 1
        for gold_start in range(1, 26, 6):
            config = HostConfig(warehouses, node_count, shard_count, node_num)
            self.assertEqual(config.node_num, node_num)
            self.assertEqual(config.start_warehouse, gold_start)
            node_num += 1

    def test_split_keys1(self):
        warehouses = 29
        node_count = 5
        shard_count = 15

        # each node has 3 shards. First two nodes: 6 wh, last - 5 wh.
        # first host: [1; 6] - 3 shards: 3, 5
        # second host: [7; 13] - 3 shards: 7, 9, 11
        #
        # last host: [25; 29] with splits 25, 27, 29

        config = HostConfig(warehouses, node_count, shard_count, 1)
        self.assertEqual(config.warehouses_per_host, 6)
        self.assertEqual(config.warehouses_per_shard, 2)
        self.assertEqual(config.shards_per_host, 3)

        split_keys = config.get_split_keys()
        self.assertEqual(len(split_keys), 2)
        self.assertEqual(split_keys, [3, 5])

        config = HostConfig(warehouses, node_count, shard_count, 2)
        self.assertEqual(config.warehouses_per_host, 6)
        self.assertEqual(config.warehouses_per_shard, 2)
        self.assertEqual(config.shards_per_host, 3)

        split_keys = config.get_split_keys()
        self.assertEqual(len(split_keys), 3)
        self.assertEqual(split_keys, [7, 9, 11])

        config = HostConfig(warehouses, node_count, shard_count, 5)
        self.assertEqual(config.warehouses_per_host, 5)
        self.assertEqual(config.warehouses_per_shard, 1)
        self.assertEqual(config.shards_per_host, 3)

        split_keys = config.get_split_keys()
        self.assertEqual(len(split_keys), 3)
        self.assertEqual(split_keys, [25, 26, 27])

    def test_split_keys2(self):
        warehouses = 29
        node_count = 5
        shard_count = 25

        # each node has 5 shards. First two nodes: 6 wh, last - 5 wh.
        # first host: [1; 6] - 5 shards: 2, 3, 4, 5
        # second host: [7; 13] - 5 shards: 7, 8, 9, 10, 11
        #
        # last host: [25; 29] with splits 25, 26, 27, 28, 29

        config = HostConfig(warehouses, node_count, shard_count, 1)
        self.assertEqual(config.start_warehouse, 1)
        self.assertEqual(config.warehouses_per_host, 6)
        self.assertEqual(config.warehouses_per_shard, 1)
        self.assertEqual(config.shards_per_host, 5)

        split_keys = config.get_split_keys()
        self.assertEqual(len(split_keys), 4)
        self.assertEqual(split_keys, [2, 3, 4, 5])

        config = HostConfig(warehouses, node_count, shard_count, 2)
        self.assertEqual(config.start_warehouse, 7)
        self.assertEqual(config.warehouses_per_host, 6)
        self.assertEqual(config.warehouses_per_shard, 1)
        self.assertEqual(config.shards_per_host, 5)

        split_keys = config.get_split_keys()
        self.assertEqual(len(split_keys), 5)
        self.assertEqual(split_keys, [7, 8, 9, 10, 11])

        config = HostConfig(warehouses, node_count, shard_count, 5)
        self.assertEqual(config.start_warehouse, 25)
        self.assertEqual(config.warehouses_per_host, 5)
        self.assertEqual(config.warehouses_per_shard, 1)
        self.assertEqual(config.shards_per_host, 5)

        split_keys = config.get_split_keys()
        self.assertEqual(len(split_keys), 5)
        self.assertEqual(split_keys, [25, 26, 27, 28, 29])

    def test_split_keys3(self):
        warehouses = 27
        node_count = 4
        shard_count = 10

        # 3 nodes with 3 shards and one with 1
        # 3 nodes with 7 wh and one with 6 wh

        config = HostConfig(warehouses, node_count, shard_count, 1)
        self.assertEqual(config.warehouses_per_host, 7)
        self.assertEqual(config.warehouses_per_shard, 2)
        self.assertEqual(config.shards_per_host, 3)

        split_keys = config.get_split_keys()
        self.assertEqual(len(split_keys), 2)
        self.assertEqual(split_keys, [3, 5])

        config = HostConfig(warehouses, node_count, shard_count, 2)
        self.assertEqual(config.warehouses_per_host, 7)
        self.assertEqual(config.shards_per_host, 3)

        split_keys = config.get_split_keys()
        self.assertEqual(len(split_keys), 3)
        self.assertEqual(split_keys, [8, 10, 12])

        config = HostConfig(warehouses, node_count, shard_count, 4)
        self.assertEqual(config.warehouses_per_host, 6)
        self.assertEqual(config.warehouses_per_shard, 6)
        self.assertEqual(config.shards_per_host, 1)

        split_keys = config.get_split_keys()
        self.assertEqual(len(split_keys), 1)
        self.assertEqual(split_keys, [22,])

    def test_all_shards_loaded1(self):
        warehouses = 10000
        node_count = 5
        shard_count = 256 * node_count

        configs = [HostConfig(warehouses, node_count, shard_count, i) for i in range(1, node_count + 1)]
        for i in range(1, node_count):
            prev_end = configs[i-1].start_warehouse + configs[i-1].warehouses_per_host - 1
            self.assertEqual(configs[i].start_warehouse - prev_end, 1)

        last_wh = configs[-1].start_warehouse + configs[-1].warehouses_per_host - 1
        self.assertEqual(last_wh, warehouses)

    def test_all_shards_loaded2(self):
        warehouses = 10000
        node_count = 5
        shard_count = 512 * node_count

        configs = [HostConfig(warehouses, node_count, shard_count, i) for i in range(1, node_count + 1)]
        print("Config0 wh per shard:{}, per_host:{}".format(configs[0].warehouses_per_shard, configs[0].warehouses_per_host))

        for i in range(1, node_count):
            prev_end = configs[i-1].start_warehouse + configs[i-1].warehouses_per_host - 1
            self.assertEqual(configs[i].start_warehouse - prev_end, 1)

        last_wh = configs[-1].start_warehouse + configs[-1].warehouses_per_host - 1
        self.assertEqual(last_wh, warehouses)


if __name__ == '__main__':
    unittest.main()
