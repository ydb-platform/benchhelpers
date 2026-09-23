"""Orchestration integration fixtures; subprocess/device access is intercepted."""
import contextlib
import csv
import io
import json
import os
from pathlib import Path
import signal
import shutil
import stat
import statistics
import tempfile
import time
import types
import unittest
from unittest import mock

import nvme_bench as bench


TARGET = '/dev/nvme999n1p2'
DEVICE = dict(path=TARGET, namespace='/dev/nvme999n1', controller='/dev/nvme999',
              partition=True, size_bytes=100 * bench.ALIGN,
              namespace_size_bytes=200 * bench.ALIGN, cpu=0, poll_cpu=2,
              major_minor='259:99', serial='MOCK-NO-DEVICE', model='Mock NVMe', start_bytes=bench.ALIGN)


def counters(read_ios, write_ios, seconds, bs=4096):
    result = dict(error=0, job_runtime=int(seconds * 1000), usr_cpu=10, sys_cpu=5,
                  iodepth_level={'1': 100})
    for direction, count in [('read', read_ios), ('write', write_ios)]:
        result[direction] = dict(io_bytes=count * bs, total_ios=count,
                                 runtime=int(seconds * 1000) if count else 0,
                                 clat_ns=dict(N=count, mean=1000 if count else 0,
                                              max=1000 if count else 0,
                                              bins={'1000': count} if count else {}),
                                 lat_ns=dict(N=count, mean=1100 if count else 0))
    return result


