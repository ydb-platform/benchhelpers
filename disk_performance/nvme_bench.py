#!/usr/bin/env python3
"""NVMe profile orchestration. All target mutations live behind validated execution."""
import argparse
import datetime as dt
import contextlib
import contextvars
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import warnings

PROFILES = ['fresh', 'medium', 'sustained']
PROFILE_ALIASES = {'used-recovered': 'medium', 'sustained-steady': 'sustained'}
READ_PERCENTAGES = [100, 95, 90, 80, 70, 50, 30, 10, 0]
BALANCED_RATIOS = [100, 90, 70, 50, 0]
ALIGN = 1024 * 1024
COMPARISON_ANCHORS = [
    ('sr1m_qd64', 'sequential', 1048576, 64, 100),
    ('rr4k_qd1', 'random', 4096, 1, 100),
    ('rr4k_qd64', 'random', 4096, 64, 100),
    ('rr8k_qd64', 'random', 8192, 64, 100),
    ('rw4k_qd32', 'random', 4096, 32, 70),
    ('wr4k_qd64', 'random', 4096, 64, 0),
    ('wr4k_qd1', 'random', 4096, 1, 0),
]
TRIAL_ORDERS = [('fresh', 'medium', 'sustained'),
                ('medium', 'sustained', 'fresh'),
                ('sustained', 'fresh', 'medium')]
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
    p.add_argument('--occupancy-percent', type=int, default=80, help='identical initialized occupancy for every profile')
    p.add_argument('--size-percent', type=int, default=None, help='measurement range percentage; default occupancy-percent, cannot exceed it')
    p.add_argument('--protocol', choices=['comparison', 'sweep'], default=None)
    p.add_argument('--matrix', choices=['balanced', 'full'], default=None, help='sweep matrix; implies --protocol sweep')
    p.add_argument('--trials', type=positive_int, default=None)
    p.add_argument('--seed', type=positive_int, default=20260909)
    p.add_argument('--workers', type=int, choices=[1, 2, 4], default=4)
    p.add_argument('--numa-node', type=int, default=0)
    p.add_argument('--run-count', type=positive_int, default=None, help='local repeats per workload')
    p.add_argument('--ramp-time', type=nonnegative_duration, default=None, help='warmup excluded from each sample')
    p.add_argument('--recovery-time', type=nonnegative_duration, default=60, help='medium idle recovery, included in preparation limit')
    p.add_argument('--medium-conditioning-time', type=nonnegative_duration, default=120, help='medium overwrite cap; also limited to 5%% of occupied bytes')
    p.add_argument('--measurement-time', type=duration, default=None, help='measured seconds per repeat, all profiles')
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
    explicit = {item.split('=', 1)[0] for item in argv}
    if args.protocol is None:
        args.protocol = 'sweep' if '--matrix' in explicit else 'comparison'
        if '--matrix' in explicit:
            print('--matrix without --protocol selects --protocol sweep (migration)', file=sys.stderr)
    if args.protocol == 'comparison' and any(x in explicit for x in ('--matrix', '--read-percentages', '--saturation-probe', '--use-aio', '--polling')):
        p.error('matrix and specialized sweep options require --protocol sweep')
    args.matrix = args.matrix or 'balanced'
    args.trials = args.trials or (3 if args.protocol == 'comparison' else 1)
    if args.prepare_only and '--trials' not in explicit:
        args.trials = 1
    if args.protocol == 'sweep' and args.trials != 1:
        p.error('sweep supports one evolving-state pass; use comparison for independent trials')
    args.run_count = args.run_count or (1 if args.protocol == 'comparison' else 5)
    args.ramp_time = 5 if args.ramp_time is None and args.protocol == 'comparison' else (3 if args.ramp_time is None else args.ramp_time)
    args.measurement_time = 20 if args.measurement_time is None and args.protocol == 'comparison' else (10 if args.measurement_time is None else args.measurement_time)
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
    if args.size_percent > args.occupancy_percent:
        p.error('size-percent cannot exceed occupancy-percent for any profile')
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
        if args.protocol == 'comparison':
            args.trials = 1
    return args


