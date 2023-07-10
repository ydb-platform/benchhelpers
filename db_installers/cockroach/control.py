#!/usr/bin/env python3
# -*- coding: UTF-8 -*-

import argparse
import logging
import os
import pipes
import sys

if __package__:
    from ..pylib.common import ErrorExit, SSHAction, PSSHAction, BaseAction, Nodes
else:
    sys.path.append(os.path.dirname(__file__) + '/..')
    from pylib.common import ErrorExit, SSHAction, PSSHAction, BaseAction, Nodes


logger = logging.getLogger(__name__)


def disk2mnt(disk):
    name = os.path.basename(disk)
    return DEPLOY_PATH + "/data/" + name


class Deploy(PSSHAction):

    def __init__(self, args):
        super().__init__(args)
        self.package = args.deploy
        self.config = args.config

    @staticmethod
    def _get_mkdir_cmd():
        paths = [pipes.quote(os.path.join(DEPLOY_PATH, dirname)) for dirname in ("data", "logs")]
        return "sudo mkdir -p " + " ".join(paths)

    def run(self):
        super().run()
        self._logger.info("Deploy %s", self.package)
        filename = os.path.basename(self.package)
        upload_path = os.path.join(DEPLOY_TMP_PATH, filename)
        self._logger.info("Upload %s", self.package)
        self.pssh_upload(self.package, DEPLOY_TMP_PATH)

        pssh_cmd = self._get_mkdir_cmd()
        pssh_cmd += "; sudo -u {user} tar -xzf {src} -C {dst} --strip-components=1 | tail; rm -f {src}".format(
            src=pipes.quote(upload_path), dst=pipes.quote(DEPLOY_PATH), user=self.sudo_user)

        self._logger.info("Extract to %s", DEPLOY_PATH)
        self.pssh_run(pssh_cmd)


class Init(SSHAction):

    def __init__(self, args):
        super().__init__(args)
        self.sudo_user = args.sudo_user

    def run(self):
        super().run()
        cockroach = DEPLOY_PATH + "/cockroach"

        cmd = "sudo -u {user} {cockroach} init --insecure --host={host}"
        cmd = cmd.format(user=self.sudo_user, host=self.host, cockroach=cockroach)

        self.ssh_cmd(cmd)