class SimulatedRunner(bench.Runner):
    """Exercise orchestration/preparation/measurement with synthetic fio counters."""
    instances = []
    failure = None
    fill_seconds = 1
    clock = None

    def __init__(self, *args):
        super().__init__(*args)
        self.calls = []
        self.saves = []
        self.sample_values = [1000, 9000, 2000, 4000, 3000]
        self.sample_numbers = {}
        self.instances.append(self)

    def save(self, report=False, plots=False):
        self.saves.append((report, plots))
        if report:
            (self.directory / 'result.txt').write_text('Mock report\n')

    def telemetry(self, force=False):
        pass

    def fio(self, args, directory, on_snapshot=None, deadline=None):
        self.calls.append((args, directory, deadline))
        limits = [limit for limit in (deadline, self.run_deadline) if limit is not None]
        deadline = min(limits) if limits else None
        options = dict(arg[2:].split('=', 1) for arg in args if '=' in arg)
        directory = Path(directory)
        case = next((p for p in self.manifest['points'] if p['id'] in directory.parts), None)
        if case and self.failure:
            raise self.failure
        if 'time_based' not in options and 'io_size' not in options and options.get('rw') == 'write':
            seconds = self.fill_seconds
            if deadline is not None and self.clock.value + seconds > deadline:
                self.clock.advance(max(0, deadline - self.clock.value))
                raise bench.BudgetExpired('preparation deadline expired during fill')
            self.clock.advance(seconds)
            result = counters(0, int(options['size']) // 4096, seconds)
            if on_snapshot:
                on_snapshot(result)
            return result
        rw = options.get('rw', 'read')
        ratio = int(options.get('rwmixread', 100 if rw in ('read', 'randread') else 0))
        bs = options.get('bs', '4096')
        bs = int(bs[:-1]) * {'K': 1024, 'M': 1024 ** 2}[bs[-1]] if bs[-1] in 'KM' else int(bs)
        seconds_limit = float(options.get('runtime', '1s').rstrip('s'))
        interval = float(options.get('status-interval', seconds_limit))
        self.clock.advance(float(options.get('ramp_time', options.get('ramp-time', '0s')).rstrip('s')))
        if case:
            number = self.sample_numbers.get(case['id'], 0)
            rate = self.sample_values[number % len(self.sample_values)]
            self.sample_numbers[case['id']] = number + 1
        else:
            rate = 100000
        volume_limit = int(options['io_size']) if 'io_size' in options else None
        if volume_limit is not None:
            seconds_limit = min(seconds_limit, volume_limit / (bs * rate))
        if deadline is not None:
            seconds_limit = min(seconds_limit, max(0, deadline - self.clock.value))
        if seconds_limit <= 0:
            raise bench.BudgetExpired('fio deadline expired')
        elapsed = min(interval, seconds_limit)
        previous = 0
        while True:
            self.clock.advance(elapsed - previous)
            previous = elapsed
            nios = round(elapsed * rate)
            reads = nios * ratio // 100
            result = counters(reads, nios - reads, elapsed, bs)
            self.last_job = result
            if on_snapshot and on_snapshot(result):
                return result
            if elapsed >= seconds_limit:
                if deadline is not None and self.clock.value >= deadline:
                    raise bench.BudgetExpired('fio deadline expired')
                return result
            elapsed = min(elapsed + interval, seconds_limit)
            if elapsed > 4000:
                raise AssertionError('mock workload did not converge')


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def advance(self, seconds):
        self.value += seconds


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='nvme-bench-test-')
        self.directory = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        SimulatedRunner.instances = []
        SimulatedRunner.failure = None
        SimulatedRunner.fill_seconds = 1
        self.clock = FakeClock()
        SimulatedRunner.clock = self.clock
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.stack.enter_context(mock.patch.object(bench.signal, 'signal'))
        self.stack.enter_context(mock.patch.object(bench.shutil, 'which', return_value='/mock/tool'))
        self.stack.enter_context(mock.patch.object(bench, 'inspect_device', return_value=DEVICE.copy()))
        self.stack.enter_context(mock.patch.object(bench.time, 'sleep', side_effect=self.clock.advance))
        self.stack.enter_context(mock.patch.object(bench.time, 'monotonic', side_effect=lambda: self.clock.value))
        self.command = self.stack.enter_context(mock.patch.object(
            bench, 'command', return_value=types.SimpleNamespace(returncode=0, stdout='', stderr='')))
        original_stat = Path.stat

        def fake_stat(path, *args, **kwargs):
            if str(path) == TARGET:
                return types.SimpleNamespace(st_rdev=os.makedev(259, 99))
            return original_stat(path, *args, **kwargs)

        self.stack.enter_context(mock.patch.object(Path, 'stat', fake_stat))
        # Any accidental raw subprocess launch fails before touching storage.
        self.stack.enter_context(mock.patch.object(bench.subprocess, 'Popen', side_effect=AssertionError('unmocked subprocess')))
        self.stack.enter_context(mock.patch.object(bench.subprocess, 'run', side_effect=AssertionError('unmocked command')))

    def args(self, *extra):
        return bench.parse_args(['--filename', TARGET, '--allow-partition', '--yes',
                                 '--results-dir', str(self.directory), *extra])

    def test_balanced_default_and_optional_full_matrix_have_unique_cases(self):
        points = bench.build_points(self.args())
        self.assertEqual(len(points), 144)
        self.assertEqual(len({(p['profile'], p['id']) for p in points}), 144)
        for profile in bench.PROFILES:
            selected = [p for p in points if p['profile'] == profile]
            self.assertEqual(len(selected), 48)
            random = [p for p in selected if p['pattern'] == 'random']
            self.assertEqual(len(random), 40)
            self.assertEqual({p['read_pct'] for p in random}, set(bench.BALANCED_RATIOS))
            self.assertEqual(sum(p['read_pct'] == 100 for p in selected), 12)
            first_write = next(i for i, p in enumerate(selected) if p['read_pct'] < 100)
            self.assertEqual(first_write, 12)
            self.assertEqual([p['read_pct'] for p in selected], sorted((p['read_pct'] for p in selected), reverse=True))
        full = bench.build_points(self.args('--matrix', 'full'))
        self.assertEqual(len(full), 294)
        self.assertEqual(len({(p['profile'], p['id']) for p in full}), 294)

    def test_deprecated_alias_conflicts_include_equals_syntax(self):
        for extra in (['--runtime=1s', '--measurement-time=2s'],
                      ['--clean-device', '--profiles=fresh']):
            with self.subTest(extra=extra), self.assertRaises(SystemExit) as raised:
                self.args(*extra)
            self.assertEqual(raised.exception.code, 2)

    def test_zero_rounded_active_range_is_rejected(self):
        args = self.args('--run-type', 'smoke', '--size-percent', '1')
        runner = bench.Runner(args, DEVICE.copy(), self.directory)
        with self.assertRaisesRegex(RuntimeError, 'below 1 MiB'):
            runner.size('medium')

    def test_ranges_are_80_percent_for_every_default_profile(self):
        runner = bench.Runner(self.args(), DEVICE.copy(), self.directory)
        for profile in bench.PROFILES:
            self.assertEqual(runner.size(profile), 80 * bench.ALIGN)
            self.assertEqual(runner.occupied_size(profile), 80 * bench.ALIGN)
        smaller = bench.Runner(self.args('--size-percent', '40'), DEVICE.copy(), self.directory)
        for profile in bench.PROFILES:
            self.assertEqual(smaller.size(profile), 40 * bench.ALIGN)
            self.assertEqual(smaller.occupied_size(profile), (40 if profile == 'fresh' else 80) * bench.ALIGN)
        with self.assertRaises(SystemExit):
            self.args('--size-percent', '90', '--occupancy-percent', '80')

    def test_default_write_budget_has_once_per_profile_fill_and_no_workload_floor(self):
        runner = bench.Runner(self.args(), DEVICE.copy(), self.directory)
        budget = runner.minimum_writes()
        expected_prep = sum(runner.occupied_size(profile) for profile in bench.PROFILES)
        self.assertEqual(budget['preparation_bytes'], expected_prep)
        self.assertEqual(budget['sustained_write_floor_bytes'], 0)
        self.assertEqual(budget['total_bytes'], expected_prep)

    def test_default_time_budget_is_hours_and_includes_five_samples(self):
        runner = bench.Runner(self.args(), DEVICE.copy(), self.directory)
        budget = runner.budget()
        self.assertEqual(budget['case_count'], 144)
        self.assertEqual(budget['sample_count'], 720)
        self.assertGreaterEqual(budget['measurement_work_s'], 144 * 5 * 13)
        self.assertEqual(budget['preparation_cap_s'], 30 * 60 + 30 * 60 + 60 * 60)
        self.assertGreaterEqual(budget['run_cap_s'], budget['preparation_cap_s'] + 720 * 15)
        self.assertLess(budget['run_cap_s'], 6 * 3600)
        explicit = bench.Runner(self.args('--max-run-time', '4h'), DEVICE.copy(), self.directory)
        self.assertEqual(explicit.budget()['run_cap_s'], 4 * 3600)

    def test_preparation_only_budget_has_no_measurement_or_reference_work(self):
        runner = bench.Runner(self.args('--profiles', 'medium', '--prepare-only'), DEVICE.copy(), self.directory)
        budget = runner.budget()
        self.assertEqual(budget['sample_count'], 0)
        self.assertEqual(budget['case_count'], 0)
        self.assertEqual(budget['measurement_work_s'], 0)
        self.assertEqual(budget['reference_work_s'], 0)
        self.assertEqual(budget['preparation_cap_s'], 1800)

    def test_all_presets_finish_with_bounded_nonconverging_conditioning(self):
        # Simulate slow initialization and conditioning that never stabilizes;
        # every case in all three profiles must still run within its budget.
        for matrix, run_type, cap in [('balanced', 'normal', 18390), ('full', 'normal', 29640),
                                      ('balanced', 'smoke', 744)]:
            with self.subTest(matrix=matrix, run_type=run_type):
                directory = self.directory / f'{matrix}_{run_type}'
                directory.mkdir()
                runner = SimulatedRunner(self.args('--matrix', matrix, '--run-type', run_type), DEVICE.copy(), directory)
                runner.fill_seconds = 1500 if run_type == 'normal' else 1
                began = self.clock.value
                with mock.patch('profile_metrics.check_stability', return_value={'stable': False, 'checks': {}}):
                    self.assertEqual(runner.run(), 0)
                self.assertEqual(runner.budget()['run_cap_s'], cap)
                self.assertLessEqual(self.clock.value - began, cap)
                self.assertEqual(len(runner.manifest['preparation']), 3)
                self.assertTrue(all(p['status'] == 'complete' for p in runner.manifest['points']))
                self.assertEqual(runner.manifest['preparation'][-1]['conditioning_status'], 'time_cap_reached')

    def test_expired_global_budget_prevents_new_fio_process(self):
        runner = bench.Runner(self.args(), DEVICE.copy(), self.directory)
        runner.run_deadline = self.clock.value - 1
        with mock.patch.object(bench.subprocess, 'Popen') as popen:
            with self.assertRaises(bench.BudgetExpired):
                runner.fio(['--name=never-start', '--runtime=1s'], self.directory)
        popen.assert_not_called()

    def test_write_accounting_includes_ramp_from_target_kernel_counter(self):
        runner = bench.Runner(self.args(), DEVICE.copy(), self.directory)
        with mock.patch.object(bench, 'read_text', return_value='0 0 0 0 0 0 30 0'):
            record = {}
            runner.record_writes(record, 10 * 512, 8192)
        self.assertEqual(record['measured_write_bytes'], 8192)
        self.assertEqual(record['host_write_bytes'], 20 * 512)
        self.assertEqual(record['host_write_accounting'], 'target_kernel_counter')

    def test_missing_or_reset_kernel_counter_marks_bytes_as_lower_bound(self):
        runner = bench.Runner(self.args(), DEVICE.copy(), self.directory)
        for before, after in ((None, None), (100, 99)):
            with self.subTest(before=before, after=after), mock.patch.object(runner, 'host_writes', return_value=after):
                record = {}
                runner.record_writes(record, before, 8192)
                self.assertEqual(record['host_write_bytes'], 8192)
                self.assertEqual(record['host_write_accounting'], 'measured_only_lower_bound')

    def test_old_profile_names_remain_aliases_and_ramp_can_be_zero(self):
        args = self.args('--profiles', 'used-recovered,sustained-steady', '--ramp-time', '0', '--run-count', '3')
        self.assertEqual(args.profiles, ['medium', 'sustained'])
        self.assertEqual(args.ramp_time, 0)
        self.assertEqual(args.run_count, 3)
        with self.assertRaises(SystemExit):
            self.args('--run-count', '0')

    def test_full_main_invocation_prepares_and_measures_all_profiles(self):
        with mock.patch.object(bench, 'inspect_device', return_value=DEVICE.copy()) as inspect, \
             mock.patch.object(bench, 'output_outside_target'), \
             mock.patch.object(bench.os, 'geteuid', return_value=0), \
             mock.patch.object(bench, 'NamespaceLock'), \
             mock.patch.object(bench, 'Runner', SimulatedRunner):
            self.assertEqual(bench.main(['--filename', TARGET, '--allow-partition', '--yes',
                                         '--results-dir', str(self.directory)]), 0)
        self.assertEqual(inspect.call_count, 5)
        runner = SimulatedRunner.instances[-1]
        self.assertEqual(runner.manifest['status'], 'complete')
        self.assertTrue(all(p['status'] == 'complete' for p in runner.manifest['points']))
        self.assertEqual(len(runner.manifest['preparation']), 3)
        self.assertEqual(runner.saves[-1], (True, True))
        discards = [call.args[0] for call in self.command.call_args_list if call.args[0][0] == 'blkdiscard']
        self.assertEqual(discards, [['blkdiscard', TARGET]] * 3)
        for profile in bench.PROFILES:
            prep = [r for r in runner.manifest['preparation'] if r['profile'] == profile]
            self.assertEqual(len(prep), 1)
            for record in prep:
                self.assertEqual(record['size_bytes'], 80 * bench.ALIGN)
                self.assertEqual(record['status'], 'complete')
            points = [p for p in runner.manifest['points'] if p['profile'] == profile]
            self.assertEqual(len({p['preparation'] for p in points}), 1)
            for point in points:
                self.assertAlmostEqual(point['metrics']['achieved_read_pct'], point['read_pct'])
                self.assertEqual(len(point['runs']), 5)
                self.assertNotIn('host_write_floor_bytes', point.get('stability', {}))
        self.assertEqual(sum(len(p['runs']) for p in runner.manifest['points']), 720)
        self.assertEqual(len(runner.manifest['references']), 6)
        self.assertTrue(all(p['status'] == 'complete' and len(p['runs']) == 1
                            for p in runner.manifest['references']))
        self.assertEqual(runner.budget()['reference_work_s'], 6 * 13)

    def test_read_only_points_record_shared_preparation_reference(self):
        runner = SimulatedRunner(self.args('--profiles', 'fresh'), DEVICE.copy(), self.directory)
        self.assertEqual(runner.run(), 0)
        reads = [p for p in runner.manifest['points'] if p['read_pct'] == 100]
        self.assertEqual({p.get('preparation') for p in reads}, {reads[0]['preparation']})

    def test_capability_failure_is_reported_without_discard(self):
        runner = SimulatedRunner(self.args('--profiles', 'fresh'), DEVICE.copy(), self.directory)
        with mock.patch.object(runner, 'fio', side_effect=RuntimeError('engine unsupported')):
            self.assertEqual(runner.run(capability_check=True), 1)
        self.assertEqual(runner.manifest['status'], 'failed')
        self.assertEqual(runner.manifest['capability_check']['status'], 'failed')
        self.assertEqual(runner.manifest['capability_check']['error'], 'engine unsupported')
        self.assertEqual(runner.manifest['preparation'], [])
        self.command.assert_not_called()
        self.assertEqual(runner.saves[-1], (True, True))

    def test_final_health_change_prevents_success(self):
        runner = SimulatedRunner(self.args('--profiles', 'fresh', '--run-type', 'smoke'), DEVICE.copy(), self.directory)

        def health(force=False):
            if len(runner.manifest['references']) == 2:
                self.assertTrue(force)
                raise RuntimeError('new NVMe health warning')

        with mock.patch.object(runner, 'telemetry', side_effect=health):
            self.assertEqual(runner.run(), 1)
        self.assertEqual(runner.manifest['status'], 'failed')
        self.assertTrue(all(p['status'] == 'complete' for p in runner.manifest['points']))
        self.assertIn('health warning', runner.manifest['error'])

    def test_reference_budget_failure_is_finalized(self):
        runner = SimulatedRunner(self.args('--profiles', 'fresh'), DEVICE.copy(), self.directory)

        def expired_reference(point):
            point['status'] = 'running'
            raise bench.BudgetExpired('reference budget exhausted')

        with mock.patch.object(runner, 'measure', side_effect=expired_reference):
            self.assertEqual(runner.run(), 1)
        self.assertEqual(runner.manifest['references'][0]['status'], 'timed_out')
        self.assertIn('reference budget', runner.manifest['references'][0]['error'])

    def test_report_refresh_once_per_case_but_manifest_preserves_every_repeat(self):
        runner = SimulatedRunner(self.args('--profiles', 'fresh'), DEVICE.copy(), self.directory)
        runner.measure(runner.manifest['points'][0])
        self.assertEqual(sum(report for report, _ in runner.saves), 1)
        self.assertEqual(len(runner.saves), 2 * runner.args.run_count + 1)

    def test_mixed_command_uses_single_shared_depth_and_ratio(self):
        runner = SimulatedRunner(self.args('--profiles', 'fresh', '--matrix', 'full'), DEVICE.copy(), self.directory)
        runner.start_deadline()
        point = next(p for p in runner.manifest['points'] if p['read_pct'] == 95 and p['qd'] == 32)
        runner.measure(point)
        args = runner.fio_base() + runner.calls[-1][0]
        self.assertIn('--numjobs=1', args)
        self.assertIn('--iodepth=32', args)
        self.assertIn('--rw=randrw', args)
        self.assertIn('--rwmixread=95', args)
        self.assertEqual(sum(x.startswith('--iodepth=') for x in args), 1)
        self.assertIn('--norandommap=1', args)
        self.assertIn('--output-format=json+', args)
        self.assertNotIn('--hipri=1', args)
        self.assertIn('--runtime=10s', args)
        self.assertIn('--ramp_time=3s', args)
        self.assertEqual(len(point['runs']), 5)
        self.assertAlmostEqual(point['metrics']['total_iops'], 3000)

    def test_medium_write_limit_is_volume_or_time_without_time_based(self):
        runner = SimulatedRunner(self.args('--profiles', 'medium'), DEVICE.copy(), self.directory)
        runner.start_deadline()
        runner.prepare('medium')
        writes = [dict(x[2:].split('=', 1) for x in args if '=' in x) for args, _, _ in runner.calls]
        aging = next(options for options in writes if 'io_size' in options)
        self.assertNotIn('time_based', aging)
        self.assertEqual(int(aging['size']), 80 * bench.ALIGN)
        self.assertLessEqual(int(aging['io_size']), int(aging['size']) * .05)
        self.assertEqual(aging['runtime'], '120s')

    def test_sustained_measurements_are_bounded_even_for_large_read_heavy_targets(self):
        device = dict(DEVICE, size_bytes=100 * 1024 ** 4)
        runner = SimulatedRunner(self.args('--profiles', 'sustained', '--matrix', 'full'), device, self.directory)
        runner.start_deadline()
        point = next(p for p in runner.manifest['points'] if p['read_pct'] == 95 and p['qd'] == 1)
        runner.measure(point)
        self.assertEqual(point['status'], 'complete')
        self.assertEqual(len(point['runs']), 5)
        self.assertLess(self.clock.value - 100, 120)
        for args, _, _ in runner.calls:
            self.assertIn('--runtime=10s', args)
            self.assertFalse(any(x.startswith('--io_size=') for x in args))

    def test_preparation_deadline_includes_fill(self):
        runner = SimulatedRunner(self.args('--profiles', 'sustained', '--max-conditioning-time', '60s'), DEVICE.copy(), self.directory)
        runner.fill_seconds = 50
        runner.start_deadline()
        began = self.clock.value
        record = runner.prepare('sustained')
        self.assertLessEqual(self.clock.value - began, 60)
        self.assertEqual(record['status'], 'complete')
        self.assertEqual(record['conditioning_status'], 'time_cap_reached')
        self.assertTrue(all(deadline is not None and deadline <= began + 60 for _, _, deadline in runner.calls))

    def test_sustained_preparation_stops_after_observed_stability(self):
        runner = SimulatedRunner(self.args('--profiles', 'sustained'), DEVICE.copy(), self.directory)
        runner.start_deadline()
        record = runner.prepare('sustained')
        self.assertEqual(record['conditioning_status'], 'observed_stable')
        self.assertEqual(record['conditioning']['consecutive_windows'], 2)
        self.assertEqual(record['conditioning']['elapsed_s'], 360)
        self.assertLess(record['elapsed_s'], 600)
        self.assertEqual(len(runner.calls), 2)

    def test_timeout_before_complete_fill_prevents_measurements(self):
        runner = SimulatedRunner(self.args('--profiles', 'fresh', '--max-preparation-time', '10s'), DEVICE.copy(), self.directory)
        runner.fill_seconds = 20
        self.assertEqual(runner.run(), 1)
        self.assertFalse(any(p['runs'] for p in runner.manifest['points']))
        self.assertEqual(len(runner.calls), 1)

    def test_global_conditioning_timeout_is_not_a_successful_qualification(self):
        runner = SimulatedRunner(self.args('--profiles', 'sustained', '--max-run-time', '20s',
                                           '--max-conditioning-time', '60s'), DEVICE.copy(), self.directory)
        self.assertEqual(runner.run(), 1)
        self.assertEqual(runner.manifest['status'], 'timed_out')
        self.assertEqual(runner.manifest['preparation'][0]['status'], 'timed_out')
        self.assertFalse(any(point['runs'] for point in runner.manifest['points']))
        self.assertLessEqual(self.clock.value, 120)

    def test_partial_repeat_failure_preserves_completed_samples_and_median(self):
        runner = SimulatedRunner(self.args('--profiles', 'fresh'), DEVICE.copy(), self.directory)
        runner.start_deadline()
        point = next(p for p in runner.manifest['points'] if p['read_pct'] == 50)
        original = runner.fio
        invocation = 0

        def fail_third(*args, **kwargs):
            nonlocal invocation
            invocation += 1
            if invocation == 3:
                raise RuntimeError('synthetic third sample failure')
            return original(*args, **kwargs)

        with mock.patch.object(runner, 'fio', side_effect=fail_third):
            with self.assertRaisesRegex(RuntimeError, 'third sample'):
                runner.measure(point)
        self.assertEqual([sample['status'] for sample in point['runs']], ['complete', 'complete', 'failed'])
        self.assertEqual(point['status'], 'failed')
        self.assertAlmostEqual(point['metrics']['total_iops'], 5000)
        self.assertEqual(invocation, 3)
        self.assertTrue(any('2/5' in message for message in point['quality']))

    def test_high_worker_cpu_is_a_quality_warning(self):
        runner = SimulatedRunner(self.args('--profiles', 'fresh', '--run-count', '1'), DEVICE.copy(), self.directory)
        runner.start_deadline()
        point = runner.manifest['points'][0]
        result = counters(10000, 0, 10)
        result.update(usr_cpu=65, sys_cpu=30)
        with mock.patch.object(runner, 'fio', return_value=result):
            runner.measure(point)
        self.assertEqual(point['status'], 'complete')
        self.assertEqual(point['metrics']['cpu_user'], 65)
        self.assertTrue(any('host-limited' in message for message in point['quality']))

    def test_medium_preparation_reserves_time_for_recovery(self):
        runner = SimulatedRunner(self.args('--profiles', 'medium', '--max-preparation-time', '100s',
                                           '--recovery-time', '30s'), DEVICE.copy(), self.directory)
        runner.fill_seconds = 50
        runner.start_deadline()
        record = runner.prepare('medium')
        aging = next(dict(x[2:].split('=', 1) for x in args if '=' in x)
                     for args, _, _ in runner.calls if any(x.startswith('--io_size=') for x in args))
        self.assertLessEqual(int(aging['runtime'].rstrip('s')), 20)
        self.assertEqual(record['recovery_status'], 'complete')
        self.assertLessEqual(record['elapsed_s'], 100)

    def test_medium_unperformed_conditioning_and_short_recovery_are_qualified(self):
        runner = SimulatedRunner(self.args('--profiles', 'medium', '--max-preparation-time', '50s'), DEVICE.copy(), self.directory)
        runner.fill_seconds = 45
        self.assertEqual(runner.run(), 0)
        record = runner.manifest['preparation'][0]
        self.assertEqual(record['conditioning_status'], 'time_cap_reached')
        self.assertEqual(record['recovery_status'], 'time_cap_reached')
        self.assertEqual(record['recovery_elapsed_s'], 5)
        self.assertEqual(len(record['quality']), 2)
        for point in runner.manifest['points']:
            self.assertEqual(point['quality'], record['quality'])
            self.assertEqual(point['recovery_status'], 'time_cap_reached')

    def test_explicitly_disabled_medium_conditioning_remains_not_requested(self):
        runner = SimulatedRunner(self.args('--profiles', 'medium', '--medium-conditioning-time', '0',
                                           '--recovery-time', '0'), DEVICE.copy(), self.directory)
        record = runner.prepare('medium')
        self.assertEqual(record['conditioning_status'], 'not_requested')
        self.assertEqual(record['quality'], [])

    def test_failed_identity_check_does_not_reuse_prior_job_write_bytes(self):
        runner = SimulatedRunner(self.args('--profiles', 'fresh'), DEVICE.copy(), self.directory)
        runner.last_job = counters(0, 1000000, 10)
        with mock.patch.object(bench, 'inspect_device', return_value=dict(DEVICE, serial='REPLACED')):
            self.assertEqual(runner.run(), 1)
        self.assertEqual(runner.manifest['preparation'][0]['host_write_bytes'], 0)

    def test_clean_but_short_fio_result_is_not_a_complete_sample(self):
        runner = SimulatedRunner(self.args('--profiles', 'fresh'), DEVICE.copy(), self.directory)
        runner.start_deadline()
        point = runner.manifest['points'][0]
        with mock.patch.object(runner, 'fio', return_value=counters(100, 0, 5)):
            with self.assertRaisesRegex(RuntimeError, 'before its requested measurement duration'):
                runner.measure(point)
        self.assertEqual(point['status'], 'failed')
        self.assertEqual(point['runs'][0]['status'], 'failed')
        self.assertNotIn('metrics', point)

    def test_identity_change_stops_before_discard(self):
        runner = SimulatedRunner(self.args('--profiles', 'fresh'), DEVICE.copy(), self.directory)
        with mock.patch.object(bench, 'inspect_device', return_value=dict(DEVICE, serial='REPLACED')):
            self.assertEqual(runner.run(), 1)
        self.command.assert_not_called()
        self.assertIn('identity', runner.manifest['error'])
        self.assertEqual(runner.manifest['preparation'][0]['status'], 'failed')

    def test_incomplete_preparation_never_reaches_measurement(self):
        runner = SimulatedRunner(self.args('--profiles', 'fresh'), DEVICE.copy(), self.directory)
        with mock.patch.object(runner, 'fio', return_value=counters(0, 1, 1)) as fio:
            self.assertEqual(runner.run(), 1)
        self.assertEqual(fio.call_count, 1)
        self.assertIn('initialize its entire range', runner.manifest['error'])
        self.assertEqual(runner.manifest['preparation'][0]['status'], 'failed')

    def test_fio_failure_stops_without_starting_next_point(self):
        runner = SimulatedRunner(self.args('--profiles', 'fresh'), DEVICE.copy(), self.directory)
        runner.failure = RuntimeError('synthetic fio I/O error')
        self.assertEqual(runner.run(), 1)
        self.assertEqual(runner.manifest['status'], 'failed')
        self.assertEqual(runner.manifest['points'][0]['status'], 'failed')
        self.assertTrue(all(p['status'] == 'pending' for p in runner.manifest['points'][1:]))
        self.assertEqual(len(runner.manifest['preparation']), 1)
        self.assertEqual(runner.saves[-1], (True, True))

    def test_interrupt_preserves_partial_manifest(self):
        runner = SimulatedRunner(self.args('--profiles', 'fresh'), DEVICE.copy(), self.directory)
        runner.failure = InterruptedError('synthetic interrupt')
        self.assertEqual(runner.run(), 1)
        self.assertEqual(runner.manifest['status'], 'interrupted')
        self.assertEqual(runner.manifest['points'][0]['status'], 'interrupted')
        self.assertTrue(all(p['status'] == 'pending' for p in runner.manifest['points'][1:]))
        self.assertEqual(runner.saves[-1], (True, True))

    def test_signal_stops_active_process_group(self):
        runner = SimulatedRunner(self.args(), DEVICE.copy(), self.directory)
        runner.process = mock.Mock(pid=99999999)
        runner.process.poll.return_value = None
        with mock.patch.object(bench.os, 'killpg') as kill:
            runner.interrupted(signal.SIGTERM, None)
        self.assertTrue(runner.stop)
        kill.assert_called_once_with(99999999, signal.SIGINT)

    def test_signal_racing_process_exit_still_records_cancellation(self):
        runner = SimulatedRunner(self.args(), DEVICE.copy(), self.directory)
        runner.process = mock.Mock(pid=99999999)
        runner.process.poll.return_value = None
        with mock.patch.object(bench.os, 'killpg', side_effect=ProcessLookupError):
            runner.interrupted(signal.SIGTERM, None)
        self.assertTrue(runner.stop)

    def test_truncated_final_json_does_not_reuse_previous_snapshot(self):
        runner = SimulatedRunner(self.args(), DEVICE.copy(), self.directory)

        def completed_process(cmd, stdout, stderr, **kwargs):
            stdout.write(json.dumps({'jobs': [counters(100, 10, 1)]}) + '\n{"jobs":[')
            stdout.flush()
            process = mock.Mock(pid=99999999)
            process.poll.return_value = 0
            process.wait.return_value = 0
            return process

        with mock.patch.object(bench.subprocess, 'Popen', side_effect=completed_process):
            with self.assertRaisesRegex(RuntimeError, 'truncated fio JSON'):
                bench.Runner.fio(runner, [], self.directory)
        self.assertIsNone(runner.process)
        self.assertFalse((self.directory / 'final-job.json').exists())

    def test_dry_run_does_not_create_output_or_execute_runner(self):
        output = self.directory / 'not-created'
        with mock.patch.object(bench, 'inspect_device', return_value=DEVICE.copy()), \
             mock.patch.object(bench, 'output_outside_target'), \
             mock.patch.object(bench.Runner, 'run') as run, \
             mock.patch.object(bench.Runner, 'fio') as fio:
            self.assertEqual(bench.main(['--filename', TARGET, '--allow-partition', '--dry-run',
                                         '--results-dir', str(output)]), 0)
        self.assertFalse(output.exists())
        run.assert_not_called()
        fio.assert_not_called()
        self.command.assert_not_called()