def build_points(args):
    if args.protocol == 'comparison':
        points = []
        for trial in range(1, args.trials + 1):
            for profile in TRIAL_ORDERS[(trial - 1) % len(TRIAL_ORDERS)]:
                if profile not in args.profiles:
                    continue
                prep_id = f't{trial:02d}_{profile}'
                for anchor, pattern, bs, qd, read_pct in COMPARISON_ANCHORS:
                    ident = f'{prep_id}_{anchor}'
                    point = dict(id=ident, trial=trial, anchor=anchor, preparation_id=prep_id,
                                 profile=profile, pattern=pattern, bs=bs, qd=qd, read_pct=read_pct,
                                 status='pending', history=[], runs=[], kind='anchor',
                                 run_count=args.run_count, relative_dir=f'trial_{trial:02d}/{profile}/{anchor}')
                    if anchor == 'wr4k_qd1':
                        point['stage'] = 'after_wr4k_qd64_before_final_read_references'
                    points.append(point)
        return points
    random_qds = ([1, 4, 16, 64] if args.matrix == 'balanced' else [1, 4, 16, 32, 64]) if args.run_type == 'normal' else [1]
    ratios = args.read_percentages
    specs = [('sequential', bs, qd, r) for r in [100, 0]
             for bs in [131072, 1048576] for qd in ([1, 64] if args.run_type == 'normal' else [1])]
    specs += [('random', bs, qd, r) for bs in [4096, 8192] for r in ratios for qd in random_qds]
    # Identical across profiles: pure reads first, increasingly write-heavy groups last.
    specs.sort(key=lambda x: (-x[3], x[0] != 'sequential', x[1], x[2]))
    points = []
    for trial in range(1, args.trials + 1):
        for profile in args.profiles:
            prep_id = f't{trial:02d}_{profile}'
            for pattern, bs, qd, read_pct in specs:
                anchor = f'{pattern}_{bs}_qd{qd}_r{read_pct}'
                ident = f'{prep_id}_{anchor}'
                points.append(dict(id=ident, trial=trial, anchor=anchor, preparation_id=prep_id,
                                   profile=profile, pattern=pattern, bs=bs, qd=qd,
                                   read_pct=read_pct, status='pending', history=[], runs=[],
                                   kind='matrix', run_count=args.run_count,
                                   relative_dir=f'trial_{trial:02d}/{profile}/{anchor}'))
            if args.saturation_probe:
                for bs in [4096, 8192]:
                    for read_pct in [100, 0]:
                        anchor = f'saturation_{bs}_qd256_r{read_pct}'
                        ident = f'{prep_id}_{anchor}'
                        points.append(dict(id=ident, trial=trial, anchor=anchor, preparation_id=prep_id,
                                           profile=profile, pattern='random', bs=bs, qd=256,
                                           read_pct=read_pct, status='pending', history=[], runs=[],
                                           kind='saturation', run_count=1,
                                           relative_dir=f'trial_{trial:02d}/{profile}/{anchor}'))
    return points


def build_schedule(args, points):
    """Materialize all preparation and observation identities before device work."""
    by_prep = {}
    for point in points:
        by_prep.setdefault(point['preparation_id'], []).append(point)
    schedule, references = [], []
    for trial in range(1, args.trials + 1):
        order = TRIAL_ORDERS[(trial - 1) % len(TRIAL_ORDERS)] if args.protocol == 'comparison' else args.profiles
        for profile in order:
            if profile not in args.profiles:
                continue
            prep_id = f't{trial:02d}_{profile}'
            schedule.append(dict(kind='preparation', trial=trial, profile=profile, id=prep_id))
            if args.prepare_only:
                continue
            def sweep_reference(stage):
                ident = f'{prep_id}_reference_{stage}'
                ref = dict(id=ident, trial=trial, anchor='rr4k_qd1', stage=stage,
                           preparation_id=prep_id, profile=profile, pattern='random', bs=4096,
                           qd=1, read_pct=100, status='pending', runs=[], kind='reference',
                           run_count=1, relative_dir=f'trial_{trial:02d}/{profile}/reference_{stage}')
                references.append(ref)
                schedule.append(dict(kind='reference', trial=trial, profile=profile, id=ident,
                                     preparation_id=prep_id, anchor='rr4k_qd1', stage=stage))
            if args.protocol == 'sweep':
                sweep_reference('early')
            for point in by_prep.get(prep_id, []):
                schedule.append(dict(kind='observation', trial=trial, profile=profile, id=point['id'],
                                     preparation_id=prep_id, anchor=point['anchor'],
                                     **({'stage': point['stage']} if point.get('stage') else {})))
            if args.protocol == 'comparison':
                for anchor, _, bs, qd, read_pct in (COMPARISON_ANCHORS[1], COMPARISON_ANCHORS[2]):
                    ident = f'{prep_id}_reference_{anchor}'
                    ref = dict(id=ident, trial=trial, anchor=anchor, preparation_id=prep_id,
                               profile=profile, pattern='random', bs=bs, qd=qd, read_pct=read_pct,
                               status='pending', history=[], runs=[], kind='reference', run_count=1,
                               relative_dir=f'trial_{trial:02d}/{profile}/reference_{anchor}')
                    references.append(ref)
                    schedule.append(dict(kind='reference', trial=trial, profile=profile, id=ident,
                                         preparation_id=prep_id, anchor=anchor))
            else:
                # Sweep retains the historical QD1 read drift references.
                sweep_reference('late')
    return schedule, references


def sample_seed(base, trial, anchor, worker, repeat):
    key = f'{base}:{trial}:{anchor}:{worker}:{repeat}'.encode('ascii')
    return int.from_bytes(hashlib.sha256(key).digest()[:4], 'big') % (2**31 - 2) + 1


def job_bytes(result, direction):
    if not result:
        return 0
    if 'read' in result:
        return result[direction]['io_bytes']
    return sum(job[direction]['io_bytes'] for job in result.values())


