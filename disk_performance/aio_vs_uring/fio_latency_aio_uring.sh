#!/bin/bash

filesize=2500G

ramp_time=10s
runtime=1m
run_type=normal

block_size=4K
run_reads=0
clocksource=cpu
format=json
results_dir="."
skip_fill_disk=0

iodepth_from=1
iodepth_to=128

run_aio=0
run_uring=0
run_uring_cqpoll=0
run_uring_sqpoll=0
run_uring_sqpoll_cqpoll=0

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
  --run-type <smoke|normal|long>   run profile (default: $run_type)
  --skip-fill-disk                 skip preconditioning fill (default: false)
  --results-dir <path>             directory for fio outputs (default: $results_dir)
  --reads                          also run read test (default: write-only)
  --iodepth-from <n>               iodepth start (default: $iodepth_from)
  --iodepth-to <n>                 iodepth end (default: $iodepth_to)
  --clocksource <name>             fio clock source (default: $clocksource)
  --format <fmt>                   output format for fio files (default: $format)

Modes (if none selected, all are run):
  --aio                            libaio mode
  --uring                          io_uring mode
  --uring-cqpoll                   io_uring + completion polling (--hipri)
  --uring-sqpoll                   io_uring + SQ polling (--sqthread_poll)
  --uring-sqpoll-cqpoll           io_uring + SQ polling + completion polling
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

function run_fio {
    local iodepth="$1"
    local rw="$2"
    local fio_test_name="${rw}_latency_test"
    local clock_args="--clocksource=$clocksource"
    local result_file="$results_dir/${mode_name}_qd${iodepth}_${rw}.$format"

    echo "-------------------------------------------------"
    echo "Running fio test: $mode_name"
    echo "ioengine=$ioengine ioengine_args='$ioengine_args' clock_args='$clock_args' bs=$block_size iodepth=$iodepth runtime=$runtime rw=$rw output=$result_file"
    echo "fio command:"
    echo "  sudo fio --name=\"$fio_test_name\" \\"
    echo "    --filename=\"$filename\" --filesize=$filesize \\"
    echo "    --time_based --ramp_time=$ramp_time --runtime=$runtime \\"
    echo "    --ioengine=$ioengine $ioengine_args $clock_args --direct=1 --verify=0 --randrepeat=0 \\"
    echo "    --bs=$block_size --iodepth=$iodepth --rw=\"rand$rw\" --iodepth_batch_submit=$iodepth \\"
    echo "    --iodepth_batch_complete_max=$iodepth \\"
    echo "    --percentile_list=\"10:50:90:95:99:99.9\" \\"
    echo "    --output-format=\"$format\" \\"
    echo "    --output=\"$result_file\""
    echo "-------------------------------------------------"

    sudo fio --name="$fio_test_name" \
    --filename="$filename" --filesize=$filesize \
    --time_based --ramp_time=$ramp_time --runtime=$runtime \
    --ioengine=$ioengine $ioengine_args $clock_args --direct=1 --verify=0 --randrepeat=0 \
    --bs=$block_size --iodepth=$iodepth --rw="rand$rw" --iodepth_batch_submit=$iodepth  \
    --iodepth_batch_complete_max=$iodepth \
    --percentile_list="10:50:90:95:99:99.9" \
    --output-format="$format" \
    --output="$result_file"

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
        --run-type)
            run_type="$2"
            shift 2
            ;;
        --skip-fill-disk)
            skip_fill_disk=1
            shift
            ;;
        --results-dir)
            results_dir="$2"
            shift 2
            ;;
        --reads)
            run_reads=1
            shift
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
        --aio)
            run_aio=1
            shift
            ;;
        --uring)
            run_uring=1
            shift
            ;;
        --uring-cqpoll)
            run_uring_cqpoll=1
            shift
            ;;
        --uring-sqpoll)
            run_uring_sqpoll=1
            shift
            ;;
        --uring-sqpoll-cqpoll)
            run_uring_sqpoll_cqpoll=1
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

fill_size_percent=100
case "$run_type" in
    smoke)
        ramp_time=1s
        runtime=2s
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

if [[ "$skip_fill_disk" -eq 0 ]]; then
    script_dir="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
    fill_script="$script_dir/../fill_disk.sh"
    echo "Filling disk (preconditioning) using $fill_script..."
    bash "$fill_script" \
        --filename "$filename" \
        --size-percent "$fill_size_percent"
    if [[ $? -ne 0 ]]; then
        echo "fill_disk failed"
        exit 1
    fi
fi

if [[ "$run_aio" -eq 0 && "$run_uring" -eq 0 && "$run_uring_cqpoll" -eq 0 && "$run_uring_sqpoll" -eq 0 && "$run_uring_sqpoll_cqpoll" -eq 0 ]]; then
    run_aio=1
    run_uring=1
    run_uring_cqpoll=1
    run_uring_sqpoll=1
    run_uring_sqpoll_cqpoll=1
fi

for (( iodepth=iodepth_from; iodepth<=iodepth_to; iodepth*=2 )); do
    if [[ "$run_aio" -eq 1 ]]; then
        ioengine=libaio
        ioengine_args=
        mode_name="aio"
        run_fio "$iodepth" "write"
        if [[ "$run_reads" -eq 1 ]]; then
            run_fio "$iodepth" "read"
        fi
    fi

    if [[ "$run_uring" -eq 1 ]]; then
        ioengine=io_uring
        ioengine_args=
        mode_name="uring"
        run_fio "$iodepth" "write"
        if [[ "$run_reads" -eq 1 ]]; then
            run_fio "$iodepth" "read"
        fi
    fi

    if [[ "$run_uring_cqpoll" -eq 1 ]]; then
        ioengine=io_uring
        ioengine_args="--hipri"
        mode_name="uring-cqpoll"
        run_fio "$iodepth" "write"
        if [[ "$run_reads" -eq 1 ]]; then
            run_fio "$iodepth" "read"
        fi
    fi

    if [[ "$run_uring_sqpoll" -eq 1 ]]; then
        ioengine=io_uring
        ioengine_args="--sqthread_poll"
        mode_name="uring-sqpoll"
        run_fio "$iodepth" "write"
        if [[ "$run_reads" -eq 1 ]]; then
            run_fio "$iodepth" "read"
        fi
    fi

    if [[ "$run_uring_sqpoll_cqpoll" -eq 1 ]]; then
        ioengine=io_uring
        ioengine_args="--sqthread_poll --hipri"
        mode_name="uring-sqpoll-cqpoll"
        run_fio "$iodepth" "write"
        if [[ "$run_reads" -eq 1 ]]; then
            run_fio "$iodepth" "read"
        fi
    fi

    if [[ "$iodepth" -eq 0 ]]; then
        break
    fi
done

if [[ "$format" == "json" ]]; then
    script_dir="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
    python3 "$script_dir/aggregate.py" "$results_dir" --format table 2>&1 | tee "$results_dir/result.txt"
fi
