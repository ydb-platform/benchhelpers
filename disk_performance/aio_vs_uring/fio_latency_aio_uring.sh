#!/bin/bash

filesize=2500G

ramp_time=10s
runtime=1m

long_cooldown_interval_seconds=3600
short_cooldown=10s
long_cooldown=5m

run_type=normal

block_size=4K
run_count=10
clocksource=cpu
format=json
results_dir="$(date +%Y%m%d_%H%M)_results"
fill_disk=0
prefix=""

iodepth_from=1
iodepth_to=128

run_aio=0
run_uring=0
run_uring_iopoll=0
run_uring_sqpoll=0
run_uring_sqpoll_iopoll=0

function usage {
    cat <<EOF
Usage: $0 --filename <path> [options]

Required:
  --filename <path>                fio target file/device

Options:
  --filesize <size>                fio target size (default: $filesize)
  --block-size <size>              fio block size (default: $block_size)
  --ramp-time <time>               fio ramp time (default: $ramp_time)
  --runtime <time>                 fio runtime (default: $runtime)
  --short-cooldown <time>          cooldown after each run (default: $short_cooldown)
  --long-cooldown <time>           cooldown each elapsed hour (default: $long_cooldown)
  --run-type <smoke|normal|long>   run profile (default: $run_type)
  --fill-disk                      run preconditioning fill (default: false)
  --results-dir <path>             directory for fio outputs (default: YYYYMMDD_HHMM_results)
  --run-count <n>                 number of repeated runs per test point (default: $run_count)
  --iodepth-from <n>               iodepth start (default: $iodepth_from)
  --iodepth-to <n>                 iodepth end (default: $iodepth_to)
  --clocksource <name>             fio clock source (default: $clocksource)
  --format <fmt>                   output format for fio files (default: $format)
  --prefix <prefix>                plot filename prefix for aggregate.py (default: empty)

Modes (if none selected, all are run):
  --aio                            libaio mode
  --uring                          io_uring mode
  --uring-iopoll                   io_uring + completion polling (--hipri)
  --uring-sqpoll                   io_uring + SQ polling (--sqthread_poll)
  --uring-sqpoll-iopoll           io_uring + SQ polling + completion polling
EOF
}

size_to_bytes() {
    local size="$1"
    local value unit multiplier

    if [[ "$size" =~ ^([0-9]+)$ ]]; then
        echo "${BASH_REMATCH[1]}"
        return 0
    fi

    if [[ "$size" =~ ^([0-9]+)([KkMmGgTtPp])$ ]]; then
        value="${BASH_REMATCH[1]}"
        unit="${BASH_REMATCH[2]}"
        case "$unit" in
            [Kk]) multiplier=$((1024));;
            [Mm]) multiplier=$((1024**2));;
            [Gg]) multiplier=$((1024**3));;
            [Tt]) multiplier=$((1024**4));;
            [Pp]) multiplier=$((1024**5));;
            *)
                return 1
                ;;
        esac
        echo $((value * multiplier))
        return 0
    fi

    return 1
}

duration_to_seconds() {
    local duration="$1"
    local value unit multiplier

    if [[ ! "$duration" =~ ^([0-9]+)([smhd])$ ]]; then
        return 1
    fi

    value="${BASH_REMATCH[1]}"
    unit="${BASH_REMATCH[2]}"
    case "$unit" in
        s) multiplier=1 ;;
        m) multiplier=60 ;;
        h) multiplier=3600 ;;
        d) multiplier=86400 ;;
        *)
            return 1
            ;;
    esac

    echo $((value * multiplier))
}

