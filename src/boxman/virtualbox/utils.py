import os
import sys
import time
import datetime as dt
from subprocess import Popen, PIPE
import shlex

from boxman import log

now = dt.datetime.fromtimestamp(time.time())


class Command(object):
    def __init__(self, cmd):
        self.cmd = cmd
        self.stdout = None
        self.stderr = None
        self.process = None

    def run(self,
            capture=False,
            show=True,
            asyncexec=False,
            check_returned_code=False,
            retry_n_time=0,
            *args, **kwargs):
        """
        Wrapper around Popen

        :param args: args passed to Popen
        :param kwargs: kwargs passed to Popen
        """
        if capture is True or show is False:
            pipe = PIPE
        else:
            pipe = None

        cmd = self.cmd
        log.info('>>> {}'.format(cmd))
        process = Popen(
            shlex.split(cmd),
            stdout=pipe,
            stderr=pipe,
            *args, **kwargs
        )
        self.process = process

        #if asyncexec:
        #    stdout, stderr = None, None
        #else:
        #    stdout, stderr = process.communicate()

        #if stdout is not None:
        #    stdout = stdout.decode()
        #if stderr is not None:
        #    stderr = stderr.decode()

        #self.stdout = stdout
        #self.stderr = stderr

        return self


def wait_procs(procs_list):
    """
    Block until all processes in the procs_list finish

    :param procs_list: a list of processes Popen
    """
    print('waiting ')
    while True:
        n_finished = 0
        for proc in procs_list:
            if proc.process.poll() is not None:
                n_finished += 1
        if n_finished == len(procs_list):
            break
        time.sleep(1)
        print('.', end='')
        sys.stdout.flush()
    print('\n')


class SshConfigGenerator:
    def __init__(self, vms, identity_file=None):
        self.vms = vms
        self.identity_file = identity_file

    def generate(self, path=None):
        prefix_indent = ' '*4
        with open(path, 'w') as fobj:
            for vm_name, vm_info in self.vms.items():
                fobj.write(f"Host {vm_info['hostname']}\n")
                fobj.write(f"{prefix_indent}Hostname localhost\n")
                fobj.write(f"{prefix_indent}User admin\n")
                fobj.write(f"{prefix_indent}Port {vm_info['access_port']}\n")
                fobj.write(f"{prefix_indent}StrictHostKeyChecking no\n")
                fobj.write(f"{prefix_indent}UserKnownHostsFile /dev/null\n")
                fobj.write(f"{prefix_indent}IdentityFile {self.identity_file}\n")


class AnsibleHelper:
    def __init__(self):
        pass
