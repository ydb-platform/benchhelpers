#!/bin/bash

set -u

usage() {
    echo "Usage: $0 --filename <filename> [--size-percent <1-100>]"
}

size_percent=100

while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --filename)
            filename="$2"
            shift 2
            ;;
        --size-percent)
            size_percent="$2"
            shift 2
            ;;
        --help|-h)
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

if [[ -z "${filename:-}" ]]; then
    echo "filename is required"
    usage
    exit 1
fi

if [[ ! -e "$filename" ]]; then
    echo "file $filename not exists"
    exit 1
fi

if ! [[ "$size_percent" =~ ^[0-9]+$ ]] || (( size_percent < 1 || size_percent > 100 )); then
    echo "size-percent must be an integer in [1,100], got: $size_percent"
    exit 1
fi

# Reset medium state before preconditioning when target is a block device.
if [[ -b "$filename" ]]; then
    echo "Discarding block device before preconditioning..."
    if ! which blkdiscard >/dev/null; then
        echo "blkdiscard not found, install util-linux package"
        exit 1
    fi
    sudo blkdiscard "$filename"
    if [[ $? -ne 0 ]]; then
        echo "blkdiscard failed for $filename"
        exit 1
    fi
fi

# Precondition a selected portion of target size.
target_size_args=(--size="${size_percent}%")

echo "Running test: seq_fill (${size_percent}% of target)"
sudo fio --name=seq_fill \
    --filename="$filename" \
    "${target_size_args[@]}" \
    --rw=write \
    --bs=1M \
    --iodepth=32 \
    --direct=1 \
    --numjobs=1 \
    --ioengine=libaio \
    --group_reporting \
    1>/dev/null
if [[ $? -ne 0 ]]; then
    echo "seq_fill failed"
    exit 1
fi

echo "Running test: rand_precondition (${size_percent}% of target)"
sudo fio --name=rand_precondition \
    --filename="$filename" \
    "${target_size_args[@]}" \
    --rw=randwrite \
    --bs=8K \
    --iodepth=32 \
    --direct=1 \
    --numjobs=1 \
    --ioengine=libaio \
    --group_reporting \
    1>/dev/null
if [[ $? -ne 0 ]]; then
    echo "rand_precondition failed"
    exit 1
fi