def job_runtime(result):
    if 'read' in result:
        return result.get('job_runtime', 0)
    return max((job.get('job_runtime', 0) for job in result.values()), default=0)


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


def numa_placement(node, allowed=None):
    """Choose distinct physical cores in the requested online/permitted node."""
    if node < 0:
        raise RuntimeError('NUMA node must be nonnegative')
    root = Path(f'/sys/devices/system/node/node{node}')
    online = cpulist(read_text(root / 'cpulist'))
    if not online or read_text(root / 'cpulist') is None:
        raise RuntimeError(f'NUMA node {node} is unavailable')
    permitted = set(os.sched_getaffinity(0) if allowed is None else allowed)
    online_cpus = set(cpulist(read_text('/sys/devices/system/cpu/online')))
    pool = sorted(permitted & set(online) & online_cpus)
    if not pool:
        raise RuntimeError(f'no online permitted CPUs in NUMA node {node}')
    physical = []
    seen = set()
    for cpu in pool:
        core = (read_text(f'/sys/devices/system/cpu/cpu{cpu}/topology/physical_package_id'),
                read_text(f'/sys/devices/system/cpu/cpu{cpu}/topology/core_id'))
        if core in seen:
            continue
        seen.add(core)
        physical.append(cpu)
    return dict(numa_node=node, cpu_pool=pool, physical_cpus=physical,
                memory_policy=f'bind:{node}', permitted_cpus=sorted(permitted))


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
    placement = numa_placement(args.numa_node)
    cpu = placement['physical_cpus'][0]
    poll_cpu = placement['physical_cpus'][1] if len(placement['physical_cpus']) > 1 else None
    if args.polling and poll_cpu is None:
        raise RuntimeError('polling needs a second available physical CPU')
    info = dict(path=str(path), namespace=f'/dev/{namespace}', controller=f'/dev/{controller}',
                partition=partition, size_bytes=size, namespace_size_bytes=ns_size,
                partition_fraction=size / ns_size, start_bytes=start, logical_sector_bytes=logical,
                major_minor=f'{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}',
                model=read_text(ctrl_sys / 'model'), serial=read_text(ctrl_sys / 'serial'),
                firmware=read_text(ctrl_sys / 'firmware_rev'), numa_node=numa,
                cpu=cpu, poll_cpu=poll_cpu, layout=tree, placement=placement,
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
        points = build_points(args)
        schedule, references = build_schedule(args, points)
        self.manifest = dict(schema_version=3, started_at=now(), status='running',
                             runner_pid=os.getpid(),
                             config=vars(args).copy(), device=device, points=points, schedule=schedule,
                             warnings=[], telemetry=[], preparation=[], references=references)
        if args.protocol == 'comparison':
            self.manifest['config']['comparison_protocol_revision'] = 2
        self.manifest['config']['queue_depths'] = sorted({p['qd'] for p in self.manifest['points'] if p['kind'] == 'matrix' and p['pattern'] == 'random'})
        if args.run_type == 'smoke':
            self.manifest['config']['read_percentages'] = [100, 50, 0]
            self.manifest['warnings'].append('SMOKE: reduced ranges and durations; not a performance comparison')
        if device['partition']:
            self.manifest['warnings'].append('Partition test: occupancy is a percentage of the partition; other namespace state is uncontrolled')
        if not shutil.which('nvme'):
            self.manifest['warnings'].append('nvme-cli unavailable: SMART and health monitoring unavailable')
        self.manifest['warnings'].append('Profiles describe preparation state; ordered writes change the device. Stability describes only the conditioning workload, not universal SSD/SNIA steady state.')
        self.prepare_number = 0

    def minimum_writes(self):
        prep = [s for s in self.manifest['schedule'] if s['kind'] == 'preparation']
        initialization = sum(self.occupied_size(s['profile']) for s in prep)
        medium = sum(((self.occupied_size(s['profile']) * 5 + 99) // 100 + 4095) // 4096 * 4096
                     for s in prep if s['profile'] == 'medium')
        sustained = sum(self.occupied_size(s['profile']) for s in prep if s['profile'] == 'sustained')
        return dict(preparation_bytes=initialization, medium_write_target_bytes=medium,
                    sustained_write_floor_bytes=sustained,
                    total_bytes=initialization + medium + sustained,
                    note='Initialization is mandatory per preparation; sustained requires at least one occupied-range volume. Medium targets five percent of occupied bytes within 120 seconds. Additional stability and measurement writes are not included.')

    def preparation_limit(self, profile):
        return self.args.max_conditioning_time if profile == 'sustained' else self.args.max_preparation_time

    def budget(self):
        profiles = {}
        prep_schedule = [s for s in self.manifest['schedule'] if s['kind'] == 'preparation']
        for profile in self.args.profiles:
            points = [] if self.args.prepare_only else [p for p in self.manifest['points'] if p['profile'] == profile]
            refs = [] if self.args.prepare_only else [p for p in self.manifest['references'] if p['profile'] == profile]
            samples = sum(p['run_count'] for p in points)
            ref_samples = sum(p['run_count'] for p in refs)
            profile_preps = sum(s['profile'] == profile for s in prep_schedule)
            profiles[profile] = dict(case_count=len(points), sample_count=samples,
                measurement_work_s=samples * (self.args.ramp_time + self.args.measurement_time),
                measurement_overhead_allowance_s=2 * (samples + ref_samples),
                reference_work_s=ref_samples * (self.args.ramp_time + self.args.measurement_time),
                preparation_cap_s=profile_preps * self.preparation_limit(profile),
                occupied_bytes=self.occupied_size(profile), measurement_bytes=self.size(profile),
                preparation_count=profile_preps)
        result = {key: sum(p[key] for p in profiles.values()) for key in
                  ('case_count', 'sample_count', 'measurement_work_s', 'measurement_overhead_allowance_s', 'reference_work_s', 'preparation_cap_s')}
        result.update(profiles=profiles, preparation_count=len(prep_schedule),
                      reference_count=len(self.manifest['references']),
                      minimum_initialization_bytes=self.minimum_writes()['preparation_bytes'])
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
        return self.range_size(self.args.occupancy_percent)

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
                  '--randrepeat=1', f'--randseed={self.args.seed}', '--refill_buffers=1',
                  '--scramble_buffers=1', '--verify=0', '--norandommap=1',
                  '--iodepth_batch_submit=1', '--iodepth_batch_complete_min=1',
                  '--percentile_list=50:90:95:99:99.9', '--lat_percentiles=1', '--clat_percentiles=1',
                  '--output-format=json+', f"--numa_mem_policy=bind:{self.args.numa_node}"]
        if self.args.polling:
            result += ['--hipri=1', '--sqthread_poll=1', '--registerfiles=1',
                       f"--sqthread_poll_cpu={self.device['poll_cpu']}"]
        return result

    def worker_layout(self, pattern, qd, size, requested=None):
        count = 1 if pattern == 'sequential' else min(self.args.workers if requested is None else requested, qd)
        cores = self.device.get('placement', {}).get('physical_cpus', [self.device['cpu']])
        if len(cores) < count:
            raise RuntimeError(f'NUMA node {self.args.numa_node} has {len(cores)} physical cores; {count} workers required')
        if size % (count * 4096):
            raise RuntimeError('eligible range cannot be divided into equal aligned worker shards')
        shard = size // count
        depths = [qd // count + (1 if i < qd % count else 0) for i in range(count)]
        return [dict(worker=i, name=f'worker_{i:02d}', cpu=cores[i], qd=depths[i],
                     offset=i * shard if pattern == 'random' else 0,
                     size=shard if pattern == 'random' else size) for i in range(count)]

    def schedule_preview(self):
        points = {p['id']: p for p in self.manifest['points'] + self.manifest['references']}
        preview = []
        for entry in self.manifest['schedule']:
            row = entry.copy()
            if entry['kind'] == 'preparation':
                occupied = self.occupied_size(entry['profile'])
                row.update(occupied_bytes=occupied,
                           measurement_bytes=self.size(entry['profile']),
                           preparation_cap_s=self.preparation_limit(entry['profile']),
                           fill=dict(pattern='sequential_write', bs=1048576, qd=32,
                                     cpu=self.device['cpu'], size=occupied))
                if entry['profile'] == 'medium':
                    row['conditioning'] = dict(pattern='random_write',
                                               required_write_bytes=((occupied * 5 + 99) // 100 + 4095) // 4096 * 4096,
                                               time_cap_s=self.args.medium_conditioning_time,
                                               recovery_s=self.args.recovery_time)
                elif entry['profile'] == 'sustained':
                    workers = self.worker_layout('random', 128, occupied, requested=4)
                    for worker in workers:
                        worker['seed'] = sample_seed(self.args.seed, entry['trial'], 'condition', worker['worker'], 1)
                    row['conditioning'] = dict(pattern='random_write', aggregate_qd=128,
                                               minimum_write_bytes=occupied, workers=workers,
                                               time_cap_s=self.preparation_limit('sustained'))
            else:
                point = points[entry['id']]
                row.update(bs=point['bs'], qd=point['qd'], read_pct=point['read_pct'],
                           pattern=point['pattern'], relative_dir=point['relative_dir'],
                           samples=[dict(index=index,
                                         workers=self.workload_command(point, index,
                                                                       self.directory / point['relative_dir'] / f'run_{index:02d}')[1])
                                    for index in range(1, point['run_count'] + 1)])
            preview.append(row)
        return preview

    def workload_command(self, point, repeat, directory):
        size = self.size(point['profile'])
        layout = self.worker_layout(point['pattern'], point['qd'], size)
        rw = 'read' if point['read_pct'] == 100 else ('write' if point['read_pct'] == 0 else 'rw')
        if point['pattern'] == 'random':
            rw = 'rand' + rw
        cmd = []
        for worker in layout:
            seed = sample_seed(self.args.seed, point['trial'], point['anchor'], worker['worker'], repeat)
            worker['seed'] = seed
            cmd += [f"--name={worker['name']}", f'--rw={rw}',
                    f"--rwmixread={point['read_pct']}", f"--bs={point['bs']}",
                    f"--iodepth={worker['qd']}", f"--iodepth_batch_complete_max={worker['qd']}",
                    f"--cpus_allowed={worker['cpu']}", f'--randseed={seed}',
                    f"--offset={worker['offset']}", f"--size={worker['size']}",
                    '--time_based=1', f'--runtime={self.args.measurement_time}s',
                    f'--ramp_time={self.args.ramp_time}s', '--status-interval=30',
                    '--log_avg_msec=1000', '--bwavgtime=1000', '--iopsavgtime=1000',
                    f'--write_bw_log={directory / ("series_bw_" + worker["name"])}',
                    f'--write_iops_log={directory / ("series_iops_" + worker["name"])}']
        return cmd, layout

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
        names = [arg.split('=', 1)[1] for arg in args if arg.startswith('--name=')]
        expected = names if names else None
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
                            validated = snapshot(payload, expected_names=expected)
                            last = validated[names[0]] if len(names) == 1 else validated
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

    def prepare(self, profile, trial=1):
        """Reset and fully initialize one trial/profile before any observations."""
        self.check_budget()
        self.last_job = None
        self.prepare_number += 1
        prep_id = f't{trial:02d}_{profile}'
        directory = self.directory / 'preparation' / prep_id
        directory.mkdir(parents=True)
        began = time.monotonic()
        deadline = began + self.preparation_limit(profile)
        record = dict(id=prep_id, trial=trial, profile=profile, size_bytes=self.occupied_size(profile),
                      measurement_bytes=self.size(profile), started_at=now(), status='running',
                      conditioning_status='not_requested', host_write_bytes=0, phase='validate', quality=[], qualified=False,
                      budget_s=self.preparation_limit(profile),
                      relative_dir=str(directory.relative_to(self.directory)))
        record['required_write_bytes'] = (record['size_bytes'] if profile == 'sustained' else
                                          record['size_bytes'] * 5 // 100 if profile == 'medium' else 0)
        record['exposure_write_bytes'] = 0
        self.manifest['preparation'].append(record)
        before = self.host_writes()
        completed_bytes = 0
        phase_prior = 0
        self.save(report=True)

        def progress(job):
            record['elapsed_s'] = time.monotonic() - began
            record['remaining_s'] = max(0, deadline - time.monotonic())
            self.record_writes(record, before, phase_prior + job_bytes(job, 'write'))
            if record['phase'] == 'fill':
                record['fill_fraction'] = job_bytes(job, 'write') / record['size_bytes']
                rate = job_bytes(job, 'write') / max(.001, job_runtime(job) / 1000)
                record['estimated_fill_remaining_s'] = (record['size_bytes'] - job_bytes(job, 'write')) / rate if rate else None
            self.save()
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
                final = self.fio(['--name=prepare', f"--cpus_allowed={self.device['cpu']}", '--rw=write', '--bs=1M', '--iodepth=32',
                                  '--iodepth_batch_complete_max=32', f'--size={record["size_bytes"]}',
                                  '--status-interval=30'], directory / 'fill', progress, deadline=deadline)
                completed_bytes = job_bytes(final, 'write')
                if completed_bytes != record['size_bytes']:
                    raise RuntimeError('sequential preparation did not initialize its entire range')
                record['fill_fraction'] = 1
                self.check_budget()
                if profile == 'fresh':
                    record['qualified'] = True
                    record['conditioning_status'] = 'not_required'
                elif profile == 'medium':
                    phase_prior = completed_bytes
                    record['phase'] = 'conditioning'
                    self.last_job = None
                    target = ((record['size_bytes'] * 5 + 99) // 100 + 4095) // 4096 * 4096
                    record['required_write_bytes'] = target
                    seconds = max(0, min(self.args.medium_conditioning_time,
                                         math.floor(deadline - time.monotonic() - self.args.recovery_time)))
                    if self.args.run_type == 'smoke':
                        seconds = min(seconds, 2)
                    record['conditioning_status'] = 'time_cap_reached'
                    if seconds:
                        self.last_job = None
                        try:
                            final = self.fio(['--name=condition', f"--cpus_allowed={self.device['cpu']}",
                                              '--rw=randwrite', '--bs=4096', '--iodepth=32',
                                              '--iodepth_batch_complete_max=32', f'--size={record["size_bytes"]}',
                                              f'--io_size={target}', f'--runtime={seconds}s', '--status-interval=30'],
                                             directory / 'conditioning', progress, deadline=deadline)
                            record['exposure_write_bytes'] = job_bytes(final, 'write')
                        except BudgetExpired:
                            self.check_budget()
                            record['exposure_write_bytes'] = job_bytes(self.last_job, 'write')
                    record['conditioning_status'] = ('exposure_complete' if record['exposure_write_bytes'] >= target
                                                     else 'time_cap_reached')
                    record['phase'] = 'recovery'
                    recovery_began = time.monotonic()
                    end = min(recovery_began + self.args.recovery_time, deadline)
                    while time.monotonic() < end:
                        self.check_budget()
                        self.telemetry()
                        time.sleep(min(1, max(0, end - time.monotonic())))
                    record['recovery_elapsed_s'] = time.monotonic() - recovery_began
                    record['recovery_status'] = ('complete' if record['recovery_elapsed_s'] >= self.args.recovery_time - .01
                                                 else 'time_cap_reached')
                    record['qualified'] = (record['conditioning_status'] == 'exposure_complete' and
                                           record['recovery_status'] == 'complete')
                else:
                    from profile_metrics import window_metrics, check_stability
                    phase_prior = completed_bytes
                    record['phase'] = 'conditioning'
                    self.last_job = None
                    layout = self.worker_layout('random', 128, record['size_bytes'], requested=4)
                    if len(layout) != 4:
                        raise RuntimeError('sustained conditioning requires four physical worker cores')
                    names = [worker['name'] for worker in layout]
                    cmd = []
                    for worker in layout:
                        cmd += [f"--name={worker['name']}", '--rw=randwrite', '--bs=4096', '--iodepth=32',
                                '--iodepth_batch_complete_max=32', f"--cpus_allowed={worker['cpu']}",
                                f"--offset={worker['offset']}", f"--size={worker['size']}",
                                f"--randseed={sample_seed(self.args.seed, trial, 'condition', worker['worker'], 1)}",
                                '--time_based=1']
                    seconds = max(0, math.floor(deadline - time.monotonic()))
                    if self.args.run_type == 'smoke':
                        seconds = min(seconds, 2)
                    windows, prior, streak, saw_cpu_unqualified = [], None, 0, False
                    def condition_progress(jobs):
                        nonlocal prior, streak, saw_cpu_unqualified
                        elapsed = job_runtime(jobs)
                        prev_elapsed = job_runtime(prior) if prior else 0
                        interval = 1000 if self.args.run_type == 'smoke' else 60000
                        volume_ok = all(jobs[name]['write']['io_bytes'] >= layout[i]['size']
                                        for i, name in enumerate(names))
                        if volume_ok and prior is None:
                            # Conditioning windows begin only after every shard
                            # has met its own write-volume floor.
                            prior = jobs
                            record['floor_reached_at_s'] = elapsed / 1000
                        elif volume_ok and elapsed - prev_elapsed > interval * 1.1:
                            # A delayed status snapshot is not a one-minute
                            # window; start a new sequence at this baseline.
                            prior = jobs
                            windows.clear()
                            streak = 0
                            record['conditioning'] = {'stable': False, 'reason': 'status interval gap',
                                                      'elapsed_s': elapsed / 1000,
                                                      'consecutive_assessments': 0, 'window_count': 0}
                        elif volume_ok and elapsed - prev_elapsed >= interval * .9:
                            window = window_metrics(prior, jobs, expected_names=names,
                                                    interval_ms=elapsed - prev_elapsed)
                            windows.append(window)
                            windows[:] = windows[-5:]
                            prior = jobs
                            cpu_ok = all(all(w.get('cpu_user') is not None and w.get('cpu_system') is not None and
                                             w['cpu_user'] + w['cpu_system'] < 90
                                             for w in candidate.get('workers', {}).values()) and
                                         len(candidate.get('workers', {})) == 4 for candidate in windows)
                            saw_cpu_unqualified = saw_cpu_unqualified or not cpu_ok
                            assessment = check_stability(windows, 0)
                            passing = len(windows) == 5 and assessment['stable'] and cpu_ok
                            streak = streak + 1 if passing else 0
                            record['conditioning'] = dict(**assessment, consecutive_assessments=streak,
                                                           elapsed_s=elapsed / 1000, worker_cpu_qualified=cpu_ok,
                                                           volume_qualified=volume_ok, queue_depth_aggregate=128,
                                                           worker_queue_depth=32, window_count=len(windows))
                            record['worker_exposure_bytes'] = {name: jobs[name]['write']['io_bytes'] for name in names}
                            record['exposure_write_bytes'] = sum(record['worker_exposure_bytes'].values())
                            if streak >= 2:
                                record['conditioning_status'] = 'observed_stable'
                                record['qualified'] = True
                        progress(jobs)
                        return record['qualified']
                    record['conditioning_status'] = 'time_cap_reached'
                    if seconds:
                        self.last_job = None
                        try:
                            final = self.fio([f'--runtime={seconds}s',
                                              f'--status-interval={1 if self.args.run_type == "smoke" else 60}'] + cmd,
                                             directory / 'conditioning', condition_progress, deadline=deadline)
                            record['worker_exposure_bytes'] = {name: final[name]['write']['io_bytes'] for name in names}
                            record['exposure_write_bytes'] = sum(record['worker_exposure_bytes'].values())
                        except BudgetExpired:
                            self.check_budget()
                            partial = self.last_job or {}
                            record['worker_exposure_bytes'] = {name: partial.get(name, {}).get('write', {}).get('io_bytes', 0)
                                                               for name in names}
                            record['exposure_write_bytes'] = sum(record['worker_exposure_bytes'].values())
                    if not record['qualified']:
                        if not all(record.get('worker_exposure_bytes', {}).get(name, 0) >= layout[i]['size']
                                   for i, name in enumerate(names)):
                            record['conditioning_status'] = 'volume_unmet'
                        elif saw_cpu_unqualified:
                            record['conditioning_status'] = 'cpu_unqualified'
                if record['conditioning_status'] == 'time_cap_reached':
                    record['quality'].append('Preparation time cap reached; requested conditioning was not completed')
                if record['conditioning_status'] in ('volume_unmet', 'cpu_unqualified'):
                    record['quality'].append(f"Sustained conditioning {record['conditioning_status'].replace('_', ' ')}")
                if record.get('recovery_status') == 'time_cap_reached':
                    record['quality'].append('Preparation time cap reached; medium recovery was shortened')
                record.update(status='complete', phase='done', finished_at=now())
        except BaseException as exc:
            record['error'] = str(exc)
            raise
        finally:
            # Track fill and conditioning as separate cumulative fio jobs.
            # The kernel counter is preferred, but fallback totals must include
            # all completed and partial conditioning writes exactly once.
            if record['phase'] == 'fill':
                completed_bytes = max(completed_bytes, job_bytes(self.last_job, 'write'))
            conditioning_bytes = record['exposure_write_bytes']
            if profile != 'fresh' and record['phase'] in ('conditioning', 'recovery', 'done'):
                conditioning_bytes = max(conditioning_bytes, job_bytes(self.last_job, 'write'))
            record['exposure_write_bytes'] = conditioning_bytes
            self.record_writes(record, before, completed_bytes + conditioning_bytes)
            record['elapsed_s'] = time.monotonic() - began
            record['headroom_s'] = self.preparation_limit(profile) - record['elapsed_s']
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
            cmd, layout = self.workload_command(point, index, directory)
            seed = [worker['seed'] for worker in layout]
            sample = dict(index=index, status='running', relative_dir=relative, seed=seed,
                          worker_layout=layout, requested_aggregate_qd=point['qd'], started_at=now(),
                          log_intervals=dict(mode='averaged_contiguous', average_ms=1000,
                                             origin_ms=0, time_reference='post_ramp_measurement_epoch',
                                             ramp_time_ms=self.args.ramp_time * 1000,
                                             directions=(['read'] if point['read_pct'] == 100 else
                                                         ['write'] if point['read_pct'] == 0 else ['read', 'write'])))
            point['runs'].append(sample)
            start, before = time.monotonic(), self.host_writes()
            self.last_job = None
            self.save()
            final = None
            try:
                final = self.fio(cmd, directory)
                # A clean early exit is still not a complete requested sample.
                jobs = final if len(layout) > 1 else {layout[0]['name']: final}
                if any(job_runtime(job) < self.args.measurement_time * 1000 * .95 for job in jobs.values()):
                    raise RuntimeError('fio sample ended before its requested measurement duration')
                metrics = (window_metrics(None, final, expected_names=[worker['name'] for worker in layout])
                           if len(layout) > 1 else window_metrics(None, final))
                sample.update(status='complete', metrics=metrics)
            except BaseException as exc:
                sample.update(status='interrupted' if isinstance(exc, (InterruptedError, KeyboardInterrupt)) else
                              ('timed_out' if isinstance(exc, BudgetExpired) else 'failed'), error=str(exc))
                point['status'] = sample['status']
                point['quality'].append(f'Only {index - 1}/{point.get("run_count", self.args.run_count)} repeats completed')
                raise
            finally:
                final = final or self.last_job
                self.record_writes(sample, before, job_bytes(final, 'write'))
                sample['ramp_write_bytes'] = (max(0, sample['host_write_bytes'] - sample['measured_write_bytes'])
                                              if sample['host_write_accounting'] == 'target_kernel_counter' else None)
                sample['ramp_write_accounting'] = ('kernel_total_minus_measured_estimate' if sample['ramp_write_bytes'] is not None
                                                   else 'unavailable')
                sample.update(elapsed_s=time.monotonic() - start, finished_at=now())
                successful = [run['metrics'] for run in point['runs'] if run['status'] == 'complete']
                if successful:
                    point['metrics'] = aggregate_runs(successful)
                    if any((worker.get('cpu_user') or 0) + (worker.get('cpu_system') or 0) >= 90
                           for worker in point['metrics'].get('workers', {}).values()) or (not point['metrics'].get('workers') and
                           sum(point['metrics'].get(k, 0) or 0 for k in ('cpu_user', 'cpu_system')) >= 90):
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

    def reference(self, point):
        self.measure(point)
        if self.args.protocol == 'comparison':
            anchor = next((p for p in self.manifest['points'] if p['trial'] == point['trial'] and
                           p['profile'] == point['profile'] and p['anchor'] == point['anchor']), None)
        else:
            anchor = next((p for p in self.manifest['references'] if p['trial'] == point['trial'] and
                           p['profile'] == point['profile'] and p.get('stage') == 'early'), None)
        if anchor and anchor is not point and anchor.get('metrics'):
            initial = anchor['metrics']['read']['iops']
            point['iops_change_percent'] = (point['metrics']['read']['iops'] / initial - 1) * 100 if initial else None
            point['note'] = 'Single local reference drift indicator; no equivalence or steady-state claim.'
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
                    self.fio(['--name=capability_check', f"--cpus_allowed={self.device['cpu']}", '--readonly', '--rw=read', '--bs=4K',
                              '--iodepth=1', '--iodepth_batch_complete_max=1', '--size=1M',
                              '--time_based=1', '--runtime=1s'], self.directory / 'capability_check')
                    active.update(status='complete', finished_at=now())
                    active = None
                completed, prep, prior_writes, prior_exact = 0, None, 0, True
                point_by_id = {p['id']: p for p in self.manifest['points'] + self.manifest['references']}
                for entry in self.manifest['schedule']:
                    self.check_budget()
                    if entry['kind'] == 'preparation':
                        active = None
                        prep = self.prepare(entry['profile'], entry['trial'])
                        prior_writes = 0
                        prior_exact = self.host_writes() is not None
                        continue
                    point = point_by_id[entry['id']]
                    active = point
                    completed += 1
                    point['preparation'] = prep['relative_dir']
                    point['conditioning_status'] = prep['conditioning_status']
                    point['recovery_status'] = prep.get('recovery_status')
                    point['preparation_qualified'] = prep['qualified']
                    point['quality'] = list(prep.get('quality', []))
                    if not prep['qualified']:
                        point['quality'].append('Exploratory: preparation did not qualify')
                    point['prior_write_bytes'] = prior_writes
                    point['prior_write_accounting'] = ('target_kernel_counter' if prior_exact
                                                       else 'measured_only_lower_bound')
                    point['profile_measurement_writes_before_bytes'] = prior_writes
                    print(f'[{completed}/{len(point_by_id)}] trial {entry["trial"]} {entry["profile"]} {point["anchor"]} '
                          f'({point["run_count"]} × {self.args.measurement_time}s + {self.args.ramp_time}s ramp)', flush=True)
                    if entry['kind'] == 'reference':
                        self.reference(point)
                    else:
                        self.measure(point)
                    prior_writes += point.get('host_write_bytes', 0)
                    prior_exact = prior_exact and point.get('host_write_accounting') == 'target_kernel_counter'
                    metrics = point['metrics']
                    print(f'  read {metrics["read"]["iops"]:.0f} / write {metrics["write"]["iops"]:.0f} IOPS', flush=True)
                active = None
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
    required_cores = max(4 if 'sustained' in args.profiles else 1,
                         args.workers if any(p['pattern'] == 'random' and p['qd'] >= args.workers
                                             for p in probe.manifest['points']) else 1)
    if len(device['placement']['physical_cpus']) < required_cores:
        raise RuntimeError(f'NUMA node {args.numa_node} has only {len(device["placement"]["physical_cpus"])} distinct physical cores; {required_cores} required')
    writes = probe.minimum_writes()
    budget = probe.budget()
    probe.manifest['budget'] = budget
    probe.manifest['planned_minimum_writes'] = writes
    test_count = 0 if args.prepare_only else len(probe.manifest['points'])
    if args.dry_run:
        print(json.dumps(dict(device=device, config=vars(args), test_count=test_count, minimum_writes=writes, budget=budget,
                              schedule=probe.schedule_preview(), output=str(directory)), indent=2))
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
        print(f"Exactly {budget['preparation_count']} preparations; {writes['preparation_bytes'] / 1e12:.3f} TB initialization; "
              f"{writes['total_bytes'] / 1e12:.3f} TB minimum planned fill plus exposure. ", flush=True)
        print(f"Whole-run device-work cap: {budget['run_cap_s'] / 3600:.2f} h (not an ETA; preparation can finish early). "
              'Cleanup and final report generation follow. A cap may leave an incomplete report.', flush=True)
        print(f'Results: {directory}', flush=True)
    if args.dry_run:
        print('Dry run: no target or result files changed.')
        return 0
    if os.geteuid() != 0:
        raise RuntimeError('run this command with sudo; it requires raw-device access')
    with NamespaceLock(device):
        original_affinity = os.sched_getaffinity(0)
        os.sched_setaffinity(0, set(device['placement']['cpu_pool']))
        try:
            command(probe.fio_base() + ['--name=validate', f"--cpus_allowed={device['cpu']}",
                                        '--rw=read', '--size=1M', '--parse-only'])
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
        finally:
            os.sched_setaffinity(0, original_affinity)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f'Error: {error}', file=sys.stderr)
        raise SystemExit(1)