function run_fio {
    local iodepth="$1"
    local rw="$2"
    local run_index="$3"
    local fio_test_name="${rw}_latency_test"
    local clock_arg="--clocksource=$clocksource"
    local result_file="$results_dir/${mode_name}_qd${iodepth}_${rw}_run${run_index}.$format"
    local iodepth_batch_submit=1
    local iodepth_batch_complete_max=1

    precondition_target "$mode_name"

    local fio_cmd=(
        sudo fio
        --name="$fio_test_name"
        --filename="$filename" --filesize="$filesize"
        --time_based --ramp_time="$ramp_time" --runtime="$runtime"
        --ioengine="$ioengine"
    )

    if [[ ${#mode_fio_args[@]} -gt 0 ]]; then
        fio_cmd+=("${mode_fio_args[@]}")
    fi

    fio_cmd+=(
        "$clock_arg"
        --direct=1 --verify=0 --randrepeat=0 --randseed=17
        --bs="$block_size" --iodepth="$iodepth" --rw="rand$rw" --iodepth_batch_submit="$iodepth_batch_submit"
        --iodepth_batch_complete_max="$iodepth_batch_complete_max"
        --lat_percentiles=1
        --percentile_list="10:50:90:95:99:99.9"
        --output-format="$format"
        --output="$result_file"
    )

    echo "-------------------------------------------------"
    echo "Running fio test: $mode_name (run $run_index/$run_count)"
    echo "ioengine=$ioengine mode_fio_args='${mode_fio_args[*]}' clock_arg='$clock_arg' bs=$block_size iodepth=$iodepth runtime=$runtime rw=$rw run_index=$run_index batch_submit=$iodepth_batch_submit batch_complete_max=$iodepth_batch_complete_max output=$result_file"
    echo "fio command:"
    printf "  "
    printf "%q " "${fio_cmd[@]}"
    echo
    echo "-------------------------------------------------"

    if ! "${fio_cmd[@]}"; then
        echo "fio command failed for mode=$mode_name iodepth=$iodepth rw=$rw run_index=$run_index"
        exit 1
    fi

    # Some fio modes may prepend warning lines before JSON payload.
    # Keep only the JSON object so downstream parsers see clean data.
    if [[ "$format" == "json" && -f "$result_file" ]]; then
        awk '
            BEGIN { started = 0 }
            {
                if (!started) {
                    pos = index($0, "{")
                    if (pos > 0) {
                        started = 1
                        print substr($0, pos)
                    }
                } else {
                    print
                }
            }
        ' "$result_file" > "${result_file}.tmp" && mv -f "${result_file}.tmp" "$result_file"
    fi
}

precondition_target() {
    local mode_label="$1"
    if [[ "$fill_disk" -eq 1 ]]; then
        local script_dir fill_script
        script_dir="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
        fill_script="$script_dir/../fill_disk.sh"
        echo "[$mode_label] Filling disk (preconditioning) using $fill_script..."
        bash "$fill_script" \
            --filename "$filename" \
            --size-percent "$fill_size_percent"
        if [[ $? -ne 0 ]]; then
            echo "[$mode_label] fill_disk failed"
            exit 1
        fi
    else
        if [[ -b "$filename" ]]; then
            echo "[$mode_label] Skipping fill; running blkdiscard on $filename..."
            sudo blkdiscard "$filename"
            if [[ $? -ne 0 ]]; then
                echo "[$mode_label] blkdiscard failed"
                exit 1
            fi
        else
            echo "[$mode_label] Skipping fill; blkdiscard requires a block device (got: $filename)"
        fi
    fi
}

set_mode_context() {
    local mode_key="$1"
    case "$mode_key" in
        aio)
            ioengine=libaio
            mode_fio_args=()
            mode_name="aio"
            ;;
        uring)
            ioengine=io_uring
            mode_fio_args=()
            mode_name="uring"
            ;;
        uring-iopoll)
            ioengine=io_uring
            mode_fio_args=(--hipri=1)
            mode_name="uring-iopoll"
            ;;
        uring-sqpoll)
            ioengine=io_uring
            mode_fio_args=(--sqthread_poll=1)
            mode_name="uring-sqpoll"
            ;;
        uring-sqpoll-iopoll)
            ioengine=io_uring
            mode_fio_args=(--sqthread_poll=1 --hipri=1)
            mode_name="uring-sqpoll-iopoll"
            ;;
        *)
            echo "Unknown mode key: $mode_key"
            exit 1
            ;;
    esac
}

