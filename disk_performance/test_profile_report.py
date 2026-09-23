"""Non-destructive report fixtures; no benchmark device is required."""

import csv
import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import profile_report as report


def fixture():
    points = []
    for profile, multiplier in (("fresh", 2), ("used-recovered", 1)):
        for read_pct in (100, 50, 0):
            for qd in (1, 4):
                points.append({
                    "id": f"{profile}-{read_pct}-{qd}", "profile": profile,
                    "pattern": "random", "bs": 4096, "qd": qd,
                    "read_pct": read_pct, "status": "complete",
                    "metrics": {direction: {
                        "iops": multiplier * qd * 1000,
                        "bw_bytes": multiplier * qd * 4096000,
                        "io_count": 60000, "clat_mean_ns": 2000,
                        "clat_max_ns": 50000,
                        "percentiles_ns": {"50": 1000, "95": 3000, "99.000000": 4000, "99.9": 10000},
                    } for direction in ("read", "write")},
                    "history": [{"elapsed_s": 1, "read": {"iops": 1000, "bw_bytes": 4096000, "clat_mean_ns": 2000}}],
                })
    return {"schema_version": 1, "status": "running", "config": {
        "profiles": ["fresh", "used-recovered"], "read_percentages": [100, 50, 0], "queue_depths": [1, 4]},
        "device": {"model": "<script>alert('bad')</script>"}, "points": points,
        "warnings": ["a & b < c"]}


