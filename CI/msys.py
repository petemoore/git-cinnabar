import hashlib
import re

from tasks import (
    Task,
    TaskEnvironment,
    Tool,
    bash_command,
)
from docker import DockerImage


CPUS = ('x86_64',)
MSYS_VERSION = {
    'x86_64': '20220904',
}


def mingw(cpu):
    return {
        'x86_64': 'MINGW64',
    }.get(cpu)


def msys(cpu):
    return {
        'x86_64': 'msys64',
    }.get(cpu)


def msys_cpu(cpu):
    return cpu


def bits(cpu):
    return {
        'x86_64': '64',
    }.get(cpu)


class MsysCommon(object):
    os = 'windows'

    def prepare_params(self, params):
        assert 'workerType' not in params
        params['workerType'] = 'win2012r2'
        assert 'mounts' not in params
        params['mounts'] = [self]
        params.setdefault('env', {})['MSYSTEM'] = mingw(self.cpu)

        command = []
        command.append('set HOME=%CD%')
        command.append('set ARTIFACTS=%CD%')
        for path in (mingw(self.cpu), 'usr'):
            command.append('set PATH=%CD%\\{}\\{}\\bin;%PATH%'
                           .format(msys(self.cpu), path))
        command.append(
            'set PATH=%CD%\\git\\{}\\bin;%PATH%'.format(mingw(self.cpu)))
        if self.PREFIX != 'msys':
            command.append('bash -c -x "{}"'.format('; '.join((
                'for postinst in /etc/post-install/*.post',
                'do test -e $postinst && . $postinst',
                'done',
            ))))
        command.append(' '.join(
            _quote(arg) for arg in bash_command(*params['command'])))
        params['command'] = command
        return params

    @property
    def index(self):
        return '.'.join(('env', self.PREFIX, self.cpu, self.hexdigest))


class MsysBase(MsysCommon, Task, metaclass=Tool):
    PREFIX = "msys"

    def __init__(self, cpu):
        assert cpu in CPUS
        _create_command = (
            'curl -L http://repo.msys2.org/distrib/{cpu}'
            '/msys2-base-{cpu}-{version}.tar.xz | xz -cd | zstd -c'
            ' > $ARTIFACTS/msys2.tar.zst'.format(
                cpu=msys_cpu(cpu), version=MSYS_VERSION[cpu])
        )
        h = hashlib.sha1(_create_command.encode())
        self.hexdigest = h.hexdigest()
        self.cpu = cpu

        Task.__init__(
            self,
            task_env=DockerImage.by_name('base'),
            description='msys2 image: base {}'.format(cpu),
            index=self.index,
            expireIn='26 weeks',
            command=[_create_command],
            artifact='msys2.tar.zst',
        )


class MsysEnvironment(MsysCommon):
    def __init__(self, name):
        cpu = self.cpu
        create_commands = [
            'pacman-key --init',
            'pacman-key --populate msys2',
            'pacman --noconfirm -Sy procps tar {}'.format(
                ' '.join(self.packages(name))),
            'pkill gpg-agent',
            'rm -rf /var/cache/pacman/pkg',
            'python2.7 -m pip install pip==20.3.4 wheel==0.37.1 --upgrade',
            'python3 -m pip install pip==22.2.2 wheel==0.37.1 --upgrade',
            'mv {}/{}/bin/{{{{mingw32-,}}}}make.exe'.format(msys(cpu),
                                                            mingw(cpu)),
            'tar -c --hard-dereference {} | zstd -c > msys2.tar.zst'.format(
                msys(cpu)),
        ]

        env = MsysBase.by_name(cpu)

        h = hashlib.sha1(env.hexdigest.encode())
        h.update(';'.join(create_commands).encode())
        self.hexdigest = h.hexdigest()

        Task.__init__(
            self,
            task_env=env,
            description='msys2 image: {} {}'.format(name, cpu),
            index=self.index,
            expireIn='26 weeks',
            command=create_commands,
            artifact='msys2.tar.zst',
        )

    def packages(self, name):
        def mingw_packages(pkgs):
            return [
                'mingw-w64-{}-{}'.format(msys_cpu(self.cpu), pkg)
                for pkg in pkgs
            ]

        packages = mingw_packages([
            'curl',
            'make',
            'python2',
            'python2-pip',
            'python3',
            'python3-pip',
        ])

        if name == 'build':
            return packages + mingw_packages([
                'gcc',
            ]) + [
                'patch',
            ]
        elif name == 'test':
            return packages + [
                'diffutils',
                'git',
            ]
        raise Exception('Unknown name: {}'.format(name))


class Msys64Environment(MsysEnvironment, Task, metaclass=TaskEnvironment):
    PREFIX = 'mingw64'
    cpu = 'x86_64'
    __init__ = MsysEnvironment.__init__


SHELL_QUOTE_RE = re.compile(r'[\\\t\r\n \'\"#<>&|`~(){}$;\*\?]')


def _quote(s):
    if s and not SHELL_QUOTE_RE.search(s):
        return s
    for c in '^&\\<>|':
        s = s.replace(c, '^' + c)
    return "'{}'".format(s.replace("'", "'\\''"))
