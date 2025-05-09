#!/usr/bin/env python3
# -*- coding: UTF-8 -*-

import argparse


import logging
import os
import pipes
import sys

MasterMemoryRatio = 0.15
TServerMemoryRatio = 0.85

if __package__:
    from ..pylib.common import ErrorExit, SSHAction, PSSHAction, BaseAction, Hosts
else:
    sys.path.append(os.path.dirname(__file__) + '/..')
    from pylib.common import ErrorExit, SSHAction, PSSHAction, BaseAction, Hosts


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
        pssh_cmd += "; cd {dst}; sudo ./bin/post_install.sh".format(dst=pipes.quote(DEPLOY_PATH))
        self._logger.info("Extract to %s", DEPLOY_PATH)
        self.pssh_run(pssh_cmd)


# actually not pssh action, but derives hosts and timeout
class Start(PSSHAction):

    def __init__(self, args):
        super().__init__(args)
        self.tservers_per_host = args.tservers_per_host

        if self.tservers_per_host > 1:
            disks_count = len(Disks)
            if disks_count < self.tservers_per_host:
                self._logger.error("Not enough disks for tserver instances")
                raise ErrorExit()
            if disks_count % self.tservers_per_host != 0:
                self._logger.error("Disks count should be multiple of tserver instances")
                raise ErrorExit()

        if TaskSets and len(TaskSets) > 0:
            if len(TaskSets) != self.tservers_per_host:
                self._logger.error("TaskSets count should be equal to tserver instances")
                raise ErrorExit()

    def start_master(self, host, store_args, listen_addr, webserver_port, master_hosts):
        self._logger.info("Start master on " + host + ", addr " + listen_addr)
        master_hosts_str = ",".join(master_hosts)
        memory_ratio = MasterMemoryRatio
        cmd = "sudo -u {user} sh -c \""

        if MasterTaskSet:
            cmd += "taskset -c " + MasterTaskSet + " "
        cmd += "{path}/yugabyte_wrapper {path}/bin/yb-master --rpc_bind_addresses={listen_addr} --master_addresses={master_hosts_str} "
        cmd += " --default_memory_limit_to_ram_ratio {memory_ratio} --webserver_port {webserver_port}"
        cmd += " " + store_args
        cmd += " \""
        cmd = cmd.format(
            path=DEPLOY_PATH,
            user=self.sudo_user,
            webserver_port=webserver_port,
            listen_addr=listen_addr,
            master_hosts_str=master_hosts_str,
            memory_ratio=memory_ratio)
        if self.dry_run:
            cmd = "echo '" + cmd + "'"

        class Args:
            def __init__(self, host, parent):
                self.hosts = host
                self.dry_run = parent.dry_run
                self.fail_on_error = parent.fail_on_error

        action = SSHAction(Args(host, self))
        action.ssh_cmd(cmd)

    def start_server(
            self,
            host,
            store_args,
            listen_addr,
            master_hosts,
            psql_port,
            cql_port,
            redis_webserver_port,
            webserver_port,
            cql_webserver_port,
            psql_webserver_port,
            task_set=None,
            memory_ratio=None):
        self._logger.info("Start server on " + host + ", addr " + listen_addr)
        master_hosts_str = ",".join(master_hosts)
        cmd = "sudo -u {user} sh -c \""
        if task_set:
            cmd += "taskset -c " + task_set + " "
        cmd += "{path}/yugabyte_wrapper {path}/bin/yb-tserver --rpc_bind_addresses={listen_addr} --tserver_master_addrs={master_hosts_str} "
        cmd += "--pgsql_proxy_webserver_port {psql_webserver_port} --cql_proxy_webserver_port {cql_webserver_port} --webserver_port {webserver_port} --redis_proxy_webserver_port {redis_webserver_port} "
        cmd += store_args
        cmd += " --pgsql_proxy_bind_address {psql_addr}"
        cmd += " --cql_proxy_bind_address {cql_addr}"
        cmd += " --ysql_max_connections 1000"
        cmd += " --start_redis_proxy=false"
        if memory_ratio:
            cmd += " --default_memory_limit_to_ram_ratio " + str(memory_ratio)
        cmd += " \""
        cmd = cmd.format(
            path=DEPLOY_PATH,
            user=self.sudo_user,
            listen_addr=listen_addr,
            master_hosts_str=master_hosts_str,
            psql_addr=LOCAL_IP.get(host, host) + ":" + str(psql_port),
            cql_addr=LOCAL_IP.get(host, host) + ":" + str(cql_port),
            redis_webserver_port=str(redis_webserver_port),
            webserver_port=str(webserver_port),
            cql_webserver_port=str(cql_webserver_port),
            psql_webserver_port=str(psql_webserver_port))
        if self.dry_run:
            cmd = "echo '" + cmd + "'"

        class Args:
            def __init__(self, host, parent):
                self.hosts = host
                self.dry_run = parent.dry_run
                self.fail_on_error = parent.fail_on_error

        action = SSHAction(Args(host, self))
        action.ssh_cmd(cmd)

    def get_master_hosts(self):
        return Hosts[:3]

    def get_master_hosts_listen(self):
        join_hosts = []
        for host in self.get_master_hosts():
            join_hosts.append(LOCAL_IP.get(host, host) + ":" + str(LISTEN_PORT_MASTER))
        return join_hosts

    def run(self):
        super().run()

        mount_dirs = [disk2mnt(d) for d in Disks]
        mount_dirs_str = ",".join(mount_dirs)
        store_args = "--fs_data_dirs=" + mount_dirs_str

        master_hosts = self.get_master_hosts_listen()
        for host in self.get_master_hosts():
            listen_host = LOCAL_IP.get(host, host) + ":" + str(LISTEN_PORT_MASTER)
            self.start_master(host, store_args, listen_host, MASTER_WEBSERVER_PORT, master_hosts)

        cores_per_instance = Cores
        memory_ratio_per_instance = TServerMemoryRatio  # default in yugabyte is 85% for tserver
        if self.tservers_per_host:
            memory_ratio_per_instance = round(memory_ratio_per_instance / self.tservers_per_host, 2)
            cores_per_instance = int(Cores // self.tservers_per_host)
            disks_per_instance = int(len(Disks) // self.tservers_per_host)

        for host in Hosts:
            if self.tservers_per_host > 1:
                start_core = 0
                current_server_port = LISTEN_PORT_SERVER
                current_psql_port = PSQL_PORT
                current_cql_port = CQL_PORT
                current_redis_webserver_port = REDIS_WEBSERVER_PORT
                current_webserver_port = SERVER_WEBSERVER_PORT
                current_cql_webserver_port = CQL_WEBSERVER_PORT
                current_psql_webserver_port = PSQL_WEBSERVER_PORT
                cores_reminder = Cores % self.tservers_per_host

                for i in range(self.tservers_per_host):
                    instance_dirs = ",".join(mount_dirs[i * disks_per_instance:(i + 1) * disks_per_instance])
                    listen_host = LOCAL_IP.get(host, host) + ":" + str(current_server_port)
                    store_args = "--fs_data_dirs=" + instance_dirs

                    end_core = start_core + cores_per_instance - 1 + (cores_reminder > 0)

                    if TaskSets and len(TaskSets):
                        task_set = TaskSets[i]
                    else:
                        task_set = str(start_core) + "-" + str(end_core)

                    self.start_server(
                        host,
                        store_args,
                        listen_host,
                        master_hosts,
                        psql_port=current_psql_port,
                        cql_port=current_cql_port,
                        redis_webserver_port=current_redis_webserver_port,
                        webserver_port=current_webserver_port,
                        cql_webserver_port=current_cql_webserver_port,
                        psql_webserver_port=current_psql_webserver_port,
                        task_set=task_set,
                        memory_ratio=memory_ratio_per_instance)

                    current_server_port += 1
                    current_psql_port += 1
                    current_cql_port += 1
                    current_redis_webserver_port += 1
                    current_webserver_port += 1
                    current_cql_webserver_port += 1
                    current_psql_webserver_port += 1
                    start_core = end_core + 1
                    cores_reminder -= (cores_reminder > 0)
            else:
                listen_host = LOCAL_IP.get(host, host) + ":" + str(LISTEN_PORT_SERVER)
                if TaskSets and len(TaskSets):
                    task_set = TaskSets[0]
                self.start_server(
                    host,
                    store_args,
                    listen_host,
                    master_hosts,
                    task_set=task_set,
                    psql_port=PSQL_PORT,
                    cql_port=CQL_PORT,
                    redis_webserver_port=REDIS_WEBSERVER_PORT,
                    webserver_port=SERVER_WEBSERVER_PORT,
                    cql_webserver_port=CQL_WEBSERVER_PORT,
                    psql_webserver_port=PSQL_WEBSERVER_PORT,
                    memory_ratio=memory_ratio_per_instance)


class Stop(PSSHAction):

    def __init__(self, args):
        super().__init__(args)

    def run(self):
        super().run()
        self._logger.info("Stop")
        cmd = "sudo -u {user} sh -c 'pkill yb-master; pkill yb-tserver; pkill haproxy; sleep 5;"
        cmd += " pkill -9 yb-master; pkill -9 yb-tserver; pkill -9 haproxy; echo \"DONE\"'"
        self.pssh_run(cmd.format(user=self.sudo_user))


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
        disk_cmd += "sudo -u {user} mkfs.xfs -f {disk} 2>&1;"
        disk_cmd += "sudo -u {user} mkdir -p {mnt};"
        disk_cmd += "sudo -u {user} mount -o defaults,noatime,nodiratime {disk} {mnt}"

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
        list_hosts = Hosts
        if self.hosts:
            print(f"{self.hosts}")
        print(' '.join(list_hosts))


class ReturnDeployPath(BaseAction):

    def __init__(self, args):
        super().__init__(args)

    def run(self):
        print(f"{DEPLOY_PATH}")


class ReturnDeployTmpPath(BaseAction):

    def __init__(self, args):
        super().__init__(args)

    def run(self):
        print(DEPLOY_TMP_PATH)


# [ control.py -c cluster_config.py stop ]
# control.py -c cluster_config.py format
# control.py -c cluster_config.py deploy
# control.py -c cluster_config.py start
# --dry-run
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
        self.parser = argparse.ArgumentParser(description="Yugabyte Deploy Utility")
        self.cmd_group = self.parser.add_mutually_exclusive_group(required=True)

        # commands
        self.add_cmd("start", Start, "start Yugabyte cluster")
        self.add_cmd("stop", Stop, "stop Yugabyte cluster")
        self.add_cmd("format", Format, "format hosts")
        self.add_cmd("clean", Clean, "umount fs, etc")
        self.add_cmd("deploy", Deploy, "deploy release to cluster", has_arg=True)
        self.add_cmd("list-hosts", ReturnHosts, "return list of hosts")
        self.add_cmd("deploy-path", ReturnDeployPath, "return deploy path")
        self.add_cmd("deploy-tmp-path", ReturnDeployTmpPath, "return deploy tmp path")

        # general options
        self.parser.add_argument("-c", "--config", type=str, help="Yugabyte Cluster Config")
        self.parser.add_argument("-p", "--package", type=str, help="Yugabyte package")
        self.parser.add_argument("--hosts", type=str, help="pssh calc expression")
        self.parser.add_argument("--username", type=str, help="pssh username")
        self.parser.add_argument("--sudo-user", type=str, help="pssh sudo username", default="root")
        self.parser.add_argument("--fail-on-error", action="store_true", help="Abort if any subcommand failed")
        self.parser.add_argument("--dry-run", action="store_true", help="Don't execute commands")
        self.parser.add_argument("--tservers-per-host", action="store", type=int, default=1, help="Number of tserver instances per host")
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
            Hosts.extend(region.Hosts)

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