# actually not pssh action, but derives hosts and timeout
class Start(PSSHAction):

    def __init__(self, args):
        super().__init__(args)
        self.task_set = args.task_set
        self.per_disk_instance = args.per_disk_instance

    def start_instance(self, host, store_args, listen_addr, http_addr, join_nodes, task_set, region, cache_size, sql_mem_size):
        self._logger.info("Start on " + host + ", addr " + listen_addr)
        join_nodes_str = ",".join(join_nodes)
        cmd = "sudo -u {user} sh -c \""
        self._logger.info(task_set)
        if task_set:
            cmd += "taskset -c " + task_set + " "
        cmd += DEPLOY_PATH + "/cockroach_wrapper start --insecure --listen-addr={listen_addr} --http-addr={http_addr} --join={join} --locality=region={region} "
        cmd += store_args
        cmd += "--cache={cache}GB --max-sql-memory={sql_mem}GB\""
        cmd = cmd.format(
            user=self.sudo_user,
            listen_addr=listen_addr,
            http_addr=http_addr,
            join=join_nodes_str,
            cache=cache_size,
            sql_mem=sql_mem_size,
            region=region)
        if self.dry_run:
            cmd = "echo '" + cmd + "'"

        class Args:
            def __init__(self, host, parent):
                self.hosts = host
                self.dry_run = parent.dry_run
                self.fail_on_error = parent.fail_on_error

        action = SSHAction(Args(host, self))
        action.ssh_cmd(cmd)

    def get_join_nodes(self):
        join_nodes = []
        for host in Nodes[:3]:
            join_nodes.append(host + ":" + str(LISTEN_PORT))
        return join_nodes

    def run_per_disk(self):
        join_nodes = self.get_join_nodes()

        cores_per_instance = Cores // len(Disks)
        cache_per_instance = CacheSizeGB // len(Disks)
        sql_mem_per_instance = SqlMemorySizeGB // len(Disks)
        for region in Regions:
            for host in region.Nodes:
                port = LISTEN_PORT
                http_port = HTTP_PORT
                start_core = 0
                sql_mem_reminder = SqlMemorySizeGB % len(Disks)
                cache_reminder = CacheSizeGB % len(Disks)
                cores_reminder = Cores % len(Disks)
                for d in Disks:
                    store_args = "--store " + disk2mnt(d) + " "
                    listen_host = ":" + str(port)
                    http_listen = ":" + str(http_port)
                    end_core = start_core + cores_per_instance - 1 + (cores_reminder > 0)
                    task_set = str(start_core) + "-" + str(end_core)
                    self.start_instance(
                        host,
                        store_args,
                        listen_host,
                        http_listen,
                        join_nodes,
                        task_set,
                        region.Name,
                        cache_per_instance + (cache_reminder > 0),
                        sql_mem_per_instance + (sql_mem_reminder > 0))
                    port += 1
                    http_port += 1
                    start_core = end_core + 1
                    cores_reminder -= (cores_reminder > 0)
                    cache_reminder -= (cache_reminder > 0)
                    sql_mem_reminder -= (sql_mem_reminder > 0)

    def run(self):
        super().run()

        if self.per_disk_instance:
            return self.run_per_disk()

        store_args = ""
        for d in Disks:
            store_args += "--store " + disk2mnt(d) + " "

        join_nodes = self.get_join_nodes()
        for region in Regions:
            for host in region.Nodes:
                http_listen = ":" + str(HTTP_PORT)
                listen_host = ":" + str(LISTEN_PORT)
                self.start_instance(
                    host,
                    store_args,
                    listen_host,
                    http_listen,
                    join_nodes,
                    self.task_set,
                    region.Name,
                    CacheSizeGB,
                    SqlMemorySizeGB)


class Stop(PSSHAction):

    def __init__(self, args):
        super().__init__(args)

    def run(self):
        super().run()
        self._logger.info("Stop")
        cmd = "sudo -u {user} sh -c 'pkill cockroach; pkill haproxy; sleep 1;"
        cmd += "pkill -9 cockroach; pkill -9 haproxy; echo \"DONE\"'"
        self.pssh_run(cmd.format(user=self.sudo_user), add_hosts=HA_PROXY_NODES)


class Clean(Stop):

    def __init__(self, args):
        super().__init__(args)

    def run(self):
        super().run()
        self._logger.info("Clean")

        disk_cmd = "sudo -u {user} umount {mnt} 2>&1"
        disk_cmds = []
        for disk in Disks:
            mount_point = disk2mnt(disk)
            disk_cmds.append(disk_cmd.format(mnt=mount_point, disk=disk, user=self.sudo_user))

        self.pssh_run(";".join(disk_cmds))


class Format(PSSHAction):

    def __init__(self, args):
        super().__init__(args)

    def run(self):
        super().run()
        logging.info("Formatting hosts")

        disk_cmd = "sudo -u {user} umount {mnt} 2>&1;"
        disk_cmd += "sudo -u {user} mkfs -t ext4 {disk} 2>&1;"
        disk_cmd += "sudo -u {user} mkdir -p {mnt};"
        disk_cmd += "sudo -u {user} mount -o defaults,noatime,nodiratime,nobarrier {disk} {mnt}"

        disk_cmds = []
        for disk in Disks:
            mount_point = disk2mnt(disk)
            disk_cmds.append(disk_cmd.format(mnt=mount_point, disk=disk, user=self.sudo_user))

        self.pssh_run(";".join(disk_cmds))


class ReturnHosts(BaseAction):

    def __init__(self, args):
        super().__init__(args)
        self.hosts = args.hosts

    def run(self):
        list_hosts = Nodes
        if self.hosts:
            print(f"{self.hosts}")
        print(' '.join(list_hosts))


class ReturnListenPort(BaseAction):

    def __init__(self, args):
        super().__init__(args)

    def run(self):
        print(LISTEN_PORT)