shuffle_run_plan() {
    local seed="$1"
    local i j tmp
    RANDOM="$seed"
    for (( i=${#run_plan[@]}-1; i>0; i-- )); do
        j=$((RANDOM % (i + 1)))
        tmp="${run_plan[i]}"
        run_plan[i]="${run_plan[j]}"
        run_plan[j]="$tmp"
    done
}

while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --filename)
            filename="$2"
            shift 2
            ;;
        --block-size)
            block_size="$2"
            shift 2
            ;;
        --filesize)
            filesize="$2"
            shift 2
            ;;
        --ramp-time)
            ramp_time="$2"
            shift 2
            ;;
        --runtime)
            runtime="$2"
            shift 2
            ;;
        --short-cooldown)
            short_cooldown="$2"
            shift 2
            ;;
        --long-cooldown)
            long_cooldown="$2"
            shift 2
            ;;
        --run-type)
            run_type="$2"
            shift 2
            ;;
        --fill-disk)
            fill_disk=1
            shift
            ;;
        --results-dir)
            results_dir="$2"
            shift 2
            ;;
        --run-count)
            run_count="$2"
            shift 2
            ;;
        --iodepth-from)
            iodepth_from="$2"
            shift 2
            ;;
        --iodepth-to)
            iodepth_to="$2"
            shift 2
            ;;
        --clocksource)
            clocksource="$2"
            shift 2
            ;;
        --format)
            format="$2"
            shift 2
            ;;
        --prefix)
            prefix="$2"
            shift 2
            ;;
        --aio)
            run_aio=1
            shift
            ;;
        --uring)
            run_uring=1
            shift
            ;;
        --uring-iopoll)
            run_uring_iopoll=1
            shift
            ;;
        --uring-sqpoll)
            run_uring_sqpoll=1
            shift
            ;;
        --uring-sqpoll-iopoll)
            run_uring_sqpoll_iopoll=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown parameter passed: $1"
            usage
            exit 1
            ;;
    esac
done

if [[ -z "$filename" ]]; then
    echo "--filename is required"
    usage
    exit 1
fi

if [[ ! -e "$filename" ]]; then
    echo "file $filename does not exist"
    exit 1
fi

if [[ -b "$filename" ]]; then
    target_size_bytes="$(sudo blockdev --getsize64 "$filename" 2>/dev/null)"
    if [[ $? -ne 0 || -z "$target_size_bytes" ]]; then
        echo "failed to get block device size for $filename"
        exit 1
    fi
elif [[ -f "$filename" ]]; then
    target_size_bytes="$(stat -c%s "$filename" 2>/dev/null)"
    if [[ $? -ne 0 || -z "$target_size_bytes" ]]; then
        echo "failed to get file size for $filename"
        exit 1
    fi
else
    echo "filename must be a block device or regular file: $filename"
    exit 1
fi

filesize_bytes="$(size_to_bytes "$filesize")"
if [[ $? -ne 0 || -z "$filesize_bytes" ]]; then
    echo "unsupported filesize format: $filesize (expected like 2500G, 4K, 1048576)"
    exit 1
fi

if (( filesize_bytes > target_size_bytes )); then
    echo "filesize ($filesize) exceeds target size ($target_size_bytes bytes)"
    exit 1
fi

if [[ "$iodepth_from" -le 0 || "$iodepth_to" -le 0 || "$iodepth_from" -gt "$iodepth_to" ]]; then
    echo "Invalid iodepth range: from=$iodepth_from to=$iodepth_to"
    exit 1
fi

if ! [[ "$run_count" =~ ^[0-9]+$ ]] || [[ "$run_count" -le 0 ]]; then
    echo "Invalid --run-count: $run_count (expected positive integer)"
    exit 1
fi

fill_size_percent=100
case "$run_type" in
    smoke)
        ramp_time=1s
        runtime=2s
        short_cooldown=1s
        fill_size_percent=1
        ;;
    normal)
        # Use explicitly provided/default values.
        ;;
    long)
        ramp_time=60s
        runtime=10m
        ;;
    *)
        echo "invalid --run-type: $run_type (expected smoke, normal, or long)"
        exit 1
        ;;
esac

if ! [[ "$ramp_time" =~ ^[0-9]+[smhd]$ ]]; then
    echo "invalid --ramp-time: $ramp_time (expected like 5s, 1m)"
    exit 1
fi