class JSONStreamTests(unittest.TestCase):
    def test_warning_with_braces_does_not_hide_next_snapshot(self):
        stream = bench.JSONStream()
        self.assertEqual(stream.feed('warning {bad option}\n{"jobs": []}'), [{'jobs': []}])

    def test_chunks_warnings_multiple_objects_and_braces_in_strings(self):
        stream = bench.JSONStream()
        payloads = [{'jobs': [{'name': 'literal } { braces', 'error': 0}]}, {'jobs': []}]
        raw = 'fio warning\n' + json.dumps(payloads[0]) + '\n' + json.dumps(payloads[1])
        observed = []
        for char in raw:
            observed.extend(stream.feed(char))
        self.assertEqual(observed, payloads)

    def test_incomplete_snapshot_not_returned_as_complete(self):
        stream = bench.JSONStream()
        self.assertEqual(stream.feed('{"jobs":['), [])
        self.assertEqual(stream.feed(']}'), [{'jobs': []}])


class DeviceSafetyTests(unittest.TestCase):
    """Drive discovery operates exclusively on a fabricated namespace tree."""

    def setUp(self):
        self.args = bench.parse_args(['--filename', TARGET, '--allow-partition'])
        self.tree = {'blockdevices': [dict(name='nvme999n1', path='/dev/nvme999n1',
                     type='disk', size=200 * bench.ALIGN, mountpoint=None,
                     children=[dict(name='nvme999n1p1', path='/dev/nvme999n1p1',
                                    type='part', size=99 * bench.ALIGN, mountpoint=None),
                               dict(name='nvme999n1p2', path=TARGET, type='part',
                                    size=100 * bench.ALIGN, mountpoint=None)])]}
        self.swaps = ''
        self.fuser_rc = 1
        self.holders = []
        self.block_mode = stat.S_IFBLK
        self.start = '2048'
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(Path, 'resolve', lambda path, **kwargs: path))
        self.stack.enter_context(mock.patch.object(Path, 'stat', lambda path, **kwargs:
                                types.SimpleNamespace(st_mode=self.block_mode, st_rdev=os.makedev(259, 99))))
        self.stack.enter_context(mock.patch.object(Path, 'iterdir', lambda path: iter(self.holders)))
        self.stack.enter_context(mock.patch.object(bench.os, 'sched_getaffinity', return_value={0, 1, 2, 3}))
        self.stack.enter_context(mock.patch.object(bench.shutil, 'which', return_value=None))
        self.stack.enter_context(mock.patch.object(bench, 'read_text', side_effect=self.read_text))
        self.commands = self.stack.enter_context(mock.patch.object(bench, 'command', side_effect=self.command))

    def read_text(self, path):
        path = str(path)
        if path.endswith('/start'):
            return self.start
        if path.endswith('/thread_siblings_list'):
            return '0-1'
        if path.endswith('/model'):
            return 'Mock NVMe'
        return None

    def command(self, args, optional=False, **kwargs):
        rc, out = 0, ''
        if args[0] == 'lsblk':
            out = json.dumps(self.tree)
        elif args[0] == 'fuser':
            rc = self.fuser_rc
        elif args[0] == 'swapon':
            out = self.swaps
        elif args[:2] == ['blockdev', '--getsize64']:
            out = str(100 * bench.ALIGN)
        elif args[:2] == ['blockdev', '--getss']:
            out = '512'
        elif args == ['fio', '--version']:
            out = 'fio-3.28'
        else:
            raise AssertionError(f'unexpected command {args}')
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr='')

    def test_allowed_partition_reports_scope_and_cpu(self):
        info = bench.inspect_device(self.args)
        self.assertEqual(info['path'], TARGET)
        self.assertEqual(info['namespace'], '/dev/nvme999n1')
        self.assertEqual(info['partition_fraction'], 0.5)
        self.assertEqual(info['start_bytes'], bench.ALIGN)
        self.assertEqual(info['cpu'], 0)
        self.assertEqual(info['poll_cpu'], 2)
        self.assertEqual(sum(call.args[0][0] == 'fuser' for call in self.commands.call_args_list), 3)

    def test_partition_requires_explicit_flag(self):
        self.args.allow_partition = False
        with self.assertRaisesRegex(RuntimeError, 'allow-partition'):
            bench.inspect_device(self.args)
        self.commands.assert_not_called()

    def test_regular_file_rejected(self):
        self.block_mode = stat.S_IFREG
        with self.assertRaisesRegex(RuntimeError, 'target must be'):
            bench.inspect_device(self.args)

    def test_mounted_sibling_rejected(self):
        self.tree['blockdevices'][0]['children'][0]['mountpoint'] = '/data'
        with self.assertRaisesRegex(RuntimeError, 'mounted device'):
            bench.inspect_device(self.args)

    def test_holders_and_open_devices_rejected(self):
        self.holders = [Path('/sys/mock/dm-0')]
        with self.assertRaisesRegex(RuntimeError, 'active holders'):
            bench.inspect_device(self.args)
        self.holders = []
        self.fuser_rc = 0
        with self.assertRaisesRegex(RuntimeError, 'open by another process'):
            bench.inspect_device(self.args)
        self.fuser_rc = 2
        with self.assertRaisesRegex(RuntimeError, 'cannot check device users'):
            bench.inspect_device(self.args)

    def test_swap_on_sibling_rejected(self):
        self.swaps = '/dev/nvme999n1p1\n'
        with self.assertRaisesRegex(RuntimeError, 'swap is active'):
            bench.inspect_device(self.args)

    def test_unaligned_partition_rejected(self):
        self.start = '1'
        with self.assertRaisesRegex(RuntimeError, '4 KiB aligned'):
            bench.inspect_device(self.args)

    def test_optional_identification_timeouts_do_not_block_device_checks(self):
        original = self.command

        def slow_nvme(args, **kwargs):
            if args[0] == 'nvme':
                raise bench.subprocess.TimeoutExpired(args, kwargs['timeout'])
            return original(args, **kwargs)

        with mock.patch.object(bench.shutil, 'which', return_value='/mock/tool'), \
             mock.patch.object(bench, 'command', side_effect=slow_nvme):
            info = bench.inspect_device(self.args)
        for key in ('identify_controller', 'identify_namespace', 'power_state', 'volatile_write_cache'):
            self.assertEqual(info[key], {'unavailable': 'command timed out'})