class ReturnDeployPath(BaseAction):

    def __init__(self, args):
        super().__init__(args)

    def run(self):
        print(DEPLOY_PATH)


class ReturnHaProxyHosts(BaseAction):

    def __init__(self, args):
        super().__init__(args)

    def run(self):
        print(' '.join(HA_PROXY_NODES))


class ReturnHaProxySetupPath(BaseAction):

    def __init__(self, args):
        super().__init__(args)

    def run(self):
        print(HA_PROXY_SETUP_PATH)


# [ control.py -c cluster_config.py stop ]
# control.py -c cluster_config.py format
# control.py -c cluster_config.py deploy
# control.py -c cluster_config.py start
# control.py -c cluster_config.py init
# --dry-run, --first-node-only
class Main(object):

    def __init__(self):
        super().__init__()
        self._logger = logger.getChild(self.__class__.__name__)
        self.args = None
        self.parser = None
        self.cmd_group = None
        self.cmds = dict()

    def add_cmd(self, name, action_cls, help, has_arg=False):
        self.cmds[name.replace("-", "_")] = action_cls
        arg_name = "--" + name
        if has_arg:
            self.cmd_group.add_argument(arg_name, help=help, action="store", type=str)
        else:
            self.cmd_group.add_argument(arg_name, help=help, action="store_true")

    def parse_args(self):
        self.parser = argparse.ArgumentParser(description="Cockroach Deploy Utility")
        self.cmd_group = self.parser.add_mutually_exclusive_group(required=True)

        # commands
        self.add_cmd("start", Start, "start Cockroach cluster")
        self.add_cmd("stop", Stop, "stop Cockroach cluster")
        self.add_cmd("format", Format, "format hosts")
        self.add_cmd("clean", Clean, "umount fs, etc")
        self.add_cmd("init", Init, "init Cockroach cluster")
        self.add_cmd("deploy", Deploy, "deploy release to cluster", has_arg=True)
        self.add_cmd("list-hosts", ReturnHosts, "return list of hosts")
        self.add_cmd("listen-port", ReturnListenPort, "return listen port")
        self.add_cmd("ha-proxy-hosts", ReturnHaProxyHosts, "return list of ha-proxy hosts")
        self.add_cmd("ha-proxy-setup-path", ReturnHaProxySetupPath, "return list of ha-proxy hosts")
        self.add_cmd("deploy-path", ReturnDeployPath, "return deploy path")

        # general options
        self.parser.add_argument("-c", "--config", type=str, help="Cockroach Cluster Config")
        self.parser.add_argument("-p", "--package", type=str, help="Cockroach package")
        self.parser.add_argument("--hosts", type=str, help="pssh calc expression")
        self.parser.add_argument("--username", type=str, help="pssh username")
        self.parser.add_argument("--sudo-user", type=str, help="pssh sudo username", default="root")
        self.parser.add_argument("--task-set", type=str, help="Specify cpus to run on")
        self.parser.add_argument("--fail-on-error", action="store_true", help="Abort if any subcommand failed")
        self.parser.add_argument("--dry-run", action="store_true", help="Don't execute commands")
        self.parser.add_argument("--per-disk-instance", action="store_true", help="Run per disk cockroach instances")
        self.args = self.parser.parse_args()

    def run(self):
        logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
        self.parse_args()

        try:
            with open(self.args.config) as f:
                exec(f.read(), globals())
        except Exception as e:
            self._logger.error("Can't import cluster config: " + str(e))
            return -1

        for region in Regions:
            Nodes.extend(region.Nodes)

        if self.args.deploy:
            if not os.path.isfile(self.args.deploy):
                self._logger.error("Can't open packge: " + self.args.deploy)
                return -1

        for cmd_name, cmd_cls in self.cmds.items():
            if cmd_name in vars(self.args) and vars(self.args)[cmd_name]:
                try:
                    action = cmd_cls(self.args)
                    action.run()
                    return 0
                except ErrorExit:
                    return 1
        self._logger.error("Can't select command")
        return 1


if __name__ == "__main__":
    sys.exit(Main().run())
