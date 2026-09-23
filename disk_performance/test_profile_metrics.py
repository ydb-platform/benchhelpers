"""Counter/histogram fixtures; no storage devices are accessed."""

import copy
import unittest

from profile_metrics import aggregate_runs, check_stability, snapshot, window_metrics


def direction(bins, runtime=60000, bs=4096):
    n = sum(bins.values())
    mean = sum(int(bucket) * count for bucket, count in bins.items()) / n if n else 0
    return {
        "io_bytes": n * bs,
        "total_ios": n,
        "runtime": runtime if n else 0,
        "clat_ns": {"N": n, "mean": mean, "max": max(map(int, bins), default=0), "bins": bins},
        "lat_ns": {"N": n, "mean": mean + 10 if n else 0},
    }


def job(read_bins=None, write_bins=None, runtime=60000):
    return {
        "error": 0,
        "read": direction(read_bins or {}, runtime),
        "write": direction(write_bins or {}, runtime),
        "job_runtime": runtime,
        "usr_cpu": 10,
        "sys_cpu": 20,
        "iodepth_level": {"1": 100},
    }


def minute(read_bw=100, write_bw=100, read_lat=1000, write_lat=1000):
    return {
        "read": {"bw_bytes": read_bw, "clat_mean_ns": read_lat, "io_count": 10},
        "write": {"bw_bytes": write_bw, "clat_mean_ns": write_lat, "io_count": 10},
    }


class WindowMetricsTests(unittest.TestCase):
    def test_snapshot_rejects_failed_and_multiple_jobs(self):
        failed = job()
        failed["error"] = 5
        for payload in ({"jobs": [failed]}, {"jobs": [job(), job()]}, {"jobs": []}, {}):
            with self.assertRaises(ValueError):
                snapshot(payload)

    def test_snapshot_rejects_missing_histogram_coverage(self):
        failed = job({"100": 1})
        failed["read"]["clat_ns"]["bins"] = {}
        with self.assertRaisesRegex(ValueError, "coverage"):
            snapshot({"jobs": [failed]})

    def test_live_snapshot_histogram_race_is_exposed(self):
        payload = job({"100": 10000})
        payload["read"]["clat_ns"]["N"] = 9998
        metrics = window_metrics(None, payload)
        self.assertEqual(metrics["read"]["sample_count"], 10000)
        self.assertEqual(metrics["read"]["clat_sample_count"], 9998)
        self.assertEqual(metrics["read"]["histogram_sample_drift"], 2)

    def test_snapshot_rejects_invalid_counters(self):
        for value in (-1, float("nan"), 2.5, True):
            failed = job()
            failed["write"]["io_bytes"] = value
            with self.assertRaises(ValueError):
                snapshot({"jobs": [failed]})

    def test_window_subtracts_counts_weighted_means_and_histograms(self):
        before = job({"100": 60}, {"200": 20})
        after = job({"100": 60, "400": 120}, {"200": 20, "800": 40}, runtime=120000)
        after["usr_cpu"] = 20
        result = window_metrics(before, after)
        self.assertEqual(result["read"]["iops"], 2)
        self.assertEqual(result["read"]["bw_bytes"], 8192)
        self.assertEqual(result["read"]["io_count"], 120)
        self.assertEqual(result["read"]["clat_mean_ns"], 400)
        self.assertEqual(result["read"]["lat_mean_ns"], 410)
        self.assertEqual(result["read"]["histogram_ns"], {"400": 120})
        self.assertEqual(result["write"]["clat_mean_ns"], 800)
        self.assertEqual(result["achieved_read_pct"], 75)
        self.assertEqual(result["achieved_read_byte_pct"], 75)
        self.assertEqual(result["cpu_user"], 30)
        self.assertEqual(result["depth_scope"], "cumulative_job")

    def test_percentiles_use_merged_counts_not_average_percentiles(self):
        result = window_metrics(None, job({"100": 950, "1000": 49, "10000": 1}))
        self.assertEqual(result["read"]["percentiles_ns"], {"50": 100, "90": 100, "95": 100, "99": 1000, "99.9": 1000})
        self.assertEqual(result["read"]["clat_max_ns"], 10000)
        self.assertFalse(result["read"]["clat_max_approximate"])

    def test_full_job_preserves_exact_maximum(self):
        payload = job({"100": 60})
        payload["read"]["clat_ns"]["max"] = 103
        result = window_metrics(None, payload)
        self.assertEqual(result["read"]["clat_max_ns"], 103)
        self.assertFalse(result["read"]["clat_max_approximate"])

    def test_delta_maximum_is_approximate_and_excludes_previous_buckets(self):
        before = job({"10000": 60})
        after = job({"10000": 60, "100": 60}, runtime=120000)
        after["read"]["clat_ns"]["max"] = 10001
        result = window_metrics(before, after)
        self.assertEqual(result["read"]["clat_max_ns"], 100)
        self.assertTrue(result["read"]["clat_max_approximate"])

    def test_missing_exact_maximum_falls_back_to_histogram(self):
        payload = job({"100": 60})
        del payload["read"]["clat_ns"]["max"]
        result = window_metrics(None, payload)
        self.assertEqual(result["read"]["clat_max_ns"], 100)
        self.assertTrue(result["read"]["clat_max_approximate"])

    def test_inactive_direction_is_not_fabricated_latency(self):
        result = window_metrics(None, job({"100": 60}))
        self.assertEqual(result["achieved_read_pct"], 100)
        self.assertEqual(result["write"]["iops"], 0)
        self.assertIsNone(result["write"]["clat_mean_ns"])
        self.assertIsNone(result["write"]["percentiles_ns"]["99"])

    def test_counter_regression_and_zero_runtime_fail(self):
        before = job({"100": 60})
        with self.assertRaisesRegex(ValueError, "decreased"):
            window_metrics(before, job({"100": 59}, runtime=120000))
        after = job({"100": 60, "200": 1})
        with self.assertRaisesRegex(ValueError, "measurement time"):
            window_metrics(before, after)

    def test_histogram_regression_fails_even_if_total_increases(self):
        with self.assertRaisesRegex(ValueError, "bins"):
            window_metrics(job({"100": 60}), job({"100": 59, "200": 2}, runtime=120000))

    def test_payload_not_mutated(self):
        payload = job({"100": 20}, {"500": 10})
        original = copy.deepcopy(payload)
        window_metrics(None, payload)
        self.assertEqual(payload, original)


