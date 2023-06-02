#!/usr/bin/env python
# -*- coding: UTF-8 -*-

import argparse
import datetime
import logging
import os
import pipes
import subprocess
import sys
import time


logger = logging.getLogger(__name__)
DEPLOY_PATH = "/place/berkanavt/cockroach"
DEPLOY_TMP_PATH = "/var/tmp"
HTTP_PORT = 8080
LISTEN_PORT = 26257
HA_PROXY_PORT = LISTEN_PORT

Nodes = []

def disk2mnt(disk):
    name = os.path.basename(disk)
    return DEPLOY_PATH + "/data/" + name


class ErrorExit(Exception):
    pass


class JobException(Exception):
    pass


class Job(object):

    def __init__(self, command, timeout=3600, kill_timeout=5, shell=False):
        super(Job, self).__init__()
        self._logger = logger.getChild(self.__class__.__name__)
        if isinstance(command, basestring):
            self.command = tuple(command.split())
        else:
            self.command = command
        self.timeout = timeout
        self.kill_timeout = kill_timeout
        self.shell = shell
        self.time_elapsed = 0
        self.terminated = False
        self.process = None

    def check_terminate_timeout(self):
        if self.terminated or self.time_elapsed <= self.timeout:
            return
        self._logger.warning("Execution of '%r' timeout. Terminating (timeout = %d)", self.command, self.timeout)
        try:
            self.process.terminate()
        except Exception:
            self._logger.error("Failed to terminate job.", exc_info=True)
        self.terminated = True

    def check_kill_timeout(self):
        if self.time_elapsed <= self.timeout + self.kill_timeout:
            return False
        self._logger.warning("Termination timeout. Killing (kill_timeout = %d)", self.kill_timeout)
        try:
            self.process.kill()
        except Exception:
            self._logger.error("Failed to kill job.", exc_info=True)
        return True

    def wait_exit_code(self):
        while True:
            time.sleep(0.1)
            self.time_elapsed += 0.1

            return_code = self.process.poll()
            if return_code is not None:
                return return_code

            self.check_terminate_timeout()
            if self.check_kill_timeout():
                return -9

    def run(self):
        """Run command with timeout"""
        logging.debug("Executing: '%r'", self.command)
        self.time_elapsed = 0
        self.terminated = False

        try:
            self.process = subprocess.Popen(
                self.command,
                stdout=sys.stdout,
                stderr=sys.stderr,
                shell=self.shell
            )
        except Exception:
            self._logger.warning("Execution of '%r' failed with:", self.command, exc_info=True)
            raise

        return self.wait_exit_code()

    def safe_run(self):
        try:
            return_code = self.run()
        except BaseException:
            self._logger.error("Error run job '%r'", self.command, exc_info=True)
            raise
        if return_code != 0:
            raise JobException("Job '{}' return code {}".format(self.command, return_code))

    def get_output(self):
        try:
            self.process = subprocess.Popen(
                self.command,
                stdout=subprocess.PIPE,
                stderr=sys.stderr,
                shell=self.shell
            )
        except Exception:
            self._logger.warning("Execution of '%r' failed with:", self.command, exc_info=True)
            raise

        return_code = self.wait_exit_code()
        output = self.process.stdout
        if return_code != 0:
            raise JobException("Job '{}' return code {}".format(self.command, return_code))
        return output


class BaseAction(object):

    def __init__(self, _args):
        super(BaseAction, self).__init__()
        self._logger = logger.getChild(self.__class__.__name__)

    def run(self):
        pass


class SSHAction(BaseAction):
    TIMEOUT = 60

    def __init__(self, args):
        super(SSHAction, self).__init__(None)
        if len(args.hosts.split(",")) != 1:
            self._logger.error("Multiple hosts for SSH: ", args.hosts, exc_info=True)
            raise ErrorExit()
        self.host = args.hosts
        self.dry_run = args.dry_run
        self.fail_on_error = args.fail_on_error

    def ssh_cmd(self, cmd):
        ssh_cmd = list()
        if self.dry_run:
            ssh_cmd.append("echo")
        ssh_cmd += ["ssh", self.host, cmd]
        job = Job(ssh_cmd, timeout=self.TIMEOUT)
        try:
            job.safe_run()
        except JobException:
            if self.fail_on_error:
                self._logger.error("Command '%s' failed", ssh_cmd, exc_info=True)
                raise ErrorExit()
        except Exception:
            raise ErrorExit()

    def run(self):
        super(SSHAction, self).run()


