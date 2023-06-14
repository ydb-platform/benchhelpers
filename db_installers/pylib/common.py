#!/usr/bin/env python3
# -*- coding: UTF-8 -*-

import logging
import subprocess
import sys
import time


logger = logging.getLogger(__name__)
Nodes = []


def set_return_message(message):
    def decorator(obj):
        def wrapper(*args, **kwargs):
            obj.return_message = message
            return obj(*args, **kwargs)
        return wrapper
    return decorator


class ErrorExit(Exception):
    pass


class JobException(Exception):
    pass


class Job(object):

    def __init__(self, command, timeout=3600, kill_timeout=5, shell=False):
        super().__init__()
        self._logger = logger.getChild(self.__class__.__name__)
        if isinstance(command, str):
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
        super().__init__()
        self._logger = logger.getChild(self.__class__.__name__)

    def run(self):
        pass


class SSHAction(BaseAction):
    TIMEOUT = 60

    def __init__(self, args):
        super().__init__(None)
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
        super().run()


class PSSHAction(BaseAction):
    TIMEOUT = 5 * 60

    def __init__(self, args):
        super().__init__(args)
        self.pssh_hosts = ""
        self.dry_run = args.dry_run
        self.hosts = args.hosts
        self.fail_on_error = args.fail_on_error
        self.username = args.username
        self.sudo_user = args.sudo_user

    def pssh_cmd(self, cmd, add_hosts=None):
        pssh_cmd = list()
        if self.dry_run:
            pssh_cmd.append("echo")
        pssh_cmd += ["parallel-ssh", "-p", "100", "-H", self._get_hosts(add_hosts)] + cmd
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
            return ["-l", self.username]
        return []

    def pssh_run(self, cmd, add_hosts=None):
        pssh_cmd = self._get_base_pssh_args()
        pssh_cmd.append(cmd)
        self.pssh_cmd(pssh_cmd, add_hosts)

    def pssh_upload(self, src, dst_dir):
        pssh_cmd = list()
        if self.dry_run:
            pssh_cmd.append("echo")

        pssh_cmd += ["parallel-scp", "-p", "100", "-H", self._get_hosts(), src, dst_dir]
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
            self.pssh_hosts = self.hosts.split()
        else:
            self.pssh_hosts = Nodes

        if len(self.pssh_hosts) == 0:
            self._logger.error("PSSH hosts list is empty. Need specify --config or/and --hosts")
            raise ErrorExit()

    def _get_hosts(self, add_hosts=None):
        hosts = self.pssh_hosts

        if add_hosts:
            if not isinstance(add_hosts, list):
                raise ValueError(f"'{add_hosts}' is not list.")
            hosts += add_hosts

        return ' '.join(set(hosts))

    def run(self):
        self._select_hosts()
        self._logger.info("PSSH hosts %s", " ".join(self.pssh_hosts))
        super().run()