class StabilityTests(unittest.TestCase):
    def test_stable_both_directions(self):
        result = check_stability([minute()] * 5, 70)
        self.assertTrue(result["stable"])
        self.assertEqual(len(result["checks"]), 4)

    def test_insufficient_windows(self):
        self.assertFalse(check_stability([minute()] * 4, 70)["stable"])

    def test_declining_throughput_rejected_by_slope(self):
        windows = [minute(read_bw=bw) for bw in (108, 104, 100, 96, 92)]
        check = check_stability(windows, 70)["checks"]["read.bw_bytes"]
        self.assertLess(check["range_ratio"], 0.2)
        self.assertGreater(check["slope_change_ratio"], 0.1)
        self.assertFalse(check["stable"])

    def test_oscillation_rejected_by_range(self):
        windows = [minute(write_lat=lat) for lat in (800, 1200, 1000, 1200, 800)]
        check = check_stability(windows, 70)["checks"]["write.clat_mean_ns"]
        self.assertEqual(check["slope_change_ratio"], 0)
        self.assertFalse(check["stable"])

    def test_reads_stable_writes_unstable(self):
        windows = [minute(write_bw=bw) for bw in (200, 175, 150, 125, 100)]
        self.assertFalse(check_stability(windows, 70)["stable"])
        self.assertTrue(check_stability(windows, 100)["stable"])

    def test_inactive_endpoint_direction_ignored(self):
        self.assertTrue(check_stability([minute(read_bw=0, read_lat=None)] * 5, 0)["stable"])
        self.assertTrue(check_stability([minute(write_bw=0, write_lat=None)] * 5, 100)["stable"])

    def test_zero_active_samples_and_nonfinite_fail(self):
        for invalid in (0, None, float("nan")):
            self.assertFalse(check_stability([minute(write_bw=invalid)] * 5, 70)["stable"])
        windows = [minute() for _ in range(5)]
        windows[2]["write"]["io_count"] = 0
        self.assertFalse(check_stability(windows, 70)["stable"])

    def test_latest_five_windows_only(self):
        self.assertTrue(check_stability([minute(read_bw=100000)] + [minute()] * 5, 70)["stable"])


