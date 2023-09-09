#!/bin/bash
# Copy SSH keys to remote hosts, to allow passwordless SSH

usage() {
    echo "copy_ssh_keys.sh --hosts <hosts_file>"
}

unique_hosts=

cleanup() {
    if [ -n "$unique_hosts" ]; then
        rm -f $unique_hosts
    fi
}

if ! which parallel-ssh >/dev/null; then
    echo "parallel-ssh not found, you should install pssh"
    exit 1
fi

if ! which parallel-scp >/dev/null; then
    echo "parallel-ssh not found, you should install pssh"
    exit 1
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --hosts)
            shift
            hosts=$1
            ;;
        *)
            usage
            exit 1
            ;;
    esac
    shift
done

if [ -z "$hosts" ]; then
    echo "Hosts file not specified"
    usage
    exit 1
fi

if [ ! -f "$hosts" ]; then
    echo "Hosts file $hosts not found"
    exit 1
fi

unique_hosts=`mktemp`
sort -u $hosts > $unique_hosts

trap cleanup EXIT

rsa_key="$HOME/.ssh/id_rsa"
dsa_key="$HOME/.ssh/id_dsa"
ssh_key=""

if [[ -f "$rsa_key" ]]; then
    echo "RSA key already exists, using $rsa_key"
    ssh_key="$rsa_key"
elif [[ -f "$dsa_key" ]]; then
    echo "DSA key already exists, using $dsa_key"
    ssh_key="$dsa_key"
else
    echo "No SSH key found. Generating $rsa_key..."
    ssh-keygen -t rsa -f "$rsa_key" -N ""
    ssh_key="$rsa_key"
fi

for host in `cat $unique_hosts`; do
    echo "Adding key to $host..."
    ssh-copy-id -o StrictHostKeyChecking=no -i "$ssh_key.pub" "$host"
done
