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


def comparison_fixture():
    preparations = []
    points = []
    schedule = []
    for trial in (1, 2, 3):
        for profile in ("fresh", "medium"):
            prep_id = f"t{trial}_{profile}"
            preparation = dict(id=prep_id, trial=trial, profile=profile, status="complete",
                               qualified=not (trial == 3 and profile == "medium"),
                               required_write_bytes=1000, exposure_write_bytes=1000,
                               headroom_s=50, conditioning_status="complete", recovery_status="complete",
                               host_write_bytes=2000, conditioning={"range_over_mean": 0.1})
            preparations.append(preparation)
            schedule.append(dict(id=prep_id, kind="preparation"))
            rate = trial * 100 if profile == "fresh" else (50, 200, 600)[trial - 1]
            metrics = dict(total_iops=rate, total_bw_bytes=rate * 4096,
                           read=dict(iops=rate, bw_bytes=rate * 4096, io_count=rate * 20,
                                     clat_mean_ns=trial * 700, clat_max_ns=trial * 10000,
                                     maximum_observed_clat_ns=trial * 10000,
                                     clat_max_approximate=False, lat_mean_ns=trial * 750,
                                     percentiles_ns={"50": trial * 500, "90": trial * 800,
                                                     "95": trial * 900, "99": trial * 1000,
                                                     "99.9": trial * 1200},
                                     pooled_percentiles_ns={"99": trial * 9000}),
                           write=dict(iops=0, bw_bytes=0, io_count=0, percentiles_ns={"99": None}))
            point = dict(id=f"{prep_id}_rr4k_qd1", trial=trial, profile=profile,
                         preparation_id=prep_id, anchor="rr4k_qd1", kind="anchor",
                         pattern="random", bs=4096, qd=1, read_pct=100,
                         status="complete", relative_dir=f"trials/{prep_id}/rr4k_qd1",
                         run_count=1, prior_write_bytes=100 + trial, prior_write_accounting="target_kernel_counter",
                         runs=[dict(index=1, status="complete", metrics=metrics, ramp_write_bytes=7,
                                    host_write_bytes=9, relative_dir=f"trials/{prep_id}/rr4k_qd1/run1")],
                         metrics=metrics)
            points.append(point)
            schedule.append(dict(id=point["id"], kind="observation"))
    reference = copy.deepcopy(points[0])
    reference.update(id="t1_fresh_reference_rr4k_qd1", kind="reference", relative_dir="refs/t1_fresh",
                     metrics=copy.deepcopy(points[0]["metrics"]))
    reference["metrics"]["total_iops"] = 80
    reference["metrics"]["read"]["iops"] = 80
    schedule.append(dict(id=reference["id"], kind="reference"))
    return dict(schema_version=3, status="complete", config=dict(protocol="comparison", trials=3,
                profiles=["fresh", "medium"], prefix="<drive>"), preparation=preparations,
                points=points, references=[reference], schedule=schedule)


