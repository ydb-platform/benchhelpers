#!/usr/bin/env python3
"""NVMe profile orchestration. All target mutations live behind validated execution."""
import argparse
import datetime as dt
import contextlib
import contextvars
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import time

PROFILES = ['fresh', 'medium', 'sustained']
PROFILE_ALIASES = {'used-recovered': 'medium', 'sustained-steady': 'sustained'}
READ_PERCENTAGES = [100, 95, 90, 80, 70, 50, 30, 10, 0]
BALANCED_RATIOS = [100, 90, 70, 50, 0]
ALIGN = 1024 * 1024
COMMAND_DEADLINE = contextvars.ContextVar('nvme_command_deadline', default=None)
COMMAND_CANCELLED = contextvars.ContextVar('nvme_command_cancelled', default=None)


@contextlib.contextmanager
def command_budget(deadline, cancelled=None):
    previous = COMMAND_DEADLINE.get()
    token = COMMAND_DEADLINE.set(min(previous, deadline) if previous is not None else deadline)
    cancel_token = COMMAND_CANCELLED.set(cancelled or COMMAND_CANCELLED.get())
    try:
        yield
    finally:
        COMMAND_DEADLINE.reset(token)
        COMMAND_CANCELLED.reset(cancel_token)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def command(args, optional=False, timeout=15):
    cancelled = COMMAND_CANCELLED.get()
    if cancelled and cancelled():
        raise InterruptedError('interrupted before command')
    deadline = COMMAND_DEADLINE.get()
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BudgetExpired('command budget exhausted')
        timeout = min(timeout, remaining)
    process = subprocess.Popen(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        if cancelled is None:
            stdout, stderr = process.communicate(timeout=timeout)
        else:
            end = time.monotonic() + timeout
            while True:
                if cancelled():
                    raise InterruptedError('interrupted during command')
                try:
                    stdout, stderr = process.communicate(timeout=max(.001, min(.2, end - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= end:
                        raise
    except BaseException as exc:
        # communicate after kill is also bounded: kernel-stuck I/O cannot be reaped on demand.
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        try:
            process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f'command PID {process.pid} remains after SIGKILL; kernel I/O may be stuck') from exc
        raise
    result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    if result.returncode and not optional:
        raise RuntimeError(f"{' '.join(map(str, args))}: {result.stderr.strip() or result.stdout.strip()}")
    return result


def duration(value):
    match = re.fullmatch(r'(\d+)([smhd]?)', value)
    if not match or int(match[1]) <= 0:
        raise argparse.ArgumentTypeError('expected a positive duration such as 60s, 5m, 1h')
    return int(match[1]) * {'': 1, 's': 1, 'm': 60, 'h': 3600, 'd': 86400}[match[2]]


def nonnegative_duration(value):
    return 0 if value in ('0', '0s') else duration(value)


def positive_int(value):
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError('must be at least 1')
    return result


def percentages(value):
    try:
        values = [int(part) for part in value.split(',')]
    except ValueError:
        raise argparse.ArgumentTypeError('expected comma-separated integer read percentages')
    if not values or len(values) != len(set(values)) or any(v < 0 or v > 100 for v in values):
        raise argparse.ArgumentTypeError('read percentages must be unique integers in [0,100]')
    return sorted(values, reverse=True)


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument('--filename', required=True, help='NVMe target; ALL DATA ON THIS TARGET WILL BE LOST')
    p.add_argument('--profiles', default=','.join(PROFILES), help='comma-separated profiles to run')
    p.add_argument('--occupancy-percent', type=int, default=80, help='occupied percentage of target for medium AND sustained')
    p.add_argument('--size-percent', type=int, default=None, help='measurement range percentage; default occupancy-percent, cannot exceed it')
    p.add_argument('--matrix', choices=['balanced', 'full'], default='balanced', help='48 or 98 cases per profile before custom ratios')
    p.add_argument('--run-count', type=positive_int, default=5, help='repeated samples per matrix case')
    p.add_argument('--ramp-time', type=nonnegative_duration, default=3, help='warmup excluded from each sample')
    p.add_argument('--recovery-time', type=nonnegative_duration, default=60, help='medium idle recovery, included in preparation limit')
    p.add_argument('--medium-conditioning-time', type=nonnegative_duration, default=120, help='medium overwrite cap; also limited to 5%% of occupied bytes')
    p.add_argument('--measurement-time', type=duration, default=10, help='measured seconds per repeat, all profiles')
    p.add_argument('--runtime', type=duration, help='alias for --measurement-time')
    p.add_argument('--max-preparation-time', type=duration, default=1800, help='fresh/medium preparation limit INCLUDING discard, fill and recovery')
    p.add_argument('--max-conditioning-time', type=duration, default=3600, help='sustained preparation limit INCLUDING discard and fill')
    p.add_argument('--max-run-time', type=duration, default=None, help='whole device-work deadline; default finite sum of preparation caps and measurement schedule with overhead')
    p.add_argument('--saturation-probe', action='store_true', help='add four bounded single-sample random QD256 diagnostics per profile')
    p.add_argument('--read-percentages', type=percentages, default=None, help='override preset random read-operation percentages, e.g. 100,70,50,0')
    p.add_argument('--results-dir', default=None, help='new or empty directory outside the target NVMe; default timestamped directory')
    p.add_argument('--prefix', default='nvme', help='report title label')
    p.add_argument('--allow-partition', action='store_true', help='allow partition target with uncontrolled state outside it')
    p.add_argument('--yes', action='store_true', help='skip destructive confirmation')
    p.add_argument('--dry-run', action='store_true', help='read-only device checks and configuration preview')
    p.add_argument('--use-aio', action='store_true', help='use libaio instead of io_uring')
    p.add_argument('--polling', action='store_true', help='enable io_uring device/SQ polling; needs a second physical CPU')
    p.add_argument('--clean-device', action='store_true', help='deprecated alias for --profiles fresh')
    p.add_argument('--run-type', choices=['normal', 'smoke'], default='normal', help='smoke reduces the matrix/range but still discards the entire target')
    p.add_argument('--prepare-only', action='store_true', help=argparse.SUPPRESS)
    return p


def parse_args(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    p = parser()
    args = p.parse_args(argv)
    if args.runtime is not None:
        if any(arg.split('=', 1)[0] == '--measurement-time' for arg in argv):
            p.error('use either --runtime or --measurement-time')
        args.measurement_time = args.runtime
    if args.clean_device:
        if any(arg.split('=', 1)[0] == '--profiles' for arg in argv):
            p.error('--clean-device conflicts with --profiles')
        print('Deprecated --clean-device: use --profiles fresh', file=sys.stderr)
        args.profiles = 'fresh'
    args.profiles = [PROFILE_ALIASES.get(x, x) for x in args.profiles.split(',')]
    if len(set(args.profiles)) != len(args.profiles) or any(x not in PROFILES for x in args.profiles):
        p.error('profiles must be a unique comma-separated subset of ' + ','.join(PROFILES))
    args.profiles = [x for x in PROFILES if x in args.profiles]
    if not 1 <= args.occupancy_percent <= 100:
        p.error('occupancy-percent must be in [1,100]')
    if args.size_percent is None:
        args.size_percent = args.occupancy_percent
    if not 1 <= args.size_percent <= 100:
        p.error('size-percent must be in [1,100]')
    if any(x != 'fresh' for x in args.profiles) and args.size_percent > args.occupancy_percent:
        p.error('size-percent cannot exceed occupancy-percent for medium/sustained')
    if args.read_percentages is None:
        args.read_percentages = list(BALANCED_RATIOS if args.matrix == 'balanced' else READ_PERCENTAGES)
    if args.polling and args.use_aio:
        p.error('--polling requires io_uring')
    if args.prepare_only and len(args.profiles) != 1:
        p.error('standalone preparation requires exactly one --profiles value')
    if args.run_type == 'smoke':
        args.measurement_time = 2
        args.run_count = 2
        args.ramp_time = 0
        args.medium_conditioning_time = 1
        args.recovery_time = 1
        args.max_preparation_time = 60
        args.max_conditioning_time = 60
        args.read_percentages = [100, 50, 0]
    return args


def build_points(args):
    random_qds = ([1, 4, 16, 64] if args.matrix == 'balanced' else [1, 4, 16, 32, 64]) if args.run_type == 'normal' else [1]
    ratios = args.read_percentages
    specs = [('sequential', bs, qd, r) for r in [100, 0]
             for bs in [131072, 1048576] for qd in ([1, 64] if args.run_type == 'normal' else [1])]
    specs += [('random', bs, qd, r) for bs in [4096, 8192] for r in ratios for qd in random_qds]
    # Identical across profiles: pure reads first, increasingly write-heavy groups last.
    specs.sort(key=lambda x: (-x[3], x[0] != 'sequential', x[1], x[2]))
    points = []
    for profile in args.profiles:
        for pattern, bs, qd, read_pct in specs:
            ident = f'{pattern}_{bs}_qd{qd}_r{read_pct}'
            points.append(dict(id=ident, profile=profile, pattern=pattern, bs=bs, qd=qd,
                               read_pct=read_pct, status='pending', history=[], runs=[],
                               kind='matrix', run_count=args.run_count,
                               relative_dir=f'{profile}/{ident}'))
        if args.saturation_probe:
            for bs in [4096, 8192]:
                for read_pct in [100, 0]:
                    ident = f'saturation_{bs}_qd256_r{read_pct}'
                    points.append(dict(id=ident, profile=profile, pattern='random', bs=bs, qd=256,
                                       read_pct=read_pct, status='pending', history=[], runs=[],
                                       kind='saturation', run_count=1, relative_dir=f'{profile}/{ident}'))
    return points


def flatten(nodes):
    for node in nodes:
        yield node
        yield from flatten(node.get('children', []))


def read_text(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def cpulist(value):
    result = []
    for part in (value or '').split(','):
        if not part:
            continue
        ends = part.split('-')
        result.extend(range(int(ends[0]), int(ends[-1]) + 1))
    return result


def inspect_device(args):
    path = Path(args.filename).resolve(strict=True)
    st = path.stat()
    if not stat.S_ISBLK(st.st_mode) or not re.fullmatch(r'nvme\d+n\d+(p\d+)?', path.name):
        raise RuntimeError('target must be an NVMe namespace or explicitly allowed NVMe partition')
    partition = bool(re.search(r'p\d+$', path.name))
    if partition and not args.allow_partition:
        raise RuntimeError('partition target requires --allow-partition; other namespace state is uncontrolled')
    namespace = re.sub(r'p\d+$', '', path.name)
    ns_sys = Path('/sys/class/block') / namespace
    tree = json.loads(command(['lsblk', '-b', '-J', '-o',
                              'NAME,PATH,TYPE,SIZE,FSTYPE,MOUNTPOINT,PKNAME,MAJ:MIN',
                              f'/dev/{namespace}']).stdout)
    nodes = list(flatten(tree['blockdevices']))
    # Check the whole namespace tree, including otherwise untested siblings.
    for node in nodes:
        if node.get('mountpoint'):
            raise RuntimeError(f"mounted device in target namespace: {node['path']}")
        holders = Path('/sys/class/block') / node['name'] / 'holders'
        if list(holders.iterdir()):
            raise RuntimeError(f"active holders on {node['path']}")
        result = command(['fuser', '-s', node['path']], optional=True)
        if result.returncode not in (0, 1):
            raise RuntimeError(f"cannot check device users: {node['path']}: {result.stderr}")
        if result.returncode == 0:
            raise RuntimeError(f"device open by another process: {node['path']}")
    swaps = command(['swapon', '--show', '--noheadings', '--raw', '--output', 'NAME']).stdout.splitlines()
    device_paths = {str(Path(n['path']).resolve()) for n in nodes}
    if any(str(Path(s).resolve()) in device_paths for s in swaps):
        raise RuntimeError('swap is active on the target namespace')
    size = int(command(['blockdev', '--getsize64', str(path)]).stdout)
    ns_size = int(next(n['size'] for n in nodes if n['name'] == namespace))
    logical = int(command(['blockdev', '--getss', str(path)]).stdout)
    if logical > 4096 or size < ALIGN:
        raise RuntimeError('target sector size/capacity cannot support this test matrix')
    start = int(read_text(Path('/sys/class/block') / path.name / 'start') or 0) * 512
    if start % 4096:
        raise RuntimeError('partition start must be 4 KiB aligned')
    controller = re.match(r'nvme\d+', namespace)[0]
    ctrl_sys = Path('/sys/class/nvme') / controller
    numa = read_text(ctrl_sys / 'device/numa_node')
    allowed = sorted(os.sched_getaffinity(0))
    local = cpulist(read_text(f'/sys/devices/system/node/node{numa}/cpulist')) if numa and numa != '-1' else []
    candidates = [cpu for cpu in allowed if cpu in local] or allowed
    cpu = candidates[0]
    siblings = cpulist(read_text(f'/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list'))
    poll_cpu = next((c for c in candidates if c not in siblings), None)
    if args.polling and poll_cpu is None:
        raise RuntimeError('polling needs a second available physical CPU')
    info = dict(path=str(path), namespace=f'/dev/{namespace}', controller=f'/dev/{controller}',
                partition=partition, size_bytes=size, namespace_size_bytes=ns_size,
                partition_fraction=size / ns_size, start_bytes=start, logical_sector_bytes=logical,
                major_minor=f'{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}',
                model=read_text(ctrl_sys / 'model'), serial=read_text(ctrl_sys / 'serial'),
                firmware=read_text(ctrl_sys / 'firmware_rev'), numa_node=numa,
                cpu=cpu, poll_cpu=poll_cpu, layout=tree,
                scheduler=read_text(ns_sys / 'queue/scheduler'),
                write_cache=read_text(ns_sys / 'queue/write_cache'),
                link_speed=read_text(ctrl_sys / 'device/current_link_speed'),
                link_width=read_text(ctrl_sys / 'device/current_link_width'),
                cpu_governor=read_text(f'/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_governor'),
                fio_version=command(['fio', '--version']).stdout.strip(), kernel=os.uname().release)
    for name, cmd in [('identify_controller', ['nvme', 'id-ctrl', info['controller'], '-o', 'json']),
                      ('identify_namespace', ['nvme', 'id-ns', str(path), '-o', 'json']),
                      ('power_state', ['nvme', 'get-feature', info['controller'], '-f', '2', '-H']),
                      ('volatile_write_cache', ['nvme', 'get-feature', info['controller'], '-f', '6', '-H'])]:
        if shutil.which('nvme'):
            try:
                r = command(cmd, optional=True, timeout=5)
                info[name] = r.stdout.strip() if r.returncode == 0 else {'unavailable': r.stderr.strip()}
            except subprocess.TimeoutExpired:
                info[name] = {'unavailable': 'command timed out'}
    return info


def output_outside_target(directory, device):
    ancestor = directory.resolve()
    while not ancestor.exists():
        ancestor = ancestor.parent
    source = command(['findmnt', '-n', '-o', 'SOURCE', '--target', str(ancestor)]).stdout.strip()
    # Trace filesystem block dependencies, including MD/LVM, by device identity.
    source = source.split('[')[0]
    if source.startswith('/dev/'):
        tree = json.loads(command(['lsblk', '-s', '-J', '-o', 'NAME,PATH', source]).stdout)
        forbidden = {Path(n['path']).name for n in flatten(device['layout']['blockdevices'])}
        if any(n['name'] in forbidden for n in flatten(tree['blockdevices'])):
            raise RuntimeError('results directory must be on storage outside the tested NVMe')


class JSONStream:
    def __init__(self):
        self.buffer = ''

    def feed(self, text):
        self.buffer += text
        values = []
        decoder = json.JSONDecoder()
        while True:
            start = self.buffer.find('{')
            if start < 0:
                self.buffer = ''
                break
            self.buffer = self.buffer[start:]
            try:
                value, end = decoder.raw_decode(self.buffer)
            except json.JSONDecodeError:
                # fio warnings may themselves contain braces. A new unindented
                # object begins a later complete snapshot, not a nested field.
                next_object = self.buffer.find('\n{', 1)
                if next_object >= 0:
                    self.buffer = self.buffer[next_object + 1:]
                    continue
                break
            values.append(value)
            self.buffer = self.buffer[end:]
        return values


class BudgetExpired(TimeoutError):
    """A wall-clock device-work budget was exhausted (not a fio I/O error)."""


class NamespaceLock:
    """Cooperating runners hold this lock throughout preparation, idle and reporting."""
    def __init__(self, device, directory=Path('/run/lock')):
        self.path = directory / ('nvme_bench_' + Path(device['namespace']).name + '.lock')
        self.fd = None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            raise RuntimeError(f'another benchmark holds {self.path}')
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        # Do not unlink: a waiting opener could otherwise hold a different inode.


class Runner:
    def __init__(self, args, device, directory):
        self.args, self.device, self.directory = args, device, directory
        self.process = None
        self.stop = False
        self.last_telemetry = 0
        self.initial_warning = None
        self.run_deadline = None
        self.last_job = None
        self.manifest = dict(schema_version=2, started_at=now(), status='running',
                             runner_pid=os.getpid(),
                             config=vars(args).copy(), device=device, points=build_points(args),
                             warnings=[], telemetry=[], preparation=[], references=[])
        self.manifest['config']['queue_depths'] = sorted({p['qd'] for p in self.manifest['points'] if p['kind'] == 'matrix' and p['pattern'] == 'random'})
        if args.run_type == 'smoke':
            self.manifest['config']['read_percentages'] = [100, 50, 0]
            self.manifest['warnings'].append('SMOKE: reduced ranges and durations; not a performance comparison')
        if device['partition']:
            self.manifest['warnings'].append('Partition test: occupancy is a percentage of the partition; other namespace state is uncontrolled')
        if not shutil.which('nvme'):
            self.manifest['warnings'].append('nvme-cli unavailable: SMART and health monitoring unavailable')
        self.manifest['warnings'].append('Profiles describe the initial state, not an immutable state: writes during the matrix change the device. Not an SNIA steady-state certification.')
        self.prepare_number = 0

    def minimum_writes(self):
        preparation = sum(self.occupied_size(profile) for profile in self.args.profiles)
        return dict(preparation_bytes=preparation, sustained_write_floor_bytes=0,
                    total_bytes=preparation,
                    note='One initialization per profile. Additional bounded conditioning and measurement writes are not included; there is no write-volume minimum.')

    def preparation_limit(self, profile):
        return self.args.max_conditioning_time if profile == 'sustained' else self.args.max_preparation_time

    def budget(self):
        profiles = {}
        for profile in self.args.profiles:
            points = [] if self.args.prepare_only else [p for p in self.manifest['points'] if p['profile'] == profile]
            samples = sum(p['run_count'] for p in points)
            refs = 0 if self.args.prepare_only else 2
            profiles[profile] = dict(case_count=len(points), sample_count=samples,
                measurement_work_s=samples * (self.args.ramp_time + self.args.measurement_time),
                measurement_overhead_allowance_s=2 * (samples + refs),
                reference_work_s=refs * (self.args.ramp_time + self.args.measurement_time),
                preparation_cap_s=self.preparation_limit(profile),
                occupied_bytes=self.occupied_size(profile), measurement_bytes=self.size(profile))
        result = {key: sum(p[key] for p in profiles.values()) for key in
                  ('case_count', 'sample_count', 'measurement_work_s', 'measurement_overhead_allowance_s', 'reference_work_s', 'preparation_cap_s')}
        result.update(profiles=profiles, preparation_count=len(profiles),
                      minimum_initialization_bytes=self.minimum_writes()['total_bytes'])
        automatic = sum(result[k] for k in ('measurement_work_s', 'measurement_overhead_allowance_s', 'reference_work_s', 'preparation_cap_s')) + 300
        result['run_cap_s'] = self.args.max_run_time or automatic
        result['note'] = ('Timing is a schedule, not a throughput prediction. Preparation caps include discard, fill, conditioning and recovery. '
                          'Process/report overhead varies. Deadline stops device work; cleanup may add up to 5 seconds per child, and final report generation follows.')
        return result

    def start_deadline(self):
        if self.run_deadline is None:
            self.manifest['budget'] = self.budget()
            self.run_deadline = time.monotonic() + self.manifest['budget']['run_cap_s']

    def check_budget(self, deadline=None):
        if self.stop:
            raise InterruptedError('interrupted by user')
        limits = [x for x in (self.run_deadline, deadline) if x is not None]
        if limits and time.monotonic() >= min(limits):
            raise BudgetExpired('wall-clock budget exhausted; no more device work will start')

    def remaining(self, deadline=None):
        self.check_budget(deadline)
        limits = [x for x in (self.run_deadline, deadline) if x is not None]
        return min(limits) - time.monotonic() if limits else 15

    def host_writes(self):
        raw = read_text(Path('/sys/class/block') / Path(self.device['path']).name / 'stat')
        try:
            return int(raw.split()[6]) * 512
        except (AttributeError, ValueError, IndexError):
            return None

    def record_writes(self, record, before, measured):
        after = self.host_writes()
        record['measured_write_bytes'] = measured
        available = before is not None and after is not None and after >= before
        record['host_write_bytes'] = after - before if available else measured
        record['host_write_accounting'] = 'target_kernel_counter' if available else 'measured_only_lower_bound'

    def save(self, report=False, plots=False):
        self.manifest['updated_at'] = now()
        temp = self.directory / 'manifest.json.tmp'
        # Histograms make full matrices large. Compact checkpoint encoding keeps
        # per-repeat persistence within the overhead allowance; prettify once
        # at termination, after device work has stopped.
        temp.write_text(json.dumps(self.manifest, indent=2 if plots else None,
                                   separators=None if plots else (',', ':'), allow_nan=False) + '\n')
        temp.replace(self.directory / 'manifest.json')
        if report:
            from profile_report import render_report
            render_report(self.directory, self.manifest, plots=plots)

    def interrupted(self, signum, frame):
        self.stop = True
        if self.process and self.process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGINT)

    def telemetry(self, force=False):
        if not shutil.which('nvme') or (not force and time.monotonic() - self.last_telemetry < 30):
            return
        self.last_telemetry = time.monotonic()
        try:
            result = command(['nvme', 'smart-log', self.device['controller'], '-o', 'json'], optional=True,
                             timeout=max(.01, min(5, self.remaining())))
        except subprocess.TimeoutExpired:
            self.manifest['telemetry'].append({'time': now(), 'unavailable': 'SMART command timed out'})
            return
        entry = {'time': now()}
        try:
            if result.returncode:
                raise ValueError(result.stderr.strip())
            entry['smart'] = json.loads(result.stdout)
        except ValueError as exc:
            entry['unavailable'] = str(exc)
            self.manifest['telemetry'].append(entry)
            return
        smart = entry['smart']
        warning = int(smart.get('critical_warning', 0))
        errors = int(smart.get('media_errors', 0))
        self.manifest['telemetry'].append(entry)
        if self.initial_warning is None:
            self.initial_warning = warning, errors
            if warning:
                raise RuntimeError(f'NVMe reports critical health warning {warning}')
        elif (warning & ~self.initial_warning[0]) or errors > self.initial_warning[1]:
            raise RuntimeError('new NVMe health warning or media error; stopping benchmark')

    def size(self, profile):
        return self.range_size(self.args.size_percent)

    def occupied_size(self, profile):
        return self.size(profile) if profile == 'fresh' else self.range_size(self.args.occupancy_percent)

    def range_size(self, percent):
        size = self.device['size_bytes']
        if self.args.run_type == 'smoke':
            size = min(size, 64 * ALIGN)
        size = size * percent // 100
        size = size // ALIGN * ALIGN
        if size < ALIGN:
            raise RuntimeError('active range rounds below 1 MiB; increase size/occupancy or target size')
        return size

    def fio_base(self):
        result = ['fio', f"--filename={self.device['path']}", '--direct=1', '--numjobs=1',
                  '--thread=1', '--ioengine=' + ('libaio' if self.args.use_aio else 'io_uring'),
                  '--randrepeat=1', '--randseed=20260909', '--refill_buffers=1',
                  '--scramble_buffers=1', '--verify=0', '--norandommap=1',
                  '--iodepth_batch_submit=1', '--iodepth_batch_complete_min=1',
                  '--percentile_list=50:90:95:99:99.9', '--lat_percentiles=1', '--clat_percentiles=1',
                  '--output-format=json+', f"--cpus_allowed={self.device['cpu']}"]
        if self.args.polling:
            result += ['--hipri=1', '--sqthread_poll=1', '--registerfiles=1',
                       f"--sqthread_poll_cpu={self.device['poll_cpu']}"]
        return result

    def fio(self, args, directory, on_snapshot=None, deadline=None):
        from profile_metrics import snapshot
        self.check_budget(deadline)
        options = dict(arg[2:].split('=', 1) for arg in args if arg.startswith('--') and '=' in arg)
        if deadline is None:
            runtime = duration(options.get('runtime', '60s'))
            ramp = nonnegative_duration(options.get('ramp_time', '0'))
            deadline = time.monotonic() + runtime + ramp + 15
        if self.run_deadline is not None:
            deadline = min(deadline, self.run_deadline)
        directory.mkdir(parents=True, exist_ok=True)
        cmd = self.fio_base() + args
        (directory / 'command.json').write_text(json.dumps(cmd, indent=2))
        last, requested, timed_out = None, False, False
        self.last_job = None
        decoder = JSONStream()
        with (directory / 'fio.stdout').open('w') as stdout, (directory / 'fio.stderr').open('w') as stderr:
            self.process = subprocess.Popen(cmd, stdout=stdout, stderr=stderr, start_new_session=True)
            stop_started = None
            with (directory / 'fio.stdout').open() as stream:
                try:
                    while True:
                        running = self.process.poll() is None
                        for payload in decoder.feed(stream.read()):
                            last = snapshot(payload)
                            self.last_job = last
                            if on_snapshot and not requested and on_snapshot(last):
                                requested = True
                                if self.process.poll() is None:
                                    os.killpg(self.process.pid, signal.SIGINT)
                                    stop_started = time.monotonic()
                        timed_out = timed_out or (running and time.monotonic() >= deadline)
                        if not timed_out and not self.stop:
                            self.telemetry()
                        if self.stop or timed_out:
                            if stop_started is None and self.process.poll() is None:
                                os.killpg(self.process.pid, signal.SIGINT)
                                stop_started = time.monotonic()
                        if stop_started is not None and time.monotonic() - stop_started > 5 and running:
                            os.killpg(self.process.pid, signal.SIGKILL)
                            raise RuntimeError('fio did not terminate within 5 seconds; SIGKILL sent')
                        if not running:
                            break
                        time.sleep(.2)
                except BaseException:
                    if self.process.poll() is None:
                        os.killpg(self.process.pid, signal.SIGINT)
                        try:
                            self.process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(self.process.pid, signal.SIGKILL)
                            try:
                                self.process.wait(timeout=1)
                            except subprocess.TimeoutExpired:
                                self.manifest['warnings'].append(f'fio PID {self.process.pid} remains after SIGKILL; kernel I/O may be stuck')
                    raise
            rc = self.process.wait(timeout=1)
            self.process = None
        if decoder.buffer.strip():
            raise RuntimeError(f'truncated fio JSON result: {directory / "fio.stdout"}')
        if last is not None and (rc == 0 or requested or timed_out or self.stop):
            (directory / 'final-job.json').write_text(json.dumps(last, indent=2))
        if self.stop:
            raise InterruptedError('interrupted by user')
        if timed_out:
            raise BudgetExpired('fio wall-clock deadline reached')
        # fio 3.28 can return 128 for a clean internally requested SIGINT stop.
        if rc != 0 and not (requested and rc in (-signal.SIGINT, 128, 128 + signal.SIGINT)):
            raise RuntimeError(f'fio failed ({rc}): {(directory / "fio.stderr").read_text().strip()}')
        if last is None:
            raise RuntimeError(f'no valid fio result: {directory / "fio.stdout"}')
        return last

    def prepare(self, profile, read_pct=None):
        """Exactly one reset and initialization for a profile, never for a case."""
        self.check_budget()
        self.last_job = None
        self.prepare_number += 1
        directory = self.directory / 'preparation' / f'{self.prepare_number:04d}_{profile}'
        directory.mkdir(parents=True)
        began = time.monotonic()
        deadline = began + self.preparation_limit(profile)
        record = dict(profile=profile, size_bytes=self.occupied_size(profile),
                      measurement_bytes=self.size(profile), started_at=now(), status='running',
                      conditioning_status='not_requested', host_write_bytes=0, phase='validate', quality=[],
                      budget_s=self.preparation_limit(profile),
                      relative_dir=str(directory.relative_to(self.directory)))
        self.manifest['preparation'].append(record)
        before = self.host_writes()
        completed_bytes = 0
        phase_prior = 0
        self.save(report=True)

        def progress(job):
            record['elapsed_s'] = time.monotonic() - began
            record['remaining_s'] = max(0, deadline - time.monotonic())
            self.record_writes(record, before, phase_prior + job['write']['io_bytes'])
            if record['phase'] == 'fill':
                record['fill_fraction'] = job['write']['io_bytes'] / record['size_bytes']
                rate = job['write']['io_bytes'] / max(.001, job.get('job_runtime', 0) / 1000)
                record['estimated_fill_remaining_s'] = (record['size_bytes'] - job['write']['io_bytes']) / rate if rate else None
            self.save(report=True)
            print(f'  {profile} {record["phase"]}: {record["elapsed_s"] / 60:.1f} min, '
                  f'{record["host_write_bytes"] / 1e12:.3f} TB written; {record["remaining_s"] / 60:.1f} min budget left', flush=True)
            return False

        try:
            with command_budget(min(deadline, self.run_deadline or deadline), cancelled=lambda: self.stop):
                self.telemetry(force=True)
                checked = inspect_device(self.args)
                if any(checked[k] != self.device[k] for k in ('major_minor', 'serial', 'size_bytes', 'start_bytes')):
                    raise RuntimeError('target device identity/layout changed')
                self.check_budget(deadline)
                record['phase'] = 'discard'
                self.save(report=True)
                print(f'  {profile}: discard {self.device["path"]} once; initialize {record["size_bytes"] / 1e12:.3f} TB once', flush=True)
                command(['blkdiscard', self.device['path']], timeout=self.remaining(deadline))
                record['phase'] = 'fill'
                self.last_job = None
                final = self.fio(['--name=prepare', '--rw=write', '--bs=1M', '--iodepth=32',
                                  '--iodepth_batch_complete_max=32', f'--size={record["size_bytes"]}',
                                  '--status-interval=30'], directory / 'fill', progress, deadline=deadline)
                completed_bytes = final['write']['io_bytes']
                if completed_bytes != record['size_bytes']:
                    raise RuntimeError('sequential preparation did not initialize its entire range')
                record['fill_fraction'] = 1
                self.check_budget()
                if profile != 'fresh' and time.monotonic() >= deadline:
                    record['conditioning_status'] = 'time_cap_reached'
                    if profile == 'medium':
                        record['recovery_status'] = 'time_cap_reached'
                if profile != 'fresh' and time.monotonic() < deadline:
                    phase_prior = completed_bytes
                    self.last_job = None
                    record['phase'] = 'conditioning'
                    interval = 60
                    windows, previous, streak = [], None, 0

                    def condition_progress(job):
                        nonlocal previous, streak
                        from profile_metrics import window_metrics, check_stability
                        elapsed = job.get('job_runtime', 0) / 1000
                        if profile == 'sustained' and (previous is None or elapsed - previous.get('job_runtime', 0) / 1000 >= interval * .9):
                            windows.append(window_metrics(previous, job))
                            windows[:] = windows[-5:]
                            previous = job
                            assessment = check_stability(windows, 0)
                            eligible = elapsed >= 5 * interval
                            streak = streak + 1 if eligible and assessment['stable'] else 0
                            record['conditioning'] = dict(**assessment, consecutive_windows=streak,
                                                           elapsed_s=elapsed, queue_depth=32, block_size=4096)
                            if streak >= 2:
                                record['conditioning_status'] = 'observed_stable'
                        progress(job)
                        return record['conditioning_status'] == 'observed_stable'

                    seconds = int(self.remaining(deadline))
                    if self.args.run_type == 'smoke':
                        seconds = min(seconds, 2)
                    cmd = ['--name=condition', '--rw=randwrite', '--rwmixread=0', '--bs=4096',
                           '--iodepth=32', '--iodepth_batch_complete_max=32', f'--size={record["size_bytes"]}',
                           f'--status-interval={interval if self.args.run_type == "normal" else 1}']
                    if profile == 'medium':
                        # Preserve the recovery allowance instead of spending it on overwrites.
                        seconds = max(0, min(seconds - self.args.recovery_time, self.args.medium_conditioning_time))
                        cap = max(4096, record['size_bytes'] * 5 // 100 // 4096 * 4096)
                        record['conditioning_write_cap_bytes'] = cap
                        # time_based would make fio IGNORE the io_size stopping condition.
                        cmd += [f'--io_size={cap}']
                        record['conditioning_status'] = ('bounded_light' if seconds else
                            'time_cap_reached' if self.args.medium_conditioning_time else 'not_requested')
                    else:
                        cmd += ['--time_based=1']
                        record['conditioning_status'] = 'time_cap_reached'
                    if seconds > 0:
                        try:
                            final = self.fio(cmd + [f'--runtime={seconds}s'], directory / 'conditioning',
                                             condition_progress, deadline=deadline)
                            completed_bytes += final['write']['io_bytes']
                        except BudgetExpired:
                            self.check_budget()  # A global timeout must NOT be downgraded to a qualification.
                            completed_bytes += (self.last_job or {}).get('write', {}).get('io_bytes', 0)
                            record['conditioning_status'] = 'time_cap_reached'
                    if profile == 'medium':
                        record['phase'] = 'recovery'
                        recovery_began = time.monotonic()
                        requested_end = recovery_began + self.args.recovery_time
                        end = min(requested_end, deadline)
                        record['recovery_status'] = 'complete' if requested_end <= deadline else 'time_cap_reached'
                        next_update = recovery_began
                        while time.monotonic() < end:
                            self.check_budget()
                            record['elapsed_s'] = time.monotonic() - began
                            record['recovery_remaining_s'] = end - time.monotonic()
                            self.telemetry()
                            if time.monotonic() >= next_update:
                                self.save(report=True)
                                next_update = time.monotonic() + 10
                            time.sleep(min(1, max(0, end - time.monotonic())))
                        record['recovery_elapsed_s'] = time.monotonic() - recovery_began
                if record['conditioning_status'] == 'time_cap_reached':
                    record['quality'].append('Preparation time cap reached; requested conditioning was not completed')
                if record.get('recovery_status') == 'time_cap_reached':
                    record['quality'].append('Preparation time cap reached; medium recovery was shortened')
                record.update(status='complete', phase='done', finished_at=now())
        except BaseException as exc:
            record['error'] = str(exc)
            raise
        finally:
            if record['status'] == 'running':
                # Include the latest partial fio counters on interruption/error.
                completed_bytes = max(completed_bytes, phase_prior + (self.last_job or {}).get('write', {}).get('io_bytes', 0))
            self.record_writes(record, before, completed_bytes)
            record['elapsed_s'] = time.monotonic() - began
            self.save(report=True)
        return record

    def measure(self, point):
        from profile_metrics import window_metrics, aggregate_runs
        point.update(status='running', baseline=point['profile'])
        point.setdefault('runs', [])
        point.setdefault('quality', [])
        began = time.monotonic()
        for index in range(1, point.get('run_count', self.args.run_count) + 1):
            self.check_budget()
            relative = f'{point["relative_dir"]}/run_{index:02d}'
            directory = self.directory / relative
            # Matched seed schedule across profiles, different offsets for each repeat.
            seed = 20260909 + index
            sample = dict(index=index, status='running', relative_dir=relative, seed=seed, started_at=now())
            point['runs'].append(sample)
            rw = 'read' if point['read_pct'] == 100 else ('write' if point['read_pct'] == 0 else 'rw')
            if point['pattern'] == 'random':
                rw = 'rand' + rw
            cmd = [f'--name={point["id"]}', f'--rw={rw}', f'--rwmixread={point["read_pct"]}',
                   f'--bs={point["bs"]}', f'--iodepth={point["qd"]}',
                   f'--iodepth_batch_complete_max={point["qd"]}', f'--randseed={seed}',
                   f'--size={self.size(point["profile"])}', '--time_based=1',
                   f'--runtime={self.args.measurement_time}s', f'--ramp_time={self.args.ramp_time}s',
                   '--status-interval=30', '--log_avg_msec=1000',
                   f'--write_bw_log={directory / "series"}', f'--write_iops_log={directory / "series"}']
            start, before = time.monotonic(), self.host_writes()
            self.last_job = None
            self.save()
            final = None
            try:
                final = self.fio(cmd, directory)
                # A clean early exit is still not a complete requested sample.
                if final.get('job_runtime', 0) < self.args.measurement_time * 1000 * .95:
                    raise RuntimeError('fio sample ended before its requested measurement duration')
                sample.update(status='complete', metrics=window_metrics(None, final))
            except BaseException as exc:
                sample.update(status='interrupted' if isinstance(exc, (InterruptedError, KeyboardInterrupt)) else
                              ('timed_out' if isinstance(exc, BudgetExpired) else 'failed'), error=str(exc))
                point['status'] = sample['status']
                point['quality'].append(f'Only {index - 1}/{point.get("run_count", self.args.run_count)} repeats completed')
                raise
            finally:
                final = final or self.last_job
                self.record_writes(sample, before, (final or {}).get('write', {}).get('io_bytes', 0))
                sample.update(elapsed_s=time.monotonic() - start, finished_at=now())
                successful = [run['metrics'] for run in point['runs'] if run['status'] == 'complete']
                if successful:
                    point['metrics'] = aggregate_runs(successful)
                    if sum(point['metrics'].get(k, 0) or 0 for k in ('cpu_user', 'cpu_system')) >= 90:
                        warning = 'Worker CPU >=90%; throughput may be host-limited (not proof of a device limit)'
                        if warning not in point['quality']:
                            point['quality'].append(warning)
                point['host_write_bytes'] = sum(r.get('host_write_bytes', 0) for r in point['runs'])
                point['measured_write_bytes'] = sum(r.get('measured_write_bytes', 0) for r in point['runs'])
                point['host_write_accounting'] = ('target_kernel_counter' if all(r.get('host_write_accounting') == 'target_kernel_counter' for r in point['runs'])
                                                  else 'measured_only_lower_bound')
                point['elapsed_s'] = time.monotonic() - began
                # Persist every repeat, but render the growing HTML/CSVs once
                # per case (or at run termination), outside active fio work.
                self.save()
        point['status'] = 'complete'
        self.save(report=True)

    def reference(self, profile, stage):
        point = dict(id=f'reference_{stage}', profile=profile, pattern='random', bs=4096, qd=1,
                     read_pct=100, status='pending', kind='reference', run_count=1, runs=[], history=[],
                     relative_dir=f'{profile}/reference_{stage}')
        self.manifest['references'].append(point)
        self.measure(point)
        if stage == 'late':
            early = next(p for p in self.manifest['references'] if p['profile'] == profile and p['id'] == 'reference_early')
            initial = early['metrics']['read']['iops']
            point['iops_change_percent'] = (point['metrics']['read']['iops'] / initial - 1) * 100 if initial else None
            point['note'] = 'Single-sample read drift indicator only; cannot establish stable write performance throughout the matrix.'
        self.save(report=True)

    def run(self, capability_check=False):
        self.start_deadline()
        handlers = {sig: signal.signal(sig, self.interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
        active = None
        try:
            with command_budget(self.run_deadline, cancelled=lambda: self.stop):
                if self.args.prepare_only:
                    self.manifest['points'] = []
                self.save(report=True)
                if capability_check:
                    active = dict(status='running', started_at=now(), relative_dir='capability_check')
                    self.manifest['capability_check'] = active
                    self.save()
                    # Verify the chosen engine before any discard, within the
                    # same failure/report lifecycle as preparation and samples.
                    self.fio(['--name=capability_check', '--readonly', '--rw=read', '--bs=4K',
                              '--iodepth=1', '--iodepth_batch_complete_max=1', '--size=1M',
                              '--time_based=1', '--runtime=1s'], self.directory / 'capability_check')
                    active.update(status='complete', finished_at=now())
                    active = None
                completed = 0
                for profile in self.args.profiles:
                    active = None
                    prep = self.prepare(profile)
                    if self.args.prepare_only:
                        continue
                    self.reference(profile, 'early')
                    profile_written = 0
                    for point in (p for p in self.manifest['points'] if p['profile'] == profile):
                        active = point
                        self.check_budget()
                        completed += 1
                        print(f'[{completed}/{len(self.manifest["points"])}] {profile} {point["id"]} '
                              f'({point["run_count"]} × {self.args.measurement_time}s + {self.args.ramp_time}s warmup)', flush=True)
                        point['preparation'] = prep['relative_dir']
                        point['conditioning_status'] = prep['conditioning_status']
                        point['recovery_status'] = prep.get('recovery_status')
                        point['quality'] = list(prep.get('quality', []))
                        point['profile_measurement_writes_before_bytes'] = profile_written
                        self.measure(point)
                        profile_written += point['host_write_bytes']
                        metrics = point['metrics']
                        print(f'  median read {metrics["read"]["iops"]:.0f} / write {metrics["write"]["iops"]:.0f} IOPS', flush=True)
                    active = None
                    self.reference(profile, 'late')
                self.telemetry(force=True)
                self.manifest['status'] = 'complete'
        except (Exception, KeyboardInterrupt) as exc:
            status = ('interrupted' if isinstance(exc, (InterruptedError, KeyboardInterrupt)) else
                      'timed_out' if isinstance(exc, (BudgetExpired, subprocess.TimeoutExpired)) else 'failed')
            self.manifest.update(status=status, error=str(exc))
            if active:
                active.update(status=status, error=str(exc), finished_at=now())
            print(f'{status}: {exc}', file=sys.stderr, flush=True)
        finally:
            records = self.manifest['preparation'] + self.manifest['points'] + self.manifest['references']
            for record in records:
                if record['status'] == 'running':
                    record.update(status=self.manifest['status'], finished_at=now(),
                                  error=self.manifest.get('error', 'run stopped'))
            self.manifest['finished_at'] = now()
            try:
                self.save(report=True, plots=True)
            finally:
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
            print(f'{self.manifest["status"]}. Report: {self.directory / "report.html"}', flush=True)
        return 0 if self.manifest['status'] == 'complete' else 1


def main(argv=None):
    args = parse_args(argv)
    for tool in ['fio', 'blkdiscard', 'lsblk', 'blockdev', 'fuser', 'swapon', 'findmnt']:
        if not shutil.which(tool):
            raise RuntimeError(f'missing dependency: {tool}')
    device = inspect_device(args)
    directory = Path(args.results_dir or dt.datetime.now().strftime('%Y%m%d_%H%M%S_results')).resolve()
    output_outside_target(directory, device)
    if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
        raise RuntimeError('results directory must not exist or must be empty')
    probe = Runner(args, device, directory)
    writes = probe.minimum_writes()
    budget = probe.budget()
    probe.manifest['budget'] = budget
    probe.manifest['planned_minimum_writes'] = writes
    test_count = 0 if args.prepare_only else len(probe.manifest['points'])
    if args.dry_run:
        print(json.dumps(dict(device=device, config=vars(args), test_count=test_count, minimum_writes=writes, budget=budget,
                              output=str(directory)), indent=2))
    else:
        print(f"Target: {device['path']} | {device['model']} | serial {device['serial']} | "
              f"{device['size_bytes'] / 1e12:.3f} TB", flush=True)
        print(f"Profiles: {', '.join(args.profiles)} | {test_count} cases | {budget['sample_count']} samples | "
              f"{args.measurement_time}s measured + {args.ramp_time}s warmup each", flush=True)
        for profile, plan in budget['profiles'].items():
            work = plan['measurement_work_s'] + plan['reference_work_s'] + plan['measurement_overhead_allowance_s']
            print(f"  {profile}: {plan['case_count']} cases; ~{work / 60:.1f} min measurements incl. 2s/sample overhead allowance; "
                  f"preparation capped at {plan['preparation_cap_s'] / 60:.0f} min incl. fill; "
                  f"occupied {plan['occupied_bytes'] / 1e12:.3f} TB, measured range {plan['measurement_bytes'] / 1e12:.3f} TB", flush=True)
        print(f"Exactly {budget['preparation_count']} preparations; {writes['total_bytes'] / 1e12:.3f} TB total initialization. "
              'Conditioning and measurements add writes; no per-case write quota.', flush=True)
        print(f"Whole-run device-work cap: {budget['run_cap_s'] / 3600:.2f} h (not an ETA; preparation can finish early). "
              'Cleanup and final report generation follow. A cap may leave an incomplete report.', flush=True)
        print(f'Results: {directory}', flush=True)
    if args.dry_run:
        print('Dry run: no target or result files changed.')
        return 0
    if os.geteuid() != 0:
        raise RuntimeError('run this command with sudo; it requires raw-device access')
    with NamespaceLock(device):
        command(probe.fio_base() + ['--name=validate', '--rw=read', '--size=1M', '--parse-only'])
        if not args.yes:
            answer = input(f'ALL DATA ON {device["path"]} WILL BE LOST. Type its full path to continue: ')
            if answer != device['path']:
                raise RuntimeError('confirmation did not match; nothing changed')
        checked = inspect_device(args)
        if any(checked[k] != device[k] for k in ('major_minor', 'serial', 'size_bytes', 'start_bytes')):
            raise RuntimeError('device identity/layout changed during confirmation')
        directory.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault('MPLCONFIGDIR', str(directory / '.matplotlib'))
        return probe.run(capability_check=True)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f'Error: {error}', file=sys.stderr)
        raise SystemExit(1)