class ReportTests(unittest.TestCase):
    def test_schema2_medians_raw_repeats_and_qualified_partial_cells(self):
        manifest = fixture()
        manifest["schema_version"] = 2
        point = manifest["points"][0]
        manifest["points"] = [point]
        point.update(status="interrupted", quality=["2 of 5 repetitions completed"])
        raw = copy.deepcopy(point["metrics"])
        point["runs"] = [{"index": 1, "status": "complete", "metrics": raw, "seed": 123, "relative_dir": "fresh/p/1"},
                         {"index": 2, "status": "complete", "metrics": raw, "seed": 456, "relative_dir": "fresh/p/2"},
                         {"index": 3, "status": "failed", "error": "fio failed", "relative_dir": "fresh/p/3"}]
        metrics = point["metrics"]
        metrics.update(run_count=2, percentile_scope="median_of_run_percentiles",
                       repeat_statistics={"read": {"iops": {"min": 1000, "median": 2000, "max": 3000, "stdev": 1414.21},
                                                        "percentiles_ns": {"99": {"min": 3000, "median": 4000, "max": 5000, "stdev": 1414.21}}}},
                       quality_flags=["read:p99.9_low_tail_sample_count"])
        metrics["read"].update(pooled_percentiles_ns={"99": 9000}, clat_max_approximate=True, maximum_observed_clat_ns=75000)
        manifest["preparation"] = [{"profile": "fresh", "status": "complete", "conditioning_status": "time_cap_reached"}]
        manifest["budget"] = {"sample_count": 720, "run_cap_s": 20000}
        self.assertEqual(report.cell(point, "read", "iops", 1000), (2, "2.00*"))
        with tempfile.TemporaryDirectory() as directory:
            paths = report.render_report(directory, manifest, plots=False)
            with open(paths["results.csv"], newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["run_count"], "2")
            self.assertEqual(rows[0]["iops_stdev"], "1414.21")
            self.assertEqual(rows[0]["clat_p99_ns"], "4000.0")
            self.assertEqual(rows[0]["pooled_clat_p99_ns"], "9000.0")
            self.assertEqual(rows[0]["clat_max_approximate"], "True")
            with open(paths["raw-runs.csv"], newline="") as handle:
                raw_rows = list(csv.DictReader(handle))
            self.assertEqual(len(raw_rows), 6)
            self.assertEqual(raw_rows[0]["seed"], "123")
            self.assertEqual(raw_rows[4]["status"], "failed")
            self.assertEqual(raw_rows[4]["iops"], "")
            html = Path(paths["report.html"]).read_text()
            self.assertIn("median of per-run p99", html)
            self.assertIn("time_cap_reached", html)
            self.assertIn("Whole-run time budget", html)
            self.assertNotIn("Sustained results use the selected stable window", html)

    def test_reports_work_without_optional_matplotlib(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(report, "plot_report", side_effect=ModuleNotFoundError("matplotlib")):
            paths = report.render_report(directory, fixture(), plots=True)
            self.assertTrue(all(Path(path).exists() for path in paths.values()))
            html = Path(paths["report.html"]).read_text()
            self.assertIn("PNG plots unavailable", html)
            self.assertIn('class="heatmap"', html)

    def test_run_and_preparation_errors_and_recovery_are_visible(self):
        manifest = fixture()
        manifest.update(status='failed', error='fill failed <device>', preparation=[
            dict(profile='medium', status='failed', conditioning_status='time_cap_reached',
                 recovery_status='time_cap_reached', quality=['medium recovery was shortened'],
                 error='fill failed <device>')])
        with tempfile.TemporaryDirectory() as directory:
            paths = report.render_report(directory, manifest, plots=False)
            html = Path(paths['report.html']).read_text()
            text = Path(paths['result.txt']).read_text()
        self.assertIn('Run stopped: fill failed &lt;device&gt;', html)
        self.assertIn('Run stopped: fill failed <device>', text)
        self.assertIn('Recovery status', html)
        self.assertIn('medium recovery was shortened', html)
        self.assertIn('recovery=time_cap_reached', text)

    def test_successful_raw_repeat_does_not_inherit_later_case_failure(self):
        manifest = fixture()
        point = manifest['points'][0]
        point.update(status='failed', error='second repeat failed', runs=[
            dict(index=1, status='complete', metrics=point['metrics']),
            dict(index=2, status='failed', error='second repeat failed')])
        manifest['points'] = [point]
        rows = list(report.csv_rows(manifest, raw=True))
        self.assertTrue(all(row['error'] is None for row in rows if row['run_index'] == 1))
        self.assertTrue(all(row['error'] == 'second repeat failed' for row in rows if row['run_index'] == 2))

    def test_representative_trace_and_compact_qualified_accounting(self):
        manifest = fixture()
        manifest["schema_version"] = 2
        point = manifest["points"][0]
        manifest["points"] = [point]
        point["metrics"].update(total_iops=2100, depth_scope="median_of_run_cumulative_distributions")
        point["metrics"]["read"]["percentiles_ns"]["90"] = 2500
        point["metrics"]["read"]["histogram_ns"] = {"1234567": 999999}
        point["host_write_bytes"] = 2**40
        point["host_write_accounting"] = "measured_only_lower_bound"
        point["runs"] = [{"index": i, "status": "complete", "metrics": {"total_iops": rate, "read": {"histogram_ns": {"1234567": 999999}}},
                          "relative_dir": f"fresh/point/{i}"} for i, rate in enumerate((1000, 2000, 5000), 1)]
        traces = report.representative_traces(manifest)
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0]["relative_dir"], "fresh/point/2")
        self.assertIn("representative repeat 2", traces[0]["id"])
        reference = copy.deepcopy(point)
        reference.update(id="reference_early", host_write_bytes=2**40, host_write_accounting="target_kernel_counter", runs=[])
        manifest["references"] = [reference]
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(report, "plot_report", side_effect=OSError("bad optional image cache")):
            paths = report.render_report(directory, manifest, plots=True)
            html = Path(paths["report.html"]).read_text()
            self.assertIn("at least 2.00 TiB total", html)
            self.assertIn("Lower bound:", html)
            self.assertNotIn("histogram_ns", html)
            self.assertNotIn("1234567", html)
            self.assertIn("Read IOPS change %", html)
            self.assertIn("PNG plots unavailable", html)
            with open(paths["results.csv"], newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["clat_p90_ns"], "2500.0")
            self.assertEqual(row["depth_scope"], "median_of_run_cumulative_distributions")
            self.assertIn("histogram_ns", point["metrics"]["read"])

    def test_complete_grid_status_and_endpoints(self):
        manifest = fixture()
        manifest["points"][1]["status"] = "unstable"
        del manifest["points"][2]
        cells = report.matrix(manifest, "fresh", 4096, "write", "iops", 1000)
        self.assertEqual(cells[0], [(None, "N/A"), (None, "unstable")])
        self.assertEqual(cells[1][0], (None, "missing"))
        self.assertEqual(cells[2][1], (8.0, "8.00"))

    def test_common_scale_excludes_invalid_results(self):
        manifest = fixture()
        manifest["points"][1]["status"] = "failed"
        manifest["points"][1]["metrics"]["read"]["iops"] = 999999999
        probe = copy.deepcopy(manifest["points"][0])
        probe.update(qd=256, kind="saturation")
        probe["metrics"]["read"]["iops"] = 999999999
        manifest["points"].append(probe)
        self.assertEqual(report.color_scale(manifest, 4096, "read", "iops", 1000), (1, 8))
        self.assertEqual(report.color(None, (1, 8)), "#e5e7eb")
        self.assertEqual(report.cell(manifest["points"][0], "read", "p99", 1000), (4, "4.00"))

    def test_offline_artifacts_and_csv_units(self):
        manifest = fixture()
        with tempfile.TemporaryDirectory() as directory:
            paths = report.render_report(directory, manifest, plots=False)
            html = Path(paths["report.html"]).read_text()
            self.assertNotIn("<script>", html)
            self.assertIn("&lt;script&gt;", html)
            self.assertIn("a &amp; b &lt; c", html)
            self.assertNotIn("https://", html)
            self.assertIn("Read% / Write%", html)
            with open(paths["results.csv"], newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 24)
            self.assertEqual(rows[0]["clat_p99_ns"], "4000.0")
            self.assertEqual(rows[0]["bw_bytes"], "8192000")
            self.assertEqual(rows[1]["iops"], "")
            self.assertEqual(rows[1]["active"], "False")
            text = Path(paths["result.txt"]).read_text()
            self.assertIn("12 complete", text)
            self.assertIn("Write p99 completion latency (us)", text)

    def test_nonfinite_values_are_missing(self):
        point = fixture()["points"][0]
        point["metrics"]["read"]["iops"] = float("nan")
        self.assertEqual(report.cell(point, "read", "iops", 1000), (None, "missing"))

    def test_preparation_totals_title_and_log_units(self):
        manifest = fixture()
        manifest.update(started_at="2026-09-09T00:00:00+00:00", updated_at="2026-09-09T00:02:00+00:00")
        manifest["config"]["prefix"] = "<my-drive>"
        manifest["preparation"] = [{"profile": "fresh", "relative_dir": "preparation/1", "size_bytes": 2**40,
                                    "host_write_bytes": 2**40, "started_at": manifest["started_at"], "status": "running"}]
        manifest["points"][0]["host_write_bytes"] = 2**40
        manifest["points"][0]["relative_dir"] = "fresh/first"
        with tempfile.TemporaryDirectory() as directory:
            location = Path(directory, "fresh/first")
            location.mkdir(parents=True)
            (location / "series_bw.1.log").write_text("1000, 1024, 0, 4096\n1000, 512, 1, 4096\n")
            samples = report.fio_series(directory, manifest["points"][0], "bw")
            self.assertEqual(samples["read"], [(1, 1048576)])
            self.assertEqual(samples["write"], [(1, 524288)])
            paths = report.render_report(directory, manifest, plots=False)
            html = Path(paths["report.html"]).read_text()
            self.assertIn("&lt;my-drive&gt;", html)
            self.assertIn("2.00 TiB total", html)
            self.assertIn("120.00 seconds", html)
            self.assertIn("Raw preparation records", html)

    def test_manifest_cli_dispatch(self):
        manifest = fixture()
        # Keep this dispatch test fast even when matplotlib is installed.
        manifest["points"] = []
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "manifest.json").write_text(json.dumps(manifest))
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("aggregate.py")), directory], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("NVMe benchmark:", result.stdout)
            self.assertTrue(Path(directory, "report.html").exists())

    def test_large_logs_use_bounded_envelope_and_preserve_spikes(self):
        point = fixture()["points"][0]
        point["relative_dir"] = "fresh/first"
        with tempfile.TemporaryDirectory() as directory:
            location = Path(directory, point["relative_dir"])
            location.mkdir(parents=True)
            path = location / "series_iops.1.log"
            with path.open("w") as handle:
                for index in range(100_003):
                    value = 900_000 if index == 12_345 else (0 if index == 65_432 else 100)
                    handle.write(f"{index * 1000}, {value}, 0, 4096\n")
                    handle.write(f"{index * 1000}, {value // 2}, 1, 4096\n")
            original_size = path.stat().st_size
            data = report.fio_series(directory, point, "iops")
            for direction in ("read", "write"):
                values = data[direction]
                self.assertLessEqual(len(values), 10_000)
                self.assertTrue(values.downsampled)
                self.assertEqual(values, sorted(values))
                self.assertIn((65_432, 0), values)
            self.assertIn((12_345, 900_000), data["read"])
            self.assertIn((12_345, 450_000), data["write"])
            self.assertEqual(data["read"][0], (0, 100))
            self.assertEqual(data["read"][-1], (100_002, 100))
            self.assertEqual(path.stat().st_size, original_size)
            envelope = report.StreamingEnvelope()
            for index in range(20_000):
                envelope.add(index, index)
                self.assertLess(len(envelope.buckets), envelope.MAX_BUCKETS)

    @unittest.skipUnless(importlib.util.find_spec("matplotlib"), "matplotlib not installed")
    def test_embedded_pngs(self):
        manifest = fixture()
        for point in manifest["points"][1:]:
            point["history"] = []
        with tempfile.TemporaryDirectory() as directory:
            report.render_report(directory, manifest, plots=True)
            html = Path(directory, "report.html").read_text()
            self.assertIn("data:image/png;base64,", html)
            self.assertNotIn("PNG plots unavailable", html)


if __name__ == "__main__":
    unittest.main()
