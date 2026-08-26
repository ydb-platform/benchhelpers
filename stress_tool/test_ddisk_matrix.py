#!/usr/bin/env python3

import unittest

import plot
import table
from normalize_stress_result import normalize


def _group(ratio, iops_by_qd, p99_by_qd, checksums="enabled"):
    inflights = []
    for qd, iops in sorted(iops_by_qd.items()):
        inflights.append(
            {
                "InFlight": qd,
                "IOPS": {
                    "min": f"{iops:.1f}",
                    "median": f"{iops:.1f}",
                    "max": f"{iops:.1f}",
                    "stdev": "0.0",
                },
                "Speed": {
                    "min": "1.0 MB/s",
                    "median": "1.0 MB/s",
                    "max": "1.0 MB/s",
                    "stdev": "0.0 MB/s",
                },
                "Runs": [{"IOPS": str(iops), "p99.00": f"{p99_by_qd[qd]} us"}],
            }
        )
    return {
        "Label": "DDisk read",
        "LogMode": "DDISK_READ",
        "TestType": "DDiskLoad",
        "Params": {"BackgroundWriteRatio": ratio, "Checksums": checksums},
        "InFlights": inflights,
    }


class NormalizeStressResultTest(unittest.TestCase):
    def test_normalizes_single_run_array(self):
        result = normalize(
            [
                {"InFlight": "1", "Speed": "40.0 MB/s", "IOPS": "10000", "p99.00": "100 us"},
                {"InFlight": "2", "Speed": "80.0 MB/s", "IOPS": "20000", "p99.00": "120 us"},
            ]
        )

        self.assertEqual([1, 2], [item["InFlight"] for item in result["InFlights"]])
        self.assertEqual("10000.0", result["InFlights"][0]["IOPS"]["median"])
        self.assertEqual("40.0 MB/s", result["InFlights"][0]["Speed"]["median"])

    def test_preserves_structured_result(self):
        source = _group(0.0, {1: 10000}, {1: 100})
        result = normalize(source)
        self.assertEqual(source, result)
        self.assertIsNot(source, result)


class BackgroundMatrixTest(unittest.TestCase):
    def setUp(self):
        self.groups = [
            _group(0.0, {1: 10000, 2: 20000}, {1: 100, 2: 120}),
            _group(0.5, {1: 8000, 2: 15000}, {1: 130, 2: 180}),
        ]

    def test_plain_matrix_contains_both_metrics(self):
        output = table._background_matrix_text(self.groups)
        self.assertIn("Read IOPS (kIOPS, median", output)
        self.assertIn("Read p99 latency", output)
        self.assertIn("10.0", output)
        self.assertIn("180.0", output)

    def test_plot_matrix_has_same_grid(self):
        qds, ratios, iops, latency, checksum_label = plot._extract_background_matrices(
            self.groups
        )
        self.assertEqual([1, 2], qds)
        self.assertEqual([0.0, 0.5], ratios)
        self.assertEqual([[10.0, 20.0], [8.0, 15.0]], iops)
        self.assertEqual([[100.0, 120.0], [130.0, 180.0]], latency)
        self.assertEqual("enabled", checksum_label)

    def test_rejects_mismatched_qd_grid(self):
        groups = [self.groups[0], _group(0.5, {1: 8000}, {1: 130})]
        with self.assertRaises(SystemExit):
            plot._extract_background_matrices(groups)


if __name__ == "__main__":
    unittest.main()
