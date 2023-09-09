#!/bin/bash
# Install Java 20 on a set of hosts

java_x86_url=https://download.oracle.com/java/20/latest/jdk-20_linux-x64_bin.tar.gz
java_arm_url=https://download.oracle.com/java/20/latest/jdk-20_linux-aarch64_bin.tar.gz


usage() {
    echo "install_java20.sh --hosts <hosts_file> [--package <java-20.tar.gz>] [--package-url <url>]"
}

unique_hosts=

cleanup() {
    if [ -n "$unique_hosts" ]; then
        rm -f $unique_hosts
    fi
}

trap cleanup SIGINT SIGTERM

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
        --package)
            shift
            package=$1
            ;;
        --package-url)
            shift
            package_url=$1
            ;;
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
some_host=`head -n 1 $unique_hosts`

if [ -n "$package" ]; then
    if [ ! -f "$package" ]; then
        echo "Package $package not found"
        exit 1
    fi

    echo "Uploading package $package to hosts $hosts"
    parallel-scp -h $unique_hosts $package $HOME
    if [ $? -ne 0 ]; then
        echo "Failed to upload package $package to hosts $hosts"
        exit 1
    fi

    package_name=`basename $package`
else
    if [ -n "$package_url" ]; then
        url=$package_url
    else
        arch=`ssh $some_host uname -m`
        if [ "$arch" == "x86_64" ]; then
            echo "Downloading x86_64 Java 20"
            url=$java_x86_url
        elif [ "$arch" == "aarch64" ]; then
            echo "Downloading aarch64 Java 20"
            url=$java_arm_url
        else
            echo "Unsupported architecture $arch"
            exit 1
        fi
    fi

    package_name=`basename $url`

    parallel-ssh -i -h $unique_hosts "wget -q -O $package_name $url"
    if [ $? -ne 0 ]; then
        echo "Failed to download from $url to hosts"
        exit 1
    fi
fi

java_dir=$(dirname `ssh $some_host "tar -tzf $package_name 2>/dev/null | head -1"`)

echo "Unpacking the package"
parallel-ssh -h $unique_hosts "tar -xzf $package_name && rm -f $package_name"
if [ $? -ne 0 ]; then
    echo "Failed to unpack the package"
    exit 1
fi

echo "Adding Java 20 symlink to the /usr/local/bin"
parallel-ssh -i -h $unique_hosts "sudo ln -s \$HOME/$java_dir/bin/java /usr/local/bin/java"
if [ $? -ne 0 ]; then
    echo "Failed to add Java 20 symlink to the /usr/local/bin"
    exit 1
fi
