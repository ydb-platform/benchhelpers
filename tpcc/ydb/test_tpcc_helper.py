#!/usr/bin/env python3

import unittest
from tpcc_helper import HostConfig

class TestHostConfig(unittest.TestCase):
    def test_start_from(self):
        warehouses = 29
        node_count = 5

        config = HostConfig(warehouses, node_count, 1)
        self.assertEqual(config.node_num, 1)
        self.assertEqual(config.start_warehouse, 1)

        node_num = 1
        for gold_start in range(1, 26, 6):
            config = HostConfig(warehouses, node_count, node_num)
            self.assertEqual(config.node_num, node_num)
            self.assertEqual(config.start_warehouse, gold_start)
            node_num += 1

    def test_all_shards_loaded1(self):
        warehouses = 10000
        node_count = 5

        configs = [HostConfig(warehouses, node_count, i) for i in range(1, node_count + 1)]
        for i in range(1, node_count):
            prev_end = configs[i-1].start_warehouse + configs[i-1].warehouses_per_host - 1
            self.assertEqual(configs[i].start_warehouse - prev_end, 1)

        last_wh = configs[-1].start_warehouse + configs[-1].warehouses_per_host - 1
        self.assertEqual(last_wh, warehouses)

    def test_all_shards_loaded2(self):
        warehouses = 10000
        node_count = 5

        configs = [HostConfig(warehouses, node_count, i) for i in range(1, node_count + 1)]

        for i in range(1, node_count):
            prev_end = configs[i-1].start_warehouse + configs[i-1].warehouses_per_host - 1
            self.assertEqual(configs[i].start_warehouse - prev_end, 1)

        last_wh = configs[-1].start_warehouse + configs[-1].warehouses_per_host - 1
        self.assertEqual(last_wh, warehouses)


if __name__ == '__main__':
    unittest.main()
