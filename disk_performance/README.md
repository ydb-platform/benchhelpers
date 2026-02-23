This folder contains the scripts to run the disk performance tests.

## fio_device.sh

This script runs a number of fio tests on a single device. Device must be unmounted before running the script.

Example:
```
./fio_device.sh --filename /dev/nvme3n1
```
