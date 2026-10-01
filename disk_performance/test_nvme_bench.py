"""Device-free orchestration tests. No test here opens a block device."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import shutil
import stat
import time
import tempfile
import types
import unittest
from unittest import mock

import nvme_bench as bench


TARGET = '/dev/nvme999n1p2'
DEVICE = dict(path=TARGET, namespace='/dev/nvme999n1', controller='/dev/nvme999',
              partition=True, size_bytes=100 * bench.ALIGN, namespace_size_bytes=200 * bench.ALIGN,
              cpu=32, poll_cpu=33, major_minor='259:99', serial='MOCK', model='Mock NVMe',
              start_bytes=bench.ALIGN,
              placement=dict(numa_node=1, cpu_pool=list(range(32, 64)) + list(range(96, 128)),
                             physical_cpus=list(range(32, 64)), memory_policy='bind:1'))


def args(*extra):
    return bench.parse_args(['--filename', TARGET, '--allow-partition', '--numa-node', '1', *extra])


def job(name, writes, runtime_ms=1000, cpu=10, reads=0):
    result = dict(jobname=name, error=0, job_runtime=runtime_ms, usr_cpu=cpu, sys_cpu=0,
                  iodepth_level={'1': 100})
    for direction, nbytes in [('read', reads), ('write', writes)]:
        count = nbytes // 4096
        result[direction] = dict(io_bytes=nbytes, total_ios=count,
                                 runtime=runtime_ms if count else 0,
                                 clat_ns=dict(N=count, mean=1000 if count else 0,
                                              max=1000 if count else 0,
                                              bins={'1000': count} if count else {}),
                                 lat_ns=dict(N=count, mean=1100 if count else 0))
    return result


class StaticPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def runner(self, *options):
        return bench.Runner(args(*options), DEVICE.copy(), self.directory)

    def test_comparison_schedule_rotates_and_has_unique_linked_records(self):
        runner = self.runner()
        manifest = runner.manifest
        self.assertEqual(manifest['schema_version'], 3)
        self.assertEqual(manifest['config']['comparison_protocol_revision'], 2)
        self.assertEqual(len(manifest['points']), 63)
        self.assertEqual(len(manifest['references']), 18)
        self.assertEqual(len([x for x in manifest['schedule'] if x['kind'] == 'preparation']), 9)
        self.assertEqual([x['profile'] for x in manifest['schedule'] if x['kind'] == 'preparation'],
                         ['fresh', 'medium', 'sustained', 'medium', 'sustained', 'fresh',
                          'sustained', 'fresh', 'medium'])
        ids = [x['id'] for x in manifest['schedule']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len({p['relative_dir'] for p in manifest['points'] + manifest['references']}), 81)
        for trial in (1, 2, 3):
            for profile in bench.PROFILES:
                entries = [entry for entry in manifest['schedule']
                           if entry['trial'] == trial and entry['profile'] == profile]
                self.assertEqual([entry['anchor'] for entry in entries if entry['kind'] == 'observation'],
                                 [anchor for anchor, *_ in bench.COMPARISON_ANCHORS])
                self.assertEqual(entries[-3]['anchor'], 'wr4k_qd1')
                self.assertEqual(entries[-3]['stage'], 'after_wr4k_qd64_before_final_read_references')
                self.assertEqual([entry['kind'] for entry in entries[-3:]],
                                 ['observation', 'reference', 'reference'])
                self.assertEqual([entry['anchor'] for entry in entries[-2:]],
                                 ['rr4k_qd1', 'rr4k_qd64'])
        for point in manifest['points'] + manifest['references']:
            self.assertIn(point['preparation_id'], ids)
        self.assertEqual(runner.budget()['run_cap_s'], 24087)
        self.assertEqual(runner.budget()['sample_count'], 63)
        self.assertEqual(runner.budget()['reference_count'], 18)

    def test_default_writes_use_full_identical_occupancy_and_exposure(self):
        runner = self.runner('--size-percent', '40')
        occupied = 80 * bench.ALIGN
        self.assertEqual({runner.occupied_size(p) for p in bench.PROFILES}, {occupied})
        self.assertEqual({runner.size(p) for p in bench.PROFILES}, {40 * bench.ALIGN})
        writes = runner.minimum_writes()
        self.assertEqual(writes['preparation_bytes'], 9 * occupied)
        self.assertEqual(writes['sustained_write_floor_bytes'], 3 * occupied)
        self.assertEqual(writes['medium_write_target_bytes'], 3 * occupied * 5 // 100)
        with self.assertRaises(SystemExit):
            args('--size-percent', '81')

    def test_worker_shards_cover_range_and_depth_is_aggregate(self):
        runner = self.runner()
        layout = runner.worker_layout('random', 64, 80 * bench.ALIGN)
        self.assertEqual([w['qd'] for w in layout], [16] * 4)
        self.assertEqual([w['offset'] for w in layout], [i * 20 * bench.ALIGN for i in range(4)])
        self.assertEqual(sum(w['size'] for w in layout), 80 * bench.ALIGN)
        self.assertEqual(len({w['cpu'] for w in layout}), 4)
        self.assertEqual(len(runner.worker_layout('random', 1, 80 * bench.ALIGN)), 1)
        self.assertEqual(len(runner.worker_layout('sequential', 64, 80 * bench.ALIGN)), 1)
        self.assertEqual([w['qd'] for w in runner.worker_layout('random', 5, 80 * bench.ALIGN)], [2, 1, 1, 1])
        self.assertEqual(len(runner.worker_layout('random', 128, 80 * bench.ALIGN, requested=4)), 4)

    def test_seeds_match_profiles_and_change_by_trial_worker_repeat(self):
        runner = self.runner('--run-count', '2')
        points = runner.manifest['points']
        fresh = next(p for p in points if p['trial'] == 1 and p['profile'] == 'fresh' and p['anchor'] == 'rr4k_qd64')
        medium = next(p for p in points if p['trial'] == 1 and p['profile'] == 'medium' and p['anchor'] == 'rr4k_qd64')
        other = next(p for p in points if p['trial'] == 2 and p['profile'] == 'fresh' and p['anchor'] == 'rr4k_qd64')
        def seeds(point, repeat):
            return [w['seed'] for w in runner.workload_command(point, repeat, self.directory)[1]]
        self.assertEqual(seeds(fresh, 1), seeds(medium, 1))
        self.assertNotEqual(seeds(fresh, 1), seeds(other, 1))
        self.assertNotEqual(seeds(fresh, 1), seeds(fresh, 2))
        self.assertEqual(len(set(seeds(fresh, 1))), 4)
        write_fresh = next(p for p in points if p['trial'] == 1 and p['profile'] == 'fresh'
                           and p['anchor'] == 'wr4k_qd1')
        write_medium = next(p for p in points if p['trial'] == 1 and p['profile'] == 'medium'
                            and p['anchor'] == 'wr4k_qd1')
        self.assertEqual(seeds(write_fresh, 1), seeds(write_medium, 1))
        self.assertEqual(len(seeds(write_fresh, 1)), 1)
        write_command, _ = runner.workload_command(write_fresh, 1, self.directory)
        self.assertIn('--rw=randwrite', write_command)
        self.assertIn('--runtime=20s', write_command)
        self.assertIn('--ramp_time=5s', write_command)

    def test_measure_persists_post_ramp_log_interval_origin(self):
        runner = self.runner()
        point = runner.manifest['points'][0]
        with mock.patch.object(runner, 'host_writes', return_value=0), \
             mock.patch.object(runner, 'save'), \
             mock.patch.object(runner, 'fio', side_effect=RuntimeError('synthetic fio failure')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic fio failure'):
                runner.measure(point)
        metadata = point['runs'][0]['log_intervals']
        self.assertEqual(metadata['mode'], 'averaged_contiguous')
        self.assertEqual(metadata['average_ms'], 1000)
        self.assertEqual(metadata['origin_ms'], 0)
        self.assertEqual(metadata['ramp_time_ms'], runner.args.ramp_time * 1000)
        self.assertEqual(metadata['time_reference'], 'post_ramp_measurement_epoch')

    def test_protocol_migration_and_sweep_compatibility(self):
        self.assertEqual(args().protocol, 'comparison')
        with contextlib.redirect_stderr(io.StringIO()) as stream:
            selected = args('--matrix', 'full')
        self.assertEqual(selected.protocol, 'sweep')
        self.assertIn('migration', stream.getvalue())
        self.assertEqual(selected.trials, 1)
        self.assertEqual(selected.run_count, 5)
        points = bench.build_points(selected)
        self.assertEqual(len(points), 3 * 98)
        self.assertEqual({profile: sum(point['profile'] == profile for point in points)
                          for profile in selected.profiles}, {profile: 98 for profile in selected.profiles})
        sweep_schedule, sweep_refs = bench.build_schedule(selected, bench.build_points(selected))
        first = [entry for entry in sweep_schedule if entry['profile'] == 'fresh']
        self.assertEqual([entry['kind'] for entry in first[:3]],
                         ['preparation', 'reference', 'observation'])
        self.assertEqual(first[1]['stage'], 'early')
        self.assertEqual(first[-1]['stage'], 'late')
        self.assertEqual([ref['stage'] for ref in sweep_refs if ref['profile'] == 'fresh'], ['early', 'late'])
        self.assertEqual(args('--profiles', 'fresh', '--prepare-only').trials, 1)
        with self.assertRaises(SystemExit):
            args('--protocol', 'comparison', '--matrix', 'full')
        self.assertEqual(args('--profiles', 'used-recovered').profiles, ['medium'])

    def test_dry_preview_includes_every_execution_layout(self):
        runner = self.runner('--profiles', 'fresh', '--trials', '2')
        preview = runner.schedule_preview()
        self.assertEqual(len(preview), 2 * (1 + 7 + 2))
        observations = [x for x in preview if x['kind'] != 'preparation']
        self.assertTrue(all(x['samples'][0]['workers'] for x in observations))
        self.assertEqual(preview[0]['occupied_bytes'], 80 * bench.ALIGN)

    def test_smoke_keeps_anchor_worker_layouts_but_reduces_duration(self):
        runner = self.runner('--run-type', 'smoke')
        self.assertEqual(runner.args.trials, 1)
        self.assertEqual(runner.args.measurement_time, 2)
        self.assertEqual(len([x for x in runner.manifest['schedule'] if x['kind'] == 'preparation']), 3)
        qd1 = next(p for p in runner.manifest['points'] if p['anchor'] == 'rr4k_qd1')
        write_qd1 = next(p for p in runner.manifest['points'] if p['anchor'] == 'wr4k_qd1')
        qd64 = next(p for p in runner.manifest['points'] if p['anchor'] == 'rr4k_qd64')
        self.assertEqual(len(runner.workload_command(qd1, 1, self.directory)[1]), 1)
        command, layout = runner.workload_command(write_qd1, 1, self.directory)
        self.assertEqual(len(layout), 1)
        self.assertIn('--rw=randwrite', command)
        self.assertIn('--iodepth=1', command)
        self.assertIn('--ramp_time=0s', command)
        self.assertIn('--ioengine=io_uring', runner.fio_base())
        self.assertIn('--direct=1', runner.fio_base())
        self.assertEqual(len(runner.workload_command(qd64, 1, self.directory)[1]), 4)


class PlacementTests(unittest.TestCase):
    def test_online_permitted_pool_and_distinct_physical_cores(self):
        def reader(path):
            path = str(path)
            if path.endswith('node1/cpulist'):
                return '32-33,96-97'
            if path.endswith('/cpu/online'):
                return '0-127'
            if 'physical_package_id' in path:
                return '0'
            if 'core_id' in path:
                return '0' if 'cpu32/' in path or 'cpu96/' in path else '1'
            return None
        with mock.patch.object(bench, 'read_text', side_effect=reader):
            placement = bench.numa_placement(1, {32, 33, 96, 97, 127})
        self.assertEqual(placement['cpu_pool'], [32, 33, 96, 97])
        self.assertEqual(placement['physical_cpus'], [32, 33])
        self.assertEqual(placement['memory_policy'], 'bind:1')

    def test_missing_node_and_no_permitted_cpu_fail(self):
        with mock.patch.object(bench, 'read_text', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'unavailable'):
                bench.numa_placement(5, {32})
        def reader(path):
            return '32-63' if str(path).endswith('node1/cpulist') else '0-127'
        with mock.patch.object(bench, 'read_text', side_effect=reader):
            with self.assertRaisesRegex(RuntimeError, 'no online permitted'):
                bench.numa_placement(1, {0, 1})


class FakeClock:
    def __init__(self):
        self.value = 100.0
    def monotonic(self):
        return self.value
    def sleep(self, seconds):
        self.value += seconds


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.clock = FakeClock()
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(mock.patch.object(bench.time, 'monotonic', self.clock.monotonic))
        self.patches.enter_context(mock.patch.object(bench.time, 'sleep', self.clock.sleep))
        self.patches.enter_context(mock.patch.object(bench, 'inspect_device', return_value=DEVICE))
        self.patches.enter_context(mock.patch.object(bench, 'command', return_value=types.SimpleNamespace(stdout='', stderr='', returncode=0)))
        self.runner = bench.Runner(args('--profiles', 'medium', '--max-preparation-time', '5m'), DEVICE.copy(), self.directory)
        self.runner.save = lambda **kwargs: None
        self.runner.telemetry = lambda **kwargs: None
        self.runner.host_writes = lambda: None
        self.runner.start_deadline()

    def test_medium_incomplete_exposure_is_exploratory_after_full_fill(self):
        def fio(options, directory, on_snapshot=None, deadline=None):
            if Path(directory).name == 'fill':
                return job('prepare', 80 * bench.ALIGN)
            return job('condition', 4096)
        self.runner.fio = fio
        record = self.runner.prepare('medium', 1)
        self.assertEqual(record['status'], 'complete')
        self.assertFalse(record['qualified'])
        self.assertEqual(record['fill_fraction'], 1)
        self.assertEqual(record['conditioning_status'], 'time_cap_reached')
        self.assertEqual(record['recovery_status'], 'complete')
        self.assertEqual(record['measured_write_bytes'], 80 * bench.ALIGN + 4096)
        self.assertEqual(record['host_write_bytes'], 80 * bench.ALIGN + 4096)
        self.assertEqual(record['host_write_accounting'], 'measured_only_lower_bound')
        from profile_report import render_report
        render_report(self.directory, self.runner.manifest, plots=False)
        with (self.directory / 'preparations.csv').open() as handle:
            import csv
            prepared = next(csv.DictReader(handle))
        self.assertEqual(int(prepared['host_write_bytes']), 80 * bench.ALIGN + 4096)

    def test_incomplete_fill_stops_before_conditioning(self):
        self.runner.fio = lambda *a, **k: job('prepare', 4096)
        with self.assertRaisesRegex(RuntimeError, 'did not initialize'):
            self.runner.prepare('medium', 1)
        self.assertEqual(self.runner.manifest['preparation'][0]['qualified'], False)

    def test_sustained_requires_post_floor_windows_and_cpu_headroom(self):
        self.runner = bench.Runner(args('--profiles', 'sustained'), DEVICE.copy(), self.directory)
        self.runner.save = lambda **kwargs: None
        self.runner.telemetry = lambda **kwargs: None
        self.runner.host_writes = lambda: None
        self.runner.start_deadline()
        callbacks = []
        def fio(options, directory, on_snapshot=None, deadline=None):
            if Path(directory).name == 'fill':
                return job('prepare', 80 * bench.ALIGN)
            shard = 20 * bench.ALIGN
            latest = None
            for minute in range(1, 9):
                self.clock.sleep(60)
                # Five stable windows before the floor cannot qualify.
                nbytes = shard // 2 if minute <= 5 else shard * (minute - 4)
                latest = {f'worker_{i:02d}': job(f'worker_{i:02d}', nbytes, minute * 60000)
                          for i in range(4)}
                callbacks.append(bool(on_snapshot(latest)))
                if callbacks[-1]:
                    break
            return latest
        self.runner.fio = fio
        record = self.runner.prepare('sustained', 1)
        self.assertFalse(record['qualified'])
        self.assertEqual(record['floor_reached_at_s'], 360)
        self.assertEqual(record['conditioning']['window_count'], 2)
        self.assertFalse(any(callbacks))

    def test_sustained_qualifies_only_after_two_post_floor_assessments(self):
        self.runner = bench.Runner(args('--profiles', 'sustained'), DEVICE.copy(), self.directory)
        self.runner.save = lambda **kwargs: None
        self.runner.telemetry = lambda **kwargs: None
        self.runner.host_writes = lambda: None
        self.runner.start_deadline()
        observations = []
        def fio(options, directory, on_snapshot=None, deadline=None):
            if Path(directory).name == 'fill':
                return job('prepare', 80 * bench.ALIGN)
            latest = None
            for minute in range(1, 9):
                self.clock.sleep(60)
                latest = {f'worker_{i:02d}': job(f'worker_{i:02d}',
                          20 * bench.ALIGN * minute, minute * 60000) for i in range(4)}
                observations.append(bool(on_snapshot(latest)))
                if observations[-1]:
                    break
            return latest
        self.runner.fio = fio
        record = self.runner.prepare('sustained', 1)
        self.assertTrue(record['qualified'])
        self.assertEqual(record['conditioning_status'], 'observed_stable')
        self.assertEqual(record['floor_reached_at_s'], 60)
        self.assertEqual(observations, [False] * 6 + [True])
        self.assertEqual(record['conditioning']['consecutive_assessments'], 2)
        self.assertEqual(record['measured_write_bytes'], 80 * bench.ALIGN + 4 * 20 * bench.ALIGN * 7)
        self.assertEqual(record['host_write_bytes'], record['measured_write_bytes'])

    def test_reset_kernel_counter_uses_fill_plus_sustained_exposure(self):
        self.runner = bench.Runner(args('--profiles', 'sustained', '--run-type', 'smoke'),
                                   DEVICE.copy(), self.directory)
        self.runner.save = lambda **kwargs: None
        self.runner.telemetry = lambda **kwargs: None
        first_counter = iter([100])
        self.runner.host_writes = lambda: next(first_counter, 50)
        self.runner.start_deadline()
        def fio(options, directory, on_snapshot=None, deadline=None):
            if Path(directory).name == 'fill':
                return job('prepare', self.runner.occupied_size('sustained'))
            shard = self.runner.occupied_size('sustained') // 4
            return {f'worker_{i:02d}': job(f'worker_{i:02d}', shard, 1000)
                    for i in range(4)}
        self.runner.fio = fio
        record = self.runner.prepare('sustained', 1)
        self.assertEqual(record['host_write_accounting'], 'measured_only_lower_bound')
        self.assertEqual(record['exposure_write_bytes'], self.runner.occupied_size('sustained'))
        self.assertEqual(record['measured_write_bytes'], 2 * self.runner.occupied_size('sustained'))
        self.assertEqual(record['host_write_bytes'], record['measured_write_bytes'])

    def test_sustained_missing_cpu_cannot_qualify(self):
        self.runner = bench.Runner(args('--profiles', 'sustained'), DEVICE.copy(), self.directory)
        self.runner.save = lambda **kwargs: None
        self.runner.telemetry = lambda **kwargs: None
        self.runner.host_writes = lambda: None
        self.runner.start_deadline()
        def fio(options, directory, on_snapshot=None, deadline=None):
            if Path(directory).name == 'fill':
                return job('prepare', 80 * bench.ALIGN)
            latest = None
            for minute in range(1, 9):
                self.clock.sleep(60)
                latest = {f'worker_{i:02d}': job(f'worker_{i:02d}',
                          20 * bench.ALIGN * minute, minute * 60000) for i in range(4)}
                latest['worker_00'].pop('usr_cpu')
                self.assertFalse(on_snapshot(latest))
            return latest
        self.runner.fio = fio
        record = self.runner.prepare('sustained', 1)
        self.assertFalse(record['qualified'])
        self.assertEqual(record['conditioning_status'], 'cpu_unqualified')


class StreamAndSafetyTests(unittest.TestCase):
    def test_json_stream_recovers_warning_braces_and_multiple_objects(self):
        decoder = bench.JSONStream()
        self.assertEqual(decoder.feed('warning {junk}\n{"jobs":['), [])
        self.assertEqual(decoder.feed(']}\n{"jobs":[]}'), [{'jobs': []}, {'jobs': []}])

    def test_expired_deadline_prevents_fio_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = bench.Runner(args(), DEVICE.copy(), Path(temp))
            runner.run_deadline = bench.time.monotonic() - 1
            with mock.patch.object(bench.subprocess, 'Popen', side_effect=AssertionError('launched')):
                with self.assertRaises(bench.BudgetExpired):
                    runner.fio(['--name=x', '--runtime=1s'], Path(temp) / 'x')

    def test_lock_serializes_sibling_partitions(self):
        with tempfile.TemporaryDirectory() as temp:
            sibling = dict(DEVICE, path='/dev/nvme999n1p1')
            with bench.NamespaceLock(DEVICE, Path(temp)):
                with self.assertRaisesRegex(RuntimeError, 'another benchmark'):
                    with bench.NamespaceLock(sibling, Path(temp)):
                        pass

    def test_output_on_target_dependency_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            def command(options, **kwargs):
                if options[0] == 'findmnt':
                    return types.SimpleNamespace(stdout=TARGET + '\n')
                return types.SimpleNamespace(stdout=json.dumps({'blockdevices': [
                    {'name': 'nvme999n1p2', 'path': TARGET}]}))
            device = dict(DEVICE, layout={'blockdevices': [
                {'name': 'nvme999n1', 'path': '/dev/nvme999n1', 'children': [
                    {'name': 'nvme999n1p2', 'path': TARGET}]}]})
            with mock.patch.object(bench, 'command', side_effect=command):
                with self.assertRaisesRegex(RuntimeError, 'outside the tested'):
                    bench.output_outside_target(Path(temp), device)

    def test_truncated_fio_output_is_rejected_and_process_reaped(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = bench.Runner(args('--profiles', 'fresh'), DEVICE.copy(), Path(temp))
            runner.telemetry = lambda **kwargs: None
            class Process:
                pid = 999999
                def poll(self):
                    return 0
                def wait(self, timeout=None):
                    return 0
            def spawn(options, stdout=None, stderr=None, **kwargs):
                stdout.write('{"jobs":[')
                stdout.flush()
                return Process()
            with mock.patch.object(bench.subprocess, 'Popen', side_effect=spawn):
                with self.assertRaisesRegex(RuntimeError, 'truncated fio JSON'):
                    runner.fio(['--name=sample', '--runtime=1s'], Path(temp) / 'sample')
            self.assertIsNone(runner.process)

    def test_new_health_warning_stops_run(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = bench.Runner(args('--profiles', 'fresh'), DEVICE.copy(), Path(temp))
            runner.start_deadline()
            first = {'critical_warning': 0, 'media_errors': 0}
            second = {'critical_warning': 1, 'media_errors': 0}
            replies = [types.SimpleNamespace(returncode=0, stdout=json.dumps(item), stderr='')
                       for item in (first, second)]
            with mock.patch.object(bench.shutil, 'which', return_value='/mock/nvme'), \
                 mock.patch.object(bench, 'command', side_effect=replies):
                runner.telemetry(force=True)
                with self.assertRaisesRegex(RuntimeError, 'health warning'):
                    runner.telemetry(force=True)

    def test_failed_measurement_preserves_partial_schema3_report(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = bench.Runner(args('--profiles', 'fresh', '--trials', '1'), DEVICE.copy(), Path(temp))
            runner.telemetry = lambda **kwargs: None
            runner.host_writes = lambda: None
            def prepared(profile, trial):
                record = dict(id=f't{trial:02d}_{profile}', trial=trial, profile=profile,
                              status='complete', qualified=True, conditioning_status='not_required',
                              relative_dir='preparation/mock', quality=[], host_write_bytes=0)
                runner.manifest['preparation'].append(record)
                return record
            runner.prepare = prepared
            runner.measure = mock.Mock(side_effect=RuntimeError('mock fio failure'))
            with mock.patch.object(bench.signal, 'signal'):
                outcome = runner.run()
            self.assertEqual(outcome, 1)
            self.assertEqual(runner.manifest['status'], 'failed')
            self.assertEqual(runner.manifest['points'][0]['status'], 'failed')
            self.assertTrue((Path(temp) / 'manifest.json').exists())
            self.assertTrue((Path(temp) / 'report.html').exists())
            self.assertTrue((Path(temp) / 'result.txt').exists())


class MainFakeRunner(bench.Runner):
    """Keep real main/run/prepare/measure paths; replace only raw fio I/O."""
    instances = []
    mode = 'normal'
    clock = None
    child_check = None

    def __init__(self, *values):
        super().__init__(*values)
        self.instances.append(self)

    def telemetry(self, force=False):
        pass

    def host_writes(self):
        return None

    def fio(self, options, directory, on_snapshot=None, deadline=None):
        if self.child_check:
            self.child_check()
        directory = Path(directory)
        names = [value.split('=', 1)[1] for value in options if value.startswith('--name=')]
        settings = {value.split('=', 1)[0]: value.split('=', 1)[1]
                    for value in options if value.startswith('--') and '=' in value}
        if directory.name == 'capability_check':
            if self.mode == 'capability_failure':
                raise RuntimeError('fio NUMA memory binding failed')
            return job(names[0], 0, 1000, reads=4096)
        if directory.name == 'fill':
            size = int(settings['--size'])
            if self.mode == 'fill_local_cap':
                self.last_job = job(names[0], size // 2)
                self.clock.value = deadline + 1
                raise bench.BudgetExpired('preparation fill cap')
            return job(names[0], size)
        if directory.name == 'conditioning':
            if self.mode == 'global_conditioning_cap':
                self.last_job = job(names[0], 4096)
                self.clock.value = self.run_deadline + 1
                raise bench.BudgetExpired('global conditioning cap')
            return job(names[0], int(settings.get('--io_size', 4096)))
        if directory.name == 'run_01' and self.mode == 'cancel_measure':
            raise InterruptedError('user cancellation')
        if directory.name == 'run_02' and self.mode == 'fail_second_repeat':
            raise RuntimeError('second local repeat failed')
        rw = settings.get('--rw', 'read')
        measured_ms = self.args.measurement_time * 1000
        reads = 4096 * 100 if rw in ('read', 'randread') else 4096 * 70 if rw in ('rw', 'randrw') else 0
        writes = 4096 * 30 if rw in ('rw', 'randrw') else 4096 * 100 if rw in ('write', 'randwrite') else 0
        result = {name: job(name, writes, measured_ms, reads=reads) for name in names}
        final = result[names[0]] if len(names) == 1 else result
        self.last_job = final
        return final


class MainLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name, 'results')
        MainFakeRunner.instances = []
        MainFakeRunner.mode = 'normal'
        MainFakeRunner.clock = None
        self.current_affinity = {0, 1}
        self.affinity_changes = []
        self.commands = []
        self.inspections = []
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(bench, 'Runner', MainFakeRunner))
        self.stack.enter_context(mock.patch.object(bench, 'NamespaceLock',
                                                   side_effect=lambda device: contextlib.nullcontext()))
        self.stack.enter_context(mock.patch.object(bench, 'output_outside_target'))
        self.stack.enter_context(mock.patch.object(bench.shutil, 'which', return_value='/mock/tool'))
        self.stack.enter_context(mock.patch.object(bench.os, 'geteuid', return_value=0))
        self.stack.enter_context(mock.patch.object(bench.os, 'sched_getaffinity',
                                                   side_effect=lambda pid: set(self.current_affinity)))
        self.stack.enter_context(mock.patch.object(bench.os, 'sched_setaffinity', side_effect=self.set_affinity))
        self.stack.enter_context(mock.patch.object(bench, 'inspect_device', side_effect=self.inspect))
        self.stack.enter_context(mock.patch.object(bench, 'command', side_effect=self.command))
        self.stack.enter_context(mock.patch.object(bench.signal, 'signal'))
        MainFakeRunner.child_check = self.check_child_affinity

    def set_affinity(self, pid, pool):
        self.current_affinity = set(pool)
        self.affinity_changes.append(set(pool))

    def check_child_affinity(self):
        self.assertEqual(self.current_affinity, set(DEVICE['placement']['cpu_pool']))

    def inspect(self, options):
        self.inspections.append(options)
        return DEVICE.copy()

    def command(self, options, **kwargs):
        self.commands.append(options)
        if options[0] in ('fio', 'blkdiscard'):
            self.check_child_affinity()
        return types.SimpleNamespace(returncode=0, stdout='', stderr='')

    def invoke(self, *extra):
        return bench.main(['--filename', TARGET, '--allow-partition', '--numa-node', '1',
                           '--profiles', 'fresh', '--trials', '1', '--run-type', 'smoke',
                           '--results-dir', str(self.output), '--yes', *extra])

    def test_full_main_binds_before_children_and_restores_after_run(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.affinity_changes[0], set(DEVICE['placement']['cpu_pool']))
        self.assertEqual(self.affinity_changes[-1], {0, 1})
        self.assertEqual(self.current_affinity, {0, 1})
        self.assertEqual(len([cmd for cmd in self.commands if cmd[0] == 'blkdiscard']), 1)
        self.assertEqual(MainFakeRunner.instances[0].manifest['status'], 'complete')
        self.assertEqual(len([p for p in MainFakeRunner.instances[0].manifest['points']
                              if p['status'] == 'complete']), 7)
        self.assertTrue((self.output / 'manifest.json').exists())

    def test_unsupported_fio_numa_policy_fails_before_discard_and_restores_affinity(self):
        ordinary = self.command
        def unsupported(options, **kwargs):
            if options[0] == 'fio':
                self.assertIn('--numa_mem_policy=bind:1', options)
                self.check_child_affinity()
                raise RuntimeError('unsupported numa_mem_policy')
            return ordinary(options, **kwargs)
        with mock.patch.object(bench, 'command', side_effect=unsupported):
            with self.assertRaisesRegex(RuntimeError, 'unsupported numa'):
                self.invoke()
        self.assertEqual(self.current_affinity, {0, 1})
        self.assertFalse(self.output.exists())
        self.assertFalse(any(cmd[0] == 'blkdiscard' for cmd in self.commands))

    def test_unavailable_numa_node_fails_before_mutation(self):
        with mock.patch.object(bench, 'inspect_device', side_effect=RuntimeError('no online permitted CPUs')):
            with self.assertRaisesRegex(RuntimeError, 'no online permitted'):
                self.invoke()
        self.assertFalse(self.affinity_changes)
        self.assertFalse(self.commands)

    def test_identity_change_after_capability_parse_stops_before_discard(self):
        values = [DEVICE.copy(), dict(DEVICE, serial='CHANGED')]
        with mock.patch.object(bench, 'inspect_device', side_effect=values):
            with self.assertRaisesRegex(RuntimeError, 'identity/layout changed'):
                self.invoke()
        self.assertEqual(self.current_affinity, {0, 1})
        self.assertFalse(self.output.exists())
        self.assertFalse(any(cmd[0] == 'blkdiscard' for cmd in self.commands))

    def test_capability_failure_after_parse_stops_before_discard_with_report(self):
        MainFakeRunner.mode = 'capability_failure'
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.invoke(), 1)
        self.assertFalse(any(cmd[0] == 'blkdiscard' for cmd in self.commands))
        self.assertEqual(self.current_affinity, {0, 1})
        self.assertEqual(MainFakeRunner.instances[0].manifest['capability_check']['status'], 'failed')
        self.assertTrue((self.output / 'manifest.json').exists())

    def test_local_fill_deadline_stops_before_measurement(self):
        clock = FakeClock()
        MainFakeRunner.mode = 'fill_local_cap'
        MainFakeRunner.clock = clock
        with mock.patch.object(bench.time, 'monotonic', clock.monotonic), \
             mock.patch.object(bench.time, 'sleep', clock.sleep), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.invoke(), 1)
        manifest = MainFakeRunner.instances[0].manifest
        self.assertEqual(manifest['status'], 'timed_out')
        self.assertEqual(manifest['preparation'][0]['phase'], 'fill')
        self.assertTrue(all(point['status'] == 'pending' for point in manifest['points']))
        self.assertEqual(self.current_affinity, {0, 1})

    def test_global_conditioning_deadline_is_not_exploratory_success(self):
        clock = FakeClock()
        MainFakeRunner.mode = 'global_conditioning_cap'
        MainFakeRunner.clock = clock
        with mock.patch.object(bench.time, 'monotonic', clock.monotonic), \
             mock.patch.object(bench.time, 'sleep', clock.sleep), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.invoke('--profiles', 'medium', '--max-run-time', '20s'), 1)
        manifest = MainFakeRunner.instances[0].manifest
        self.assertEqual(manifest['status'], 'timed_out')
        self.assertFalse(manifest['preparation'][0]['qualified'])
        self.assertTrue(all(point['status'] == 'pending' for point in manifest['points']))
        self.assertEqual(self.current_affinity, {0, 1})

    def test_cancellation_finalizes_partial_sample_and_manifest(self):
        MainFakeRunner.mode = 'cancel_measure'
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.invoke(), 1)
        manifest = MainFakeRunner.instances[0].manifest
        self.assertEqual(manifest['status'], 'interrupted')
        self.assertEqual(manifest['points'][0]['runs'][0]['status'], 'interrupted')
        self.assertEqual(json.loads((self.output / 'manifest.json').read_text())['status'], 'interrupted')
        self.assertEqual(self.current_affinity, {0, 1})

    def test_second_local_repeat_failure_retains_first_complete_metrics(self):
        MainFakeRunner.mode = 'fail_second_repeat'
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.invoke(), 1)
        point = MainFakeRunner.instances[0].manifest['points'][0]
        self.assertEqual([run['status'] for run in point['runs']], ['complete', 'failed'])
        self.assertEqual(point['metrics']['run_count'], 1)
        self.assertEqual(point['status'], 'failed')
        self.assertTrue((self.output / 'raw-runs.csv').exists())

    def test_dry_run_has_full_schedule_and_no_mutation(self):
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            self.assertEqual(self.invoke('--dry-run'), 0)
        self.assertIn('"schedule"', captured.getvalue())
        self.assertFalse(self.output.exists())
        self.assertFalse(self.affinity_changes)
        self.assertFalse(self.commands)


class DeviceSafetyTests(unittest.TestCase):
    """Drive discovery operates exclusively on a fabricated namespace tree."""

    def setUp(self):
        self.args = bench.parse_args(['--filename', TARGET, '--allow-partition', '--numa-node', '0'])
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
        if path.endswith('node0/cpulist') or path.endswith('/cpu/online'):
            return '0-3'
        if path.endswith('/physical_package_id'):
            return '0'
        if path.endswith('/core_id'):
            return '0' if 'cpu0/' in path or 'cpu1/' in path else '1'
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
                '--time_based=1', '--rate_iops=10000', '--output-format=json+',
                '--numa_mem_policy=bind:1', '--cpus_allowed=32']

    def telemetry(self, force=False):
        pass

    def fio(self, args, directory, on_snapshot=None, deadline=None):
        # Each --name starts another fio job, so never supply a duplicate name.
        if not any(arg.startswith('--name=') for arg in args):
            args = ['--name=null-stream-test', *args]
        return super().fio(args, directory, on_snapshot, deadline)


@unittest.skipUnless(shutil.which('fio') and {32, 33} <= os.sched_getaffinity(0),
                     'fio and NUMA1 CPUs 32-33 are required for diskless integration')
class RealNullEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='nvme-null-stream-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.runner = NullRunner(args('--profiles', 'fresh'), DEVICE.copy(), self.directory)

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

    def test_multiworker_null_jobs_have_shared_interval_and_all_artifacts(self):
        from profile_metrics import window_metrics
        cmd = ['--runtime=2s', '--status-interval=1',
               '--name=worker_00', '--cpus_allowed=32', '--rw=randread', '--bs=4K', '--iodepth=2', '--size=1M',
               '--name=worker_01', '--cpus_allowed=33', '--rw=randread', '--bs=4K', '--iodepth=2', '--size=1M']
        final = self.runner.fio(cmd, self.directory / 'multi')
        self.assertEqual(set(final), {'worker_00', 'worker_01'})
        metrics = window_metrics(None, final, expected_names=['worker_00', 'worker_01'])
        self.assertEqual(metrics['read']['io_count'], sum(j['read']['total_ios'] for j in final.values()))
        self.assertEqual(metrics['read']['sample_count'], sum(sum(int(n) for n in j['read']['clat_ns']['bins'].values()) for j in final.values()))
        self.assertEqual(set(metrics['workers']), set(final))
        self.assertEqual(json.loads((self.directory / 'multi' / 'final-job.json').read_text()), final)
        self.assertTrue((self.directory / 'multi' / 'fio.stdout').stat().st_size)


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