class ReportTests(unittest.TestCase):
    def test_schema3_independent_preparations_and_paired_ratios(self):
        manifest = comparison_fixture()
        summary = report.comparison_summary(manifest)
        fresh = next(row for row in summary["cross_trial"] if row["kind"] == "primary"
                     and row["profile"] == "fresh" and row["metric"] == "total_iops")
        medium = next(row for row in summary["cross_trial"] if row["kind"] == "primary"
                      and row["profile"] == "medium" and row["metric"] == "total_iops")
        self.assertEqual((fresh["qualified_trials"], fresh["median"], fresh["minimum"], fresh["maximum"]),
                         (3, 200, 100, 300))
        run_p99 = next(row for row in summary["cross_trial"] if row["kind"] == "primary"
                       and row["profile"] == "fresh" and row["metric"] == "read_p99_ns")
        self.assertEqual(run_p99["median"], 2000)
        self.assertEqual((medium["qualified_trials"], medium["independent_preparations"],
                          medium["available_preparations"], medium["local_repeats"], medium["median"]),
                         (2, 2, 3, 2, 125))
        ratio = next(row for row in summary["paired_ratios"] if row["kind"] == "primary"
                     and row["profile"] == "medium" and row["metric"] == "total_iops")
        self.assertEqual((ratio["paired_trials"], ratio["minimum"], ratio["median"], ratio["maximum"]),
                         (2, 0.5, 0.75, 1))
        self.assertEqual([row["trial"] for row in ratio["trial_ratios"]], [1, 2])
        self.assertAlmostEqual(next(row for row in summary["drift"] if row["metric"] == "read_iops")["change_pct"], -20)
        self.assertFalse(next(row for row in summary["within_trial"] if row["trial"] == 3
                              and row["profile"] == "medium" and row["metric"] == "total_iops")["included"])

    def test_schema3_artifacts_show_qualification_order_ramp_and_percentile_scope(self):
        manifest = comparison_fixture()
        manifest["points"][0]["metrics"]["workers"] = {"worker_0": dict(cpu_user=11, cpu_system=2,
                                                              depth={"1": 99}, depth_scope="per_job",
                                                              read={"histogram_ns": {"99999": 5}})}
        with tempfile.TemporaryDirectory() as directory:
            paths = report.render_report(directory, manifest, plots=False)
            html = Path(paths["report.html"]).read_text()
            text = Path(paths["result.txt"]).read_text()
            with open(paths["results.csv"], newline="") as handle:
                observations = list(csv.DictReader(handle))
            with open(paths["raw-runs.csv"], newline="") as handle:
                runs = list(csv.DictReader(handle))
            with open(paths["cross-trial.csv"], newline="") as handle:
                cross = list(csv.DictReader(handle))
            self.assertIn("&lt;drive&gt;", html)
            self.assertIn('href="#prep-0"', html)
            self.assertIn("Prior write bytes incl ramps", html)
            self.assertIn("Run p99 values are separate from combined histogram p99", html)
            self.assertIn("Scheduled order:", text)
            self.assertIn("Paired profile/fresh ratios", text)
            self.assertEqual(observations[0]["read_run_p99_ns"], "1000.0")
            self.assertEqual(observations[0]["read_combined_histogram_p99_ns"], "9000.0")
            self.assertEqual(observations[0]["prior_write_bytes"], "101")
            self.assertEqual(observations[0]["ramp_write_bytes"], "7")
            self.assertIn('"cpu_user": 11', observations[0]["workers"])
            self.assertNotIn("99999", observations[0]["workers"])
            self.assertEqual(runs[0]["ramp_write_bytes"], "7")
            self.assertTrue(any(row["profile"] == "medium" and row["qualified_trials"] == "2"
                                for row in cross))

    def test_schema3_qd1_latency_uses_independent_trials_and_separates_references(self):
        manifest = comparison_fixture()
        first = manifest["points"][0]
        extra = copy.deepcopy(first["runs"][0])
        extra["index"] = 2
        extra["metrics"]["read"]["clat_max_ns"] = 70000
        first["runs"].append(extra)
        first["run_count"] = 2
        manifest["references"][0]["metrics"]["read"]["percentiles_ns"]["99"] = 90000
        manifest["references"][0]["runs"][0]["metrics"]["read"]["percentiles_ns"]["99"] = 90000
        rows = report.qd1_latency_summary(manifest)
        fresh = next(row for row in rows["primary"] if row["profile"] == "fresh")
        medium = next(row for row in rows["primary"] if row["profile"] == "medium")
        reference = next(row for row in rows["reference"] if row["profile"] == "fresh")
        self.assertEqual((fresh["qualified_trials"], fresh["local_repeats"]), (3, 4))
        self.assertEqual(fresh["99"], "2.00 [1.00, 3.00] (n=3)")
        self.assertEqual(fresh["clat_mean_ns"], "1.40 [0.70, 2.10] (n=3)")
        self.assertEqual(fresh["worst_observed_clat"], "70.00 (exact; 4 recorded maxima)")
        self.assertEqual((medium["qualified_trials"], medium["99"]),
                         (2, "1.50 [1.00, 2.00] (n=2)"))
        self.assertEqual((reference["qualified_trials"], reference["99"]),
                         (1, "90.00 [90.00, 90.00] (n=1)"))
        with tempfile.TemporaryDirectory() as directory:
            paths = report.render_report(directory, manifest, plots=False)
            html = Path(paths["report.html"]).read_text()
            self.assertLess(html.index("4 KiB random-read QD1 latency"),
                            html.index("Preparation qualification and exposure"))
            self.assertIn("Final reference", html)
            self.assertIn("Write QD1 was not measured in this run; write-QD64 is not a substitute", html)
            with open(paths["results.csv"], newline="") as handle:
                self.assertFalse(any(row["anchor"] == "wr4k_qd1" for row in csv.DictReader(handle)))
            self.assertIn("70% reads and 30% writes", html)
            self.assertIn("medians of total and directional IOPS across repeats need not add up", html)
            text = Path(paths["result.txt"]).read_text()
            self.assertIn("Primary QD1 read latency", text)
            self.assertIn("Final-reference QD1 read latency", text)
        first["runs"][0]["metrics"]["read"].pop("clat_max_approximate")
        unknown = next(row for row in report.qd1_latency_summary(manifest)["primary"]
                       if row["profile"] == "fresh")
        self.assertIn("exactness unknown", unknown["worst_observed_clat"])
        manifest["config"]["run_type"] = "smoke"
        self.assertTrue(all(row["qualified_trials"] == 0 and row["99"] == "N/A"
                            for group in report.qd1_latency_summary(manifest).values() for row in group))

    def test_schema3_write_qd1_has_its_own_qualified_latency_table(self):
        manifest = comparison_fixture()
        for source in list(manifest["points"]):
            point = copy.deepcopy(source)
            point.update(id=source["id"].replace("rr4k_qd1", "wr4k_qd1"), anchor="wr4k_qd1",
                         read_pct=0, relative_dir=source["relative_dir"].replace("rr4k_qd1", "wr4k_qd1"))
            trial = source["trial"]
            point["metrics"]["read"] = dict(iops=0, bw_bytes=0, io_count=0,
                                             percentiles_ns={"99": None})
            point["metrics"]["write"] = dict(iops=source["metrics"]["total_iops"],
                                              bw_bytes=source["metrics"]["total_bw_bytes"],
                                              io_count=source["metrics"]["read"]["io_count"],
                                              clat_mean_ns=trial * 2000,
                                              clat_max_ns=trial * 20000,
                                              maximum_observed_clat_ns=trial * 20000,
                                              clat_max_approximate=False,
                                              lat_mean_ns=trial * 2100,
                                              percentiles_ns={"50": trial * 1000,
                                                              "90": trial * 3000,
                                                              "95": trial * 4000,
                                                              "99": trial * 5000,
                                                              "99.9": trial * 7000})
            manifest["points"].append(point)
            manifest["schedule"].append(dict(id=point["id"], kind="observation"))
        rows = report.qd1_latency_summary(manifest, "write")["primary"]
        fresh = next(row for row in rows if row["profile"] == "fresh")
        medium = next(row for row in rows if row["profile"] == "medium")
        self.assertEqual((fresh["qualified_trials"], fresh["99"], fresh["worst_observed_clat"]),
                         (3, "10.00 [5.00, 15.00] (n=3)", "60.00 (exact; 3 recorded maxima)"))
        self.assertEqual((medium["qualified_trials"], medium["99"]),
                         (2, "7.50 [5.00, 10.00] (n=2)"))
        with tempfile.TemporaryDirectory() as directory:
            paths = report.render_report(directory, manifest, plots=False)
            html = Path(paths["report.html"]).read_text()
            self.assertIn("4 KiB random-read QD1 latency", html)
            self.assertIn("4 KiB random-write QD1 latency", html)
            self.assertIn("Qualified trial measurements are shown below", html)
            self.assertNotIn("Write QD1 was not measured in this run", html)
            with open(paths["results.csv"], newline="") as handle:
                exported = next(row for row in csv.DictReader(handle)
                                if row["id"] == "t1_fresh_wr4k_qd1")
            self.assertEqual(exported["write_clat_mean_ns"], "2000")
            self.assertEqual(exported["write_clat_p99.9_ns"], "7000.0")
            self.assertEqual(exported["write_lat_mean_ns"], "2100")
            self.assertIn("Primary QD1 write latency", Path(paths["result.txt"]).read_text())
        manifest["config"]["run_type"] = "smoke"
        self.assertTrue(all(row["qualified_trials"] == 0 for row in
                            report.qd1_latency_summary(manifest, "write")["primary"]))

    def test_schema3_latency_csv_and_directional_detail_restore_all_metrics(self):
        manifest = comparison_fixture()
        mixed = copy.deepcopy(manifest["points"][0])
        mixed.update(id="t1_fresh_rw4k_qd32", anchor="rw4k_qd32", qd=32, read_pct=70)
        mixed["metrics"]["write"] = dict(clat_mean_ns=2500, clat_max_ns=9000,
                                          maximum_observed_clat_ns=9000,
                                          clat_max_approximate=True, lat_mean_ns=2800,
                                          percentiles_ns={"50": 2000, "90": 3000, "95": 4000,
                                                          "99": 5000, "99.9": 7000})
        manifest["points"].append(mixed)
        manifest["schedule"].append(dict(id=mixed["id"], kind="observation"))
        with tempfile.TemporaryDirectory() as directory:
            paths = report.render_report(directory, manifest, plots=False)
            with open(paths["results.csv"], newline="") as handle:
                rows = {row["id"]: row for row in csv.DictReader(handle)}
            with open(paths["raw-runs.csv"], newline="") as handle:
                raw = next(row for row in csv.DictReader(handle) if row["id"] == mixed["id"])
            read = rows["t1_fresh_rr4k_qd1"]
            self.assertEqual([read[key] for key in ("read_clat_mean_ns", "read_lat_mean_ns",
                                                   "read_clat_p50_ns", "read_clat_p90_ns",
                                                   "read_clat_p95_ns", "read_clat_p99_ns",
                                                   "read_clat_p99.9_ns", "read_clat_max_ns")],
                             ["700", "750", "500.0", "800.0", "900.0", "1000.0", "1200.0", "10000"])
            self.assertEqual(read["read_run_p99_ns"], "1000.0")
            self.assertEqual(read["read_combined_histogram_p99_ns"], "9000.0")
            self.assertEqual(read["read_pooled_clat_p99_ns"], "9000.0")
            self.assertEqual(rows[mixed["id"]]["write_clat_p99.9_ns"], "7000.0")
            self.assertEqual(rows[mixed["id"]]["write_clat_max_approximate"], "True")
            self.assertEqual(raw["write_lat_mean_ns"], "2800")
            html = Path(paths["report.html"]).read_text()
            self.assertIn("<summary>Directional latency for every observation</summary>", html)
            self.assertIn("Total latency mean µs", html)
            self.assertIn("<td>write</td>", html)
            self.assertIn("<td>7.00</td>", html)
            self.assertIn("Directional latency detail (microseconds", Path(paths["result.txt"]).read_text())

    def test_schema3_partial_report_keeps_missing_groups_visible(self):
        manifest = comparison_fixture()
        manifest["points"] = [manifest["points"][0], manifest["points"][1]]
        manifest["points"][1]["status"] = "failed"
        manifest["points"][1]["metrics"] = {}
        manifest["points"][1]["runs"] = []
        manifest["references"] = []
        summary = report.comparison_summary(manifest)
        missing = next(row for row in summary["cross_trial"] if row["profile"] == "medium"
                       and row["metric"] == "total_iops")
        self.assertEqual(missing["qualified_trials"], 0)
        self.assertIsNone(missing["median"])
        paired = next(row for row in summary["paired_ratios"] if row["profile"] == "medium"
                      and row["metric"] == "total_iops")
        self.assertEqual(paired["paired_trials"], 0)
        with tempfile.TemporaryDirectory() as directory:
            paths = report.render_report(directory, manifest, plots=False)
            self.assertIn("failed", Path(paths["result.txt"]).read_text())
            self.assertTrue(all(Path(path).exists() for path in paths.values()))

    def test_schema3_smoke_keeps_raw_observations_without_qualified_comparisons(self):
        manifest = comparison_fixture()
        manifest["config"]["run_type"] = "smoke"
        for prep in manifest["preparation"]:
            prep["qualified"] = True
        summary = report.comparison_summary(manifest)
        self.assertTrue(all(not row["included"] for row in summary["within_trial"]))
        self.assertTrue(any(row["value"] is not None for row in summary["within_trial"]))
        self.assertTrue(all(row["qualified_trials"] == 0 and row["median"] is None
                            for row in summary["cross_trial"]))
        self.assertTrue(all(row["paired_trials"] == 0 and row["median"] is None
                            for row in summary["paired_ratios"]))
        self.assertTrue(all(not row["qualified"] and row["change_pct"] is None
                            for row in summary["drift"]))
        with tempfile.TemporaryDirectory() as directory:
            paths = report.render_report(directory, manifest, plots=False)
            self.assertIn("Smoke measurements are exploratory workflow observations",
                          Path(paths["report.html"]).read_text())
            with open(paths["results.csv"], newline="") as handle:
                self.assertEqual(next(csv.DictReader(handle))["total_iops"], "100.0")
            with open(paths["cross-trial.csv"], newline="") as handle:
                self.assertTrue(all(row["qualified_trials"] == "0" for row in csv.DictReader(handle)))

    def test_schema3_sweep_drift_matches_stage_trial_and_preparation(self):
        manifest = fixture()
        manifest.update(schema_version=3, config={**manifest["config"], "protocol": "sweep"})
        manifest["references"] = []
        for trial, prep_id, early, late in ((1, "t01_fresh", 100000, 80000),
                                           (2, "t02_fresh", 200000, 100000)):
            for stage, rate in (("early", early), ("late", late)):
                manifest["references"].append(dict(
                    id=f"{prep_id}_reference_{stage}", trial=trial,
                    preparation_id=prep_id, profile="fresh", stage=stage,
                    status="complete", pattern="random", bs=4096, qd=1, read_pct=100,
                    metrics={"read": {"iops": rate}}))
        with tempfile.TemporaryDirectory() as directory:
            text = Path(report.render_report(directory, manifest, plots=False)["result.txt"]).read_text()
            self.assertIn("trial 1 t01_fresh fresh reference read IOPS drift: -20.00%", text)
            self.assertIn("trial 2 t02_fresh fresh reference read IOPS drift: -50.00%", text)

    def test_schema2_sweep_drift_retains_exact_legacy_reference_ids(self):
        manifest = fixture()
        manifest["schema_version"] = 2
        manifest["references"] = [dict(id=f"reference_{stage}", profile="fresh",
                                       status="complete", pattern="random", bs=4096,
                                       qd=1, read_pct=100, metrics={"read": {"iops": rate}})
                                  for stage, rate in (("early", 100000), ("late", 80000))]
        with tempfile.TemporaryDirectory() as directory:
            text = Path(report.render_report(directory, manifest, plots=False)["result.txt"]).read_text()
            self.assertIn("fresh reference read IOPS drift: -20.00%", text)

    def test_schema3_aggregate_cli_dispatch(self):
        manifest = comparison_fixture()
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "manifest.json").write_text(json.dumps(manifest))
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("aggregate.py")), directory],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("independent preparations qualified", result.stdout)
            self.assertTrue(Path(directory, "paired-ratios.csv").exists())

    def test_worker_logs_sum_aligned_intervals_before_downsampling(self):
        point = {"relative_dir": "t1/anchor", "log_intervals":
                 {"mode": "averaged_contiguous", "average_ms": 1000, "origin_ms": 0,
                  "time_reference": "post_ramp_measurement_epoch", "ramp_time_ms": 5000}}
        with tempfile.TemporaryDirectory() as directory:
            location = Path(directory, point["relative_dir"])
            location.mkdir(parents=True)
            (location / "series_iops.1.log").write_text("1000, 100, 0, 4096\n2000, 200, 0, 4096\n")
            (location / "series_iops.2.log").write_text("1000, 300, 0, 4096\n2000, 400, 0, 4096\n")
            series = report.fio_series(directory, point, "iops")
            self.assertEqual(series["read"], [(1, 400), (2, 600)])
            (location / "series_iops.1.log").write_text("1000, 100, 1, 4096\n1000, 200, 0, 4096\n")
            (location / "series_iops.2.log").write_text("1000, 300, 0, 4096\n1000, 400, 1, 4096\n")
            mixed = report.fio_series(directory, point, "iops")
            self.assertEqual(mixed["read"], [(1, 500)])
            self.assertEqual(mixed["write"], [(1, 500)])

    def test_worker_logs_rebin_jitter_and_staggered_starts(self):
        point = {"relative_dir": "logs", "log_intervals":
                 {"mode": "averaged_contiguous", "average_ms": 1000, "origin_ms": 0,
                  "time_reference": "post_ramp_measurement_epoch", "ramp_time_ms": 5000}}
        with tempfile.TemporaryDirectory() as directory:
            location = Path(directory, "logs")
            location.mkdir()
            first = location / "series_iops.1.log"
            second = location / "series_iops.2.log"
            first.write_text("1000, 100, 0\n2000, 200, 0\n3000, 300, 0\n")
            second.write_text("1001, 300, 0\n2001, 400, 0\n3001, 500, 0\n")
            jitter = report.fio_series(directory, point, "iops")["read"]
            self.assertAlmostEqual(dict(jitter)[1], 400)
            self.assertAlmostEqual(dict(jitter)[2], 599.9)
            second.write_text("1500, 300, 0\n2500, 400, 0\n3500, 500, 0\n")
            stagger = report.fio_series(directory, point, "iops")["read"]
            self.assertEqual(stagger, [(1, 400), (2, 550), (3, 750)])
            self.assertTrue(stagger.incomplete_coverage)

    def test_worker_logs_partial_final_interval_and_missing_worker_coverage(self):
        point = {"relative_dir": "logs", "log_intervals":
                 {"mode": "averaged_contiguous", "average_ms": 1000, "origin_ms": 0,
                  "time_reference": "post_ramp_measurement_epoch", "ramp_time_ms": 5000}}
        with tempfile.TemporaryDirectory() as directory:
            location = Path(directory, "logs")
            location.mkdir()
            first = location / "series_iops.1.log"
            second = location / "series_iops.2.log"
            first.write_text("1000, 100, 0\n2000, 200, 0\n2500, 300, 0\n")
            second.write_text("1000, 300, 0\n2000, 400, 0\n3000, 500, 0\n")
            partial_series = report.fio_series(directory, point, "iops")["read"]
            self.assertEqual(dict(partial_series)[2.5], 800)
            self.assertNotIn(3, dict(partial_series))
            self.assertTrue(partial_series.incomplete_coverage)
            first.write_text("1000, 100, 0\n2000, 100, 0\n4000, 100, 0\n5000, 100, 0\n")
            second.write_text("1000, 300, 0\n2000, 300, 0\n3000, 300, 0\n4000, 300, 0\n5000, 300, 0\n")
            uneven = report.fio_series(directory, point, "iops")["read"]
            self.assertEqual(uneven, [(1, 400), (2, 400), (3, 400), (4, 400), (5, 400)])
            self.assertFalse(uneven.incomplete_coverage)

    def test_worker_logs_explicit_origin_and_unknown_coverage(self):
        point = {"relative_dir": "logs", "worker_layout": [{"name": "worker_00"}, {"name": "worker_01"}],
                 "log_intervals": {"mode": "averaged_contiguous", "average_ms": 1000,
                                   "origin_ms": 5000, "time_reference": "job_epoch",
                                   "ramp_time_ms": 5000, "directions": ["read"]}}
        with tempfile.TemporaryDirectory() as directory:
            location = Path(directory, "logs")
            location.mkdir()
            first = location / "series_iops_worker_00.1.log"
            second = location / "series_iops_worker_01.2.log"
            first.write_text("6000, 100, 0\n7000, 100, 0\n")
            second.write_text("6000, 300, 0\n7000, 300, 0\n")
            known = report.fio_series(directory, point, "iops")["read"]
            self.assertEqual(known, [(6, 400), (7, 400)])
            second.unlink()
            unknown = report.fio_series(directory, point, "iops")["read"]
            self.assertEqual(unknown, [])
            self.assertTrue(unknown.incomplete_coverage)
            point.pop("log_intervals")
            legacy = report.fio_series(directory, point, "iops")["read"]
            self.assertEqual(legacy, [(6, 100), (7, 100)])
            second.write_text("0, 300, 0\n6001, 300, 0\n7001, 300, 0\n")
            unknown_points = report.fio_series(directory, point, "iops")["read"]
            self.assertEqual(unknown_points, [(0, 300), (6, 100), (6.001, 300),
                                              (7, 100), (7.001, 300)])
            self.assertTrue(unknown_points.incomplete_coverage)

    def test_worker_log_glob_matches_fio_worker_suffixes(self):
        point = {"relative_dir": "t1/anchor/run_01"}
        with tempfile.TemporaryDirectory() as directory:
            location = Path(directory, point["relative_dir"])
            location.mkdir(parents=True)
            (location / "series_bw_worker_00_bw.1.log").write_text("1000, 1024, 0, 4096\n")
            (location / "series_bw_worker_01_bw.2.log").write_text("1000, 2048, 0, 4096\n")
            self.assertEqual(report.fio_series(directory, point, "bw")["read"], [(1, 3 * 1024**2)])

    @unittest.skipUnless(importlib.util.find_spec("matplotlib"), "matplotlib not installed")
    def test_schema3_embeds_representative_worker_throughput_plot(self):
        manifest = comparison_fixture()
        point = manifest["points"][0]
        with tempfile.TemporaryDirectory() as directory:
            location = Path(directory, point["runs"][0]["relative_dir"])
            location.mkdir(parents=True)
            (location / "series_iops_worker_00_iops.1.log").write_text("1000, 100, 0, 4096\n")
            (location / "series_iops_worker_01_iops.2.log").write_text("1000, 200, 0, 4096\n")
            (location / "series_bw_worker_00_bw.1.log").write_text("1000, 1024, 0, 4096\n")
            paths = report.render_report(directory, manifest, plots=True)
            html = Path(paths["report.html"]).read_text()
            self.assertIn("data:image/png;base64,", html)
            self.assertIn("Worker intervals are aligned and summed", html)
            self.assertIn("trial 1 fresh rr4k_qd1 / run 1", html)
            self.assertIn("<details><summary>trial 1 fresh rr4k_qd1 / run 1</summary>", html)
            self.assertIn('alt="trial 1 fresh rr4k_qd1 / run 1"', html)

    def test_schema3_plot_failure_keeps_report_artifacts(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(report, "comparison_plots", side_effect=ImportError("matplotlib")):
            paths = report.render_report(directory, comparison_fixture(), plots=True)
            self.assertIn("PNG plots unavailable", Path(paths["report.html"]).read_text())
            self.assertTrue(all(Path(path).exists() for path in paths.values()))

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