class PSSHAction(BaseAction):
    TIMEOUT = 5 * 60

    def __init__(self, args):
        super(PSSHAction, self).__init__(args)
        self.pssh_hosts = list()
        self.dry_run = args.dry_run
        self.hosts = args.hosts
        self.fail_on_error = args.fail_on_error
        self.username = args.username
        self.sudo_user = args.sudo_user

    def pssh_cmd(self, cmd):
        pssh_cmd = list()
        if self.dry_run:
            pssh_cmd.append("echo")
        pssh_cmd += ["pssh", "-p", "100", "--no-bastion", "--no-yubikey"] + cmd + self.pssh_hosts
        job = Job(pssh_cmd, timeout=self.TIMEOUT)
        try:
            job.safe_run()
        except JobException:
            if self.fail_on_error:
                self._logger.error("Command '%s' failed", pssh_cmd, exc_info=True)
                raise ErrorExit()
        except Exception:
            raise ErrorExit()

    def _get_base_pssh_args(self):
        if self.username is not None:
            return ["-u", self.username]
        return []

    def pssh_run(self, cmd, stream=False):
        pssh_cmd = ["run"]
        pssh_cmd += self._get_base_pssh_args()
        pssh_cmd.append(cmd)
        self.pssh_cmd(pssh_cmd)

    def pssh_upload(self, src, dst_dir):
        pssh_cmd = list()
        if self.dry_run:
            pssh_cmd.append("echo")

        hosts_with_dir = []
        for host in self.pssh_hosts:
            hosts_with_dir.append(host + ":" + dst_dir)

        pssh_cmd += ["pssh", "scp", "-p", "100", "--no-bastion", "--no-yubikey", src] + hosts_with_dir
        job = Job(pssh_cmd, timeout=self.TIMEOUT)
        try:
            job.safe_run()
        except JobException:
            if self.fail_on_error:
                self._logger.error("Command '%s' failed", pssh_cmd, exc_info=True)
                raise ErrorExit()
        except Exception:
            raise ErrorExit()


    def _select_hosts(self):
        if self.hosts is not None:
            # overwrite Nodes
            self.pssh_hosts.append(self.hosts)
        else:
            self.pssh_hosts = Nodes

        if len(self.pssh_hosts) == 0:
            self._logger.error("PSSH hosts list is empty. Need specify --config or/and --hosts")
            raise ErrorExit()

    def run(self):
        self._select_hosts()
        self._logger.info("PSSH hosts %s", " ".join(self.pssh_hosts))
        super(PSSHAction, self).run()


class Deploy(PSSHAction):

    def __init__(self, args):
        super(Deploy, self).__init__(args)
        self.package = args.deploy
        self.config = args.config

    @staticmethod
    def _get_mkdir_cmd():
        paths = [pipes.quote(os.path.join(DEPLOY_PATH, dirname)) for dirname in ("data", "logs")]
        return "sudo mkdir -p " + " ".join(paths)

    def run(self):
        super(Deploy, self).run()
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
        super(Init, self).__init__(args)
        self.sudo_user = args.sudo_user

    def run(self):
        super(Init, self).run()
        cockroach = DEPLOY_PATH + "/cockroach"

        cmd = "sudo -u {user} {cockroach} init --insecure --host={host}"
        cmd = cmd.format(user=self.sudo_user, host=self.host, cockroach=cockroach)

        self.ssh_cmd(cmd)


# actually not pssh action, but derives hosts and timeout
class Start(PSSHAction):

    def __init__(self, args):
        super(Start, self).__init__(args)
        self.task_set = args.task_set
        self.per_disk_instance = args.per_disk_instance

    def start_instance(self, host, store_args, listen_addr, http_addr, join_nodes, task_set, region, cache_size, sql_mem_size):
        self._logger.info("Start on " + host + ", addr " + listen_addr)
        now = datetime.datetime.now()
        join_nodes_str = ",".join(join_nodes)
        cmd = "sudo -u {user} sh -c \""
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

        cores_per_instance = Cores / len(Disks)
        cache_per_instance = CacheSizeGB / len(Disks)
        sql_mem_per_instance = SqlMemorySizeGB / len(Disks)
        for region in Regions:
            for host in region.Nodes:
                port = LISTEN_PORT
                http_port = HTTP_PORT
                start_core = 0
                for d in Disks:
                    store_args = "--store " + disk2mnt(d) + " "
                    listen_host = ":" + str(port)
                    http_listen = "localhost:" + str(http_port)
                    end_core = start_core + cores_per_instance - 1
                    task_set = str(start_core) + "-" + str(end_core)
                    self.start_instance(
                        host,
                        store_args,
                        listen_host,
                        http_listen,
                        join_nodes,
                        task_set,
                        region.Name,
                        cache_per_instance,
                        sql_mem_per_instance)
                    port += 1
                    http_port += 1
                    start_core += cores_per_instance

    def run(self):
        super(Start, self).run()

        if self.per_disk_instance:
            return self.run_per_disk()

        store_args = ""
        for d in Disks:
            store_args += "--store " + disk2mnt(d) + " "

        join_nodes = self.get_join_nodes()
        for region in Regions:
            for host in region.Nodes:
                http_listen = "localhost:" + str(HTTP_PORT)
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
        super(Stop, self).__init__(args)

    def run(self):
        super(Stop, self).run()
        self._logger.info("Stop")
        cmd = "sudo -u {user} sh -c \"pkill cockroach; pkill haproxy; sleep 1;"
        cmd += "pkill -9 cockroach; pkill -9 haproxy\""
        self.pssh_run(cmd.format(user=self.sudo_user))


class Clean(Stop):
    def __init__(self, args):
        super(Clean, self).__init__(args)

    def run(self):
        super(Clean, self).run()
        self._logger.info("Clean")

        disk_cmd = "sudo -u {user} umount {mnt} 2>&1"
        disk_cmds = []
        for disk in Disks:
            mount_point = disk2mnt(disk)
            disk_cmds.append(disk_cmd.format(mnt=mount_point, disk=disk, user=self.sudo_user))

        self.pssh_run(";".join(disk_cmds))


class Format(PSSHAction):

    def __init__(self, args):
        super(Format, self).__init__(args)

    def run(self):
        super(Format, self).run()
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


# [ control.py -c cluster_config.py stop ]
# control.py -c cluster_config.py format
# control.py -c cluster_config.py deploy
# control.py -c cluster_config.py start
# control.py -c cluster_config.py init
# --dry-run, --first-node-only
class Main(object):

    def __init__(self):
        super(Main, self).__init__()
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

        for cmd_name, cmd_cls in self.cmds.iteritems():
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