class NullRunner(bench.Runner):
    """Real fio process, diskless null engine: no target or file I/O occurs."""
    def fio_base(self):
        return ['fio', '--ioengine=null', '--size=1M', '--rw=randrw',
                '--rwmixread=70', '--numjobs=1',
                '--time_based=1', '--rate_iops=10000', '--output-format=json+']

    def telemetry(self, force=False):
        pass

    def fio(self, args, directory, on_snapshot=None, deadline=None):
        # Each --name starts another fio job, so never supply a duplicate name.
        if not any(arg.startswith('--name=') for arg in args):
            args = ['--name=null-stream-test', *args]
        return super().fio(args, directory, on_snapshot, deadline)


@unittest.skipUnless(shutil.which('fio'), 'fio is required for diskless stream integration')
class RealNullEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='nvme-null-stream-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.runner = NullRunner(bench.parse_args(['--filename', TARGET]), DEVICE.copy(), self.directory)

    def test_real_process_drains_final_snapshot_and_writes_artifacts(self):
        seen = []
        final = self.runner.fio(['--runtime=2s', '--status-interval=1'], self.directory,
                                lambda job: seen.append(job) or False)
        self.assertGreaterEqual(len(seen), 2)
        self.assertGreaterEqual(final['job_runtime'], 1900)
        self.assertEqual(final, seen[-1])
        self.assertEqual(final, json.loads((self.directory / 'final-job.json').read_text()))
        self.assertTrue((self.directory / 'fio.stdout').stat().st_size)
        self.assertIsNone(self.runner.process)
        command = json.loads((self.directory / 'command.json').read_text())
        self.assertIn('--ioengine=null', command)
        self.assertFalse(any(arg.startswith('--filename=') for arg in command))

    def test_callback_sigint_stops_real_process_and_retains_final_counters(self):
        seen = []
        final = self.runner.fio(['--runtime=10s', '--status-interval=1'], self.directory,
                                lambda job: seen.append(job) or True)
        self.assertEqual(len(seen), 1)
        self.assertGreaterEqual(final['job_runtime'], seen[0]['job_runtime'])
        self.assertLess(final['job_runtime'], 9000)
        self.assertEqual(final['error'], 0)
        self.assertIsNone(self.runner.process)
        self.assertTrue((self.directory / 'final-job.json').exists())

    def test_wall_clock_deadline_stops_a_long_null_job(self):
        began = time.monotonic()
        with self.assertRaises(bench.BudgetExpired):
            self.runner.fio(['--runtime=30s', '--status-interval=1'], self.directory,
                            deadline=began + 1)
        self.assertLess(time.monotonic() - began, 8)
        self.assertIsNone(self.runner.process)
        self.assertIsNotNone(self.runner.last_job)

    def test_fio_ramp_is_excluded_from_final_measured_byte_counters(self):
        began = time.monotonic()
        final = self.runner.fio(['--rw=write', '--bs=4K', '--rate_iops=100',
                                 '--runtime=2s', '--ramp_time=1s'], self.directory)
        self.assertGreater(time.monotonic() - began, 2.8)
        self.assertGreaterEqual(final['write']['total_ios'], 190)
        self.assertLessEqual(final['write']['total_ios'], 210)
        self.assertLess(final['job_runtime'], 2300)
        record = {}
        with mock.patch.object(self.runner, 'host_writes', return_value=None):
            self.runner.record_writes(record, None, final['write']['io_bytes'])
        self.assertEqual(record['host_write_accounting'], 'measured_only_lower_bound')

    def test_measure_three_real_null_repeats_generates_median_and_raw_reports(self):
        args = bench.parse_args(['--filename', TARGET, '--profiles', 'fresh', '--run-count', '3',
                                 '--runtime', '1s', '--ramp-time', '1s'])
        runner = NullRunner(args, DEVICE.copy(), self.directory)
        point = next(p for p in runner.manifest['points'] if p['pattern'] == 'random'
                     and p['read_pct'] == 50 and p['qd'] == 1 and p['bs'] == 4096)
        runner.start_deadline()
        runner.measure(point)
        self.assertEqual(point['status'], 'complete')
        self.assertEqual(point['metrics']['run_count'], 3)
        self.assertEqual(len(point['runs']), 3)
        rates = []
        for sample in point['runs']:
            self.assertEqual(sample['status'], 'complete')
            self.assertGreater(sample['elapsed_s'], 1.8)
            artifact = self.directory / sample['relative_dir']
            final = json.loads((artifact / 'final-job.json').read_text())
            self.assertGreaterEqual(final['job_runtime'], 950)
            self.assertLess(final['job_runtime'], 1200)
            self.assertEqual(sample['metrics']['read']['io_count'], final['read']['total_ios'])
            self.assertIn('90', sample['metrics']['read']['percentiles_ns'])
            rates.append(sample['metrics']['read']['iops'])
            command = json.loads((artifact / 'command.json').read_text())
            self.assertIn('--ioengine=null', command)
            self.assertFalse(any(arg.startswith('--filename=') for arg in command))
        expected = statistics.median(rates)
        self.assertAlmostEqual(point['metrics']['read']['iops'], expected)
        manifest = json.loads((self.directory / 'manifest.json').read_text())
        saved = next(p for p in manifest['points'] if p['id'] == point['id'])
        self.assertEqual(saved['metrics']['run_count'], 3)
        with (self.directory / 'results.csv').open() as stream:
            summary = next(row for row in csv.DictReader(stream)
                           if row['id'] == point['id'] and row['direction'] == 'read')
        self.assertAlmostEqual(float(summary['iops']), expected)
        self.assertEqual(summary['run_count'], '3')
        self.assertTrue(summary['clat_p90_ns'])
        with (self.directory / 'raw-runs.csv').open() as stream:
            raw = [row for row in csv.DictReader(stream) if row['id'] == point['id']]
        self.assertEqual(len(raw), 6)
        self.assertEqual({row['run_index'] for row in raw}, {'1', '2', '3'})
        self.assertTrue(all(row['clat_p90_ns'] for row in raw))
        html = (self.directory / 'report.html').read_text()
        self.assertIn('Median kIOPS', html)
        self.assertIn('3 / 3', html)
        self.assertIn(f'{expected / 1000:,.2f}', html)


class NamespaceLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='nvme-lock-test-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def test_sibling_partitions_contend_and_lock_can_be_reacquired_after_exit(self):
        sibling = dict(DEVICE, path='/dev/nvme999n1p1', major_minor='259:98')
        first = bench.NamespaceLock(DEVICE, self.directory)
        with first:
            with self.assertRaisesRegex(RuntimeError, 'another benchmark'):
                with bench.NamespaceLock(sibling, self.directory):
                    self.fail('sibling namespace lock unexpectedly acquired')
        self.assertTrue(first.path.exists())
        with bench.NamespaceLock(sibling, self.directory):
            pass

    def test_independent_namespaces_do_not_block_each_other(self):
        other = dict(DEVICE, namespace='/dev/nvme998n1')
        with bench.NamespaceLock(DEVICE, self.directory), bench.NamespaceLock(other, self.directory):
            pass

    def test_lock_does_not_follow_a_symlink(self):
        lock = bench.NamespaceLock(DEVICE, self.directory)
        target = self.directory / 'unrelated'
        target.write_text('untouched')
        lock.path.symlink_to(target)
        with self.assertRaises(OSError):
            with lock:
                self.fail('symlink was followed')
        self.assertEqual(target.read_text(), 'untouched')


class CommandTimeoutTests(unittest.TestCase):
    def test_commands_have_a_finite_default_timeout(self):
        process = mock.Mock(returncode=0)
        process.communicate.return_value = ('', '')
        with mock.patch.object(bench.subprocess, 'Popen', return_value=process):
            bench.command(['true'])
        self.assertGreater(process.communicate.call_args.kwargs['timeout'], 0)
        self.assertLessEqual(process.communicate.call_args.kwargs['timeout'], 30)

    def test_nested_command_budget_uses_earliest_deadline_and_restores_context(self):
        process = mock.Mock(returncode=0)
        process.communicate.return_value = ('', '')
        with mock.patch.object(bench.subprocess, 'Popen', return_value=process), \
             mock.patch.object(bench.time, 'monotonic', return_value=100):
            with bench.command_budget(120):
                with bench.command_budget(110):
                    bench.command(['true'], timeout=30)
                    self.assertEqual(process.communicate.call_args.kwargs['timeout'], 10)
                bench.command(['true'], timeout=30)
                self.assertEqual(process.communicate.call_args.kwargs['timeout'], 20)
            bench.command(['true'], timeout=30)
            self.assertEqual(process.communicate.call_args.kwargs['timeout'], 30)

    def test_expired_command_budget_never_launches_a_process(self):
        with mock.patch.object(bench.subprocess, 'Popen') as popen, \
             mock.patch.object(bench.time, 'monotonic', return_value=100), \
             bench.command_budget(99):
            with self.assertRaises(bench.BudgetExpired):
                bench.command(['true'])
        popen.assert_not_called()

    def test_cancelled_command_budget_never_launches_a_process(self):
        with mock.patch.object(bench.subprocess, 'Popen') as popen, \
             bench.command_budget(time.monotonic() + 30, cancelled=lambda: True):
            with self.assertRaises(InterruptedError):
                bench.command(['true'])
        popen.assert_not_called()
        self.assertIsNone(bench.COMMAND_CANCELLED.get())

    @unittest.skipUnless(shutil.which('sleep'), 'sleep executable needed for timeout integration')
    def test_real_sleep_timeout_kills_and_reaps_only_its_own_child(self):
        spawn = bench.subprocess.Popen
        children = []

        def capture(*args, **kwargs):
            child = spawn(*args, **kwargs)
            children.append(child)
            return child

        began = time.monotonic()
        with mock.patch.object(bench.subprocess, 'Popen', side_effect=capture):
            with self.assertRaises(bench.subprocess.TimeoutExpired):
                bench.command(['sleep', '2'], timeout=.05)
        self.assertLess(time.monotonic() - began, 1.5)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())

    @unittest.skipUnless(shutil.which('sleep'), 'sleep executable needed for cancellation integration')
    def test_real_command_cancellation_does_not_wait_for_long_budget(self):
        spawn = bench.subprocess.Popen
        children = []

        def capture(*args, **kwargs):
            child = spawn(*args, **kwargs)
            children.append(child)
            return child

        began = time.monotonic()
        with mock.patch.object(bench.subprocess, 'Popen', side_effect=capture), \
             bench.command_budget(began + 30, cancelled=lambda: time.monotonic() - began >= .1):
            with self.assertRaises(InterruptedError):
                bench.command(['sleep', '2'], timeout=30)
        self.assertLess(time.monotonic() - began, 1.5)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertIsNone(bench.COMMAND_CANCELLED.get())


if __name__ == '__main__':
    unittest.main()