if ! [[ "$runtime" =~ ^[0-9]+[smhd]$ ]]; then
    echo "invalid --runtime: $runtime (expected like 15s, 1m)"
    exit 1
fi

if ! [[ "$short_cooldown" =~ ^[0-9]+[smhd]$ ]]; then
    echo "invalid --short-cooldown: $short_cooldown (expected like 10s, 1m)"
    exit 1
fi

if ! [[ "$long_cooldown" =~ ^[0-9]+[smhd]$ ]]; then
    echo "invalid --long-cooldown: $long_cooldown (expected like 5m, 1h)"
    exit 1
fi

short_cooldown_seconds="$(duration_to_seconds "$short_cooldown")"
if [[ $? -ne 0 || -z "$short_cooldown_seconds" ]]; then
    echo "failed to parse --short-cooldown: $short_cooldown"
    exit 1
fi

long_cooldown_seconds="$(duration_to_seconds "$long_cooldown")"
if [[ $? -ne 0 || -z "$long_cooldown_seconds" ]]; then
    echo "failed to parse --long-cooldown: $long_cooldown"
    exit 1
fi

if [[ -z "$clocksource" ]]; then
    echo "Invalid clocksource: empty value"
    exit 1
fi

if [[ ! -d "$results_dir" ]]; then
    mkdir -p "$results_dir"
    if [[ $? -ne 0 ]]; then
        echo "failed to create results dir $results_dir"
        exit 1
    fi
fi

if [[ "$run_aio" -eq 0 && "$run_uring" -eq 0 && "$run_uring_iopoll" -eq 0 && "$run_uring_sqpoll" -eq 0 && "$run_uring_sqpoll_iopoll" -eq 0 ]]; then
    run_aio=1
    run_uring=1
    run_uring_iopoll=1
    run_uring_sqpoll=1
    run_uring_sqpoll_iopoll=1
fi

selected_modes=()
if [[ "$run_aio" -eq 1 ]]; then
    selected_modes+=("aio")
fi
if [[ "$run_uring" -eq 1 ]]; then
    selected_modes+=("uring")
fi
if [[ "$run_uring_iopoll" -eq 1 ]]; then
    selected_modes+=("uring-iopoll")
fi
if [[ "$run_uring_sqpoll" -eq 1 ]]; then
    selected_modes+=("uring-sqpoll")
fi
if [[ "$run_uring_sqpoll_iopoll" -eq 1 ]]; then
    selected_modes+=("uring-sqpoll-iopoll")
fi

run_plan=()
for (( iodepth=iodepth_from; iodepth<=iodepth_to; iodepth*=2 )); do
    for mode_key in "${selected_modes[@]}"; do
        for (( run_idx=1; run_idx<=run_count; run_idx++ )); do
            run_plan+=("${mode_key}|${iodepth}|write|${run_idx}")
        done
    done
done

shuffle_run_plan 11

next_long_cooldown_epoch=$(( $(date +%s) + long_cooldown_interval_seconds ))

for run_entry in "${run_plan[@]}"; do
    IFS='|' read -r mode_key iodepth rw run_idx <<< "$run_entry"
    set_mode_context "$mode_key"
    run_fio "$iodepth" "$rw" "$run_idx"

    if (( short_cooldown_seconds > 0 )); then
        echo "Short cooldown: sleeping for $short_cooldown"
        sleep "$short_cooldown"
    fi

    now_epoch="$(date +%s)"
    while (( now_epoch >= next_long_cooldown_epoch )); do
        if (( long_cooldown_seconds > 0 )); then
            echo "Long cooldown: sleeping for $long_cooldown"
            sleep "$long_cooldown"
        fi
        next_long_cooldown_epoch=$((next_long_cooldown_epoch + long_cooldown_interval_seconds))
    done
done

if [[ "$format" == "json" ]]; then
    script_dir="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
    aggregate_cmd=(python3 "$script_dir/aggregate.py" "$results_dir" --format table --plot)
    if [[ -n "$prefix" ]]; then
        aggregate_cmd+=(--prefix "$prefix")
    fi
    "${aggregate_cmd[@]}" 2>&1 | tee "$results_dir/result.txt"
fi