class RepeatAggregationTests(unittest.TestCase):
    def test_empty_runs_rejected(self):
        with self.assertRaisesRegex(ValueError, "empty"):
            aggregate_runs([])

    def test_total_is_median_of_totals_not_sum_of_direction_medians(self):
        runs = [window_metrics(None, job({"100": read * 60}, {"200": write * 60}))
                for read, write in [(100, 1000), (1000, 100), (1000, 1000)]]
        result = aggregate_runs(runs)
        self.assertEqual(result["read"]["iops"], 1000)
        self.assertEqual(result["write"]["iops"], 1000)
        self.assertEqual(result["total_iops"], 1100)
        self.assertEqual(result["total_bw_bytes"], 1100 * 4096)
        stats = result["repeat_statistics"]["total_iops"]
        self.assertEqual((stats["min"], stats["median"], stats["max"]), (1100, 1100, 2000))
        self.assertAlmostEqual(stats["stdev"], 519.6152422706632)
        self.assertEqual(result["run_count"], 3)
        self.assertEqual(result["stdev_scope"], "sample_standard_deviation")

    def test_median_run_percentile_distinct_from_pooled_percentile(self):
        runs = [window_metrics(None, job({"100": 1})),
                window_metrics(None, job({"1000": 1})),
                window_metrics(None, job({"10000": 1000}))]
        result = aggregate_runs(runs)
        self.assertEqual(result["read"]["percentiles_ns"]["99"], 1000)
        self.assertEqual(result["read"]["pooled_percentiles_ns"]["99"], 10000)
        self.assertEqual(result["read"]["histogram_ns"], {"100": 1, "1000": 1, "10000": 1000})
        self.assertEqual(result["read"]["sample_count"], 1002)
        self.assertEqual(result["read"]["clat_max_ns"], 1000)
        self.assertEqual(result["read"]["maximum_observed_clat_ns"], 10000)
        self.assertEqual(result["repeat_statistics"]["read"]["percentiles_ns"]["90"]["median"], 1000)

    def test_achieved_ratios_pool_counts_and_bytes_not_run_percentages(self):
        reads = job({"100": 10})
        writes = job(write_bins={"200": 100})
        writes["write"]["io_bytes"] *= 2
        result = aggregate_runs([window_metrics(None, payload) for payload in (reads, writes, writes)])
        self.assertAlmostEqual(result["achieved_read_pct"], 100 * 10 / 210)
        self.assertAlmostEqual(result["achieved_read_byte_pct"], 100 * 10 / 410)
        self.assertEqual(result["ratio_scope"], "pooled_io_counts")
        self.assertEqual(result["read"]["io_count"], 10)
        self.assertEqual(result["write"]["io_count"], 200)

    def test_inactive_direction_remains_na_and_gets_no_tail_warning(self):
        result = aggregate_runs([window_metrics(None, job({"100": 200000}))] * 5)
        self.assertEqual(result["write"]["iops"], 0)
        self.assertIsNone(result["write"]["clat_mean_ns"])
        self.assertIsNone(result["write"]["percentiles_ns"]["99"])
        self.assertIsNone(result["write"]["pooled_percentiles_ns"]["99"])
        self.assertIsNone(result["repeat_statistics"]["write"]["clat_mean_ns"]["stdev"])
        self.assertFalse(result["write"]["tail_quality"]["active"])
        self.assertEqual(result["quality_flags"], [])

    def test_pooled_coverage_does_not_hide_low_sample_individual_runs(self):
        result = aggregate_runs([window_metrics(None, job({"100": 30000}))] * 5)
        quality = result["read"]["tail_quality"]
        self.assertEqual(quality["minimum_run_tail_samples"]["99.9"], 30)
        self.assertEqual(quality["pooled_tail_samples"]["99.9"], 150)
        self.assertIn("99.9", quality["low_sample_percentiles"])
        self.assertNotIn("99.9", quality["pooled_low_sample_percentiles"])
        self.assertIn("read:p99.9_low_tail_sample_count", result["quality_flags"])

    def test_tail_advisory_threshold_boundary(self):
        result = aggregate_runs([window_metrics(None, job({"100": 100000}))] * 5)
        self.assertEqual(result["read"]["tail_quality"]["minimum_run_tail_samples"]["99.9"], 100)
        self.assertEqual(result["quality_flags"], [])

    def test_approximate_maximum_metadata_preserved(self):
        exact = window_metrics(None, job({"100": 60}))
        approximate = window_metrics(job({"100": 60}), job({"100": 60, "1000": 60}, runtime=120000))
        result = aggregate_runs([exact, approximate])
        self.assertTrue(result["read"]["clat_max_approximate"])
        self.assertFalse(result["write"]["clat_max_approximate"])
        self.assertFalse(aggregate_runs([exact, exact])["read"]["clat_max_approximate"])

    def test_one_repeat_preserves_estimates_but_flags_no_repeat_evidence(self):
        run = window_metrics(None, job({"100": 100000}))
        result = aggregate_runs([run])
        self.assertEqual(result["read"]["iops"], run["read"]["iops"])
        self.assertEqual(result["read"]["percentiles_ns"], run["read"]["percentiles_ns"])
        self.assertEqual(result["repeat_statistics"]["total_iops"]["stdev"], 0)
        self.assertEqual(result["quality_flags"], ["single_repeat"])

    def test_even_repeat_median_and_standard_deviation(self):
        result = aggregate_runs([window_metrics(None, job({"100": 60})),
                                 window_metrics(None, job({"300": 180}))])
        self.assertEqual(result["read"]["iops"], 2)
        self.assertEqual(result["read"]["clat_mean_ns"], 200)
        self.assertAlmostEqual(result["repeat_statistics"]["read"]["iops"]["stdev"], 2 ** .5)

    def test_invalid_histograms_and_nonfinite_metrics_rejected(self):
        run = window_metrics(None, job({"100": 100000}))
        broken = copy.deepcopy(run)
        broken["read"]["histogram_ns"] = {}
        with self.assertRaisesRegex(ValueError, "coverage"):
            aggregate_runs([broken])
        broken = copy.deepcopy(run)
        broken["total_iops"] = float("nan")
        with self.assertRaisesRegex(ValueError, "finite"):
            aggregate_runs([broken])

    def test_inputs_not_mutated(self):
        runs = [window_metrics(None, job({"100": 100}, {"200": 60}))]
        original = copy.deepcopy(runs)
        aggregate_runs(runs)
        self.assertEqual(runs, original)


if __name__ == "__main__":
    unittest.main()
