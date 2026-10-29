#!/usr/bin/env python3
"""Hand existing desktop sessions to the packaged phone backend, keeping data."""
import argparse
import configparser
import os
from pathlib import Path
import pwd
import shlex
import signal
import subprocess
import time

SYSTEM_UNIT = Path('/usr/lib/systemd/user/rust-connect.service')
LEGACY_UNIT = Path('/usr/share/rust-connect/legacy-user.service')
NATIVE_KDE = {'/usr/bin/kdeconnectd', '/usr/lib/kdeconnectd', '/usr/libexec/kdeconnectd', '/usr/lib64/kdeconnectd'}
KDE_UNITS = ('kdeconnect.service', 'kdeconnectd.service', 'plasma-kdeconnect.service',
             'app-org.kde.kdeconnect.daemon@autostart.service')


def manager(*args, check=False):
    env = dict(os.environ)
    runtime = Path(env.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}')
    if not (runtime / 'bus').exists():
        env['SYSTEMD_OFFLINE'] = '1'
    return subprocess.run(['systemctl', '--user', *args], capture_output=True,
                          text=True, timeout=20, check=check, env=env)


def backup(path):
    destination = path.with_name(path.name + '.pre-rust-connect')
    suffix = 0
    while destination.exists() or destination.is_symlink():
        suffix += 1
        destination = path.with_name(path.name + f'.pre-rust-connect.{suffix}')
    path.rename(destination)
    print(f'rust-connect: kept {path} as {destination}', flush=True)


def entry_command(path, section):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read_string(path.read_text())
    return parser[section]


def legacy_files(home):
    """Recognize only shipped legacy files. Unknown activation overrides block takeover."""
    config = Path(os.environ.get('XDG_CONFIG_HOME') or home / '.config')
    data = Path(os.environ.get('XDG_DATA_HOME') or home / '.local/share')
    unit = config / 'systemd/user/rust-connect.service'
    activation = data / 'dbus-1/services/org.kde.kdeconnect.service'
    moves = []
    for dropin in (config / 'systemd/user/rust-connect.service.d').glob('*.conf'):
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.read_string(dropin.read_text())
        if parser.has_option('Service', 'ExecStart'):
            raise RuntimeError(f'custom service command left unchanged: {dropin}')
    if unit.exists() or unit.is_symlink():
        if not unit.is_symlink() and unit.read_bytes() == LEGACY_UNIT.read_bytes():
            moves.append(unit)
        elif not unit.is_symlink() and unit.read_bytes() == SYSTEM_UNIT.read_bytes():
            pass  # An explicit override that already uses the packaged backend.
        else:
            raise RuntimeError(f'custom service override left unchanged: {unit}')
    allowed = NATIVE_KDE | {'/usr/bin/rust-connect', str(home / '.local/bin/rust-connect')}
    if activation.exists() or activation.is_symlink():
        if activation.is_symlink():
            raise RuntimeError(f'custom D-Bus activation left unchanged: {activation}')
        section = entry_command(activation, 'D-BUS Service')
        if section.get('Name') != 'org.kde.kdeconnect' or shlex.split(section.get('Exec', '')) not in [[p] for p in allowed]:
            raise RuntimeError(f'custom D-Bus activation left unchanged: {activation}')
        moves.append(activation)
    for name in ('kdeconnectd', 'kdeconnect-app'):
        path = home / '.local/bin' / name
        reference = Path('/usr/bin') / name
        if path.is_file() and not path.is_symlink() and path.read_bytes() == reference.read_bytes():
            moves.append(path)
    autostart = config / 'autostart/org.kde.kdeconnect.daemon.desktop'
    if autostart.is_file() and not autostart.is_symlink():
        section = entry_command(autostart, 'Desktop Entry')
        if shlex.split(section.get('Exec', '')) in [[p] for p in NATIVE_KDE]:
            moves.append(autostart)
    return moves


def owner_pid():
    owner = subprocess.run(['busctl', '--user', '--timeout=2', 'call', 'org.freedesktop.DBus',
                            '/org/freedesktop/DBus', 'org.freedesktop.DBus',
                            'GetConnectionUnixProcessID', 's', 'org.kde.kdeconnect'],
                           capture_output=True, text=True, timeout=4)
    fields = owner.stdout.split()
    return int(fields[1]) if len(fields) == 2 and fields[0] == 'u' else None


def old_daemons(home):
    """Find native KDE daemons and the registered legacy Rust backend, never CLI clients."""
    bus_pid = owner_pid()
    handles = []
    try:
        for path in Path('/proc').iterdir():
            if not path.name.isdigit():
                continue
            fd = None
            try:
                pid = int(path.name)
                fd = os.pidfd_open(pid)
                if path.stat().st_uid != os.getuid():
                    continue
                executable = os.readlink(path / 'exe').removesuffix(' (deleted)')
                if executable in NATIVE_KDE or (pid == bus_pid and executable in {
                    '/usr/bin/rust-connect', str(home / '.local/bin/rust-connect'),
                }):
                    handles.append(fd)
                    fd = None
            except (OSError, ProcessLookupError):
                pass
            finally:
                if fd is not None:
                    os.close(fd)
        return handles
    except BaseException:
        for fd in handles:
            os.close(fd)
        raise


def migrate(home, upgrade=False):
    state = manager('is-enabled', 'rust-connect.service').stdout.strip()
    if state in ('masked', 'masked-runtime'):
        print('rust-connect: service is masked; preserving the user choice.', flush=True)
        return
    # Validate before stopping anything; preserve a custom service/activation pair.
    moves = legacy_files(home)
    runtime = Path(os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}')
    online = (runtime / 'bus').exists()
    running = online and manager('is-active', '--quiet', 'rust-connect.service').returncode == 0
    should_start = not upgrade or running or state in ('enabled', 'enabled-runtime')
    handles = old_daemons(home) if online else []
    try:
        if online:
            for unit in KDE_UNITS:
                if manager('is-active', '--quiet', unit).returncode == 0:
                    manager('stop', unit, check=True)
                    should_start = True
            if handles:
                should_start = True
            if running:
                manager('stop', 'rust-connect.service', check=True)
            # pidfds cannot be redirected to a reused PID after a unit exits.
            import select
            poll = select.poll()
            pending = set()
            for fd in handles:
                try:
                    signal.pidfd_send_signal(fd, signal.SIGTERM)
                except ProcessLookupError:
                    continue
                poll.register(fd, select.POLLIN)
                pending.add(fd)
            deadline = time.monotonic() + 5
            while pending and time.monotonic() < deadline:
                for fd, _ in poll.poll(100):
                    pending.discard(fd)
            if pending:
                raise RuntimeError('old phone backend did not stop; log out before starting Rust Connect')
        for path in moves:
            backup(path)
        if should_start:
            # Also works for an offline account, without connecting to its bus.
            manager('--no-reload', 'reenable', 'rust-connect.service', check=True)
        if online:
            manager('daemon-reload', check=True)
            if should_start:
                manager('start', 'rust-connect.service', check=True)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    pid = owner_pid()
                    main = manager('show', 'rust-connect.service', '--property=MainPID', '--value').stdout.strip()
                    if pid and str(pid) == main:
                        break
                    time.sleep(.2)
                else:
                    raise RuntimeError('packaged service did not acquire org.kde.kdeconnect; check its user-service journal')
        print(f'rust-connect: backend migration complete for {home}; phone data unchanged.', flush=True)
    finally:
        for fd in handles:
            os.close(fd)


def all_users(upgrade):
    if os.geteuid() != 0:
        raise RuntimeError('--all-users requires root')
    result = 0
    for account in pwd.getpwall():
        if account.pw_uid < 1000 or account.pw_uid == 65534:
            continue
        home = Path(account.pw_dir)
        runtime = Path(f'/run/user/{account.pw_uid}')
        legacy = [home / '.config/systemd/user/rust-connect.service',
                  home / '.local/share/dbus-1/services/org.kde.kdeconnect.service',
                  home / '.config/autostart/org.kde.kdeconnect.daemon.desktop']
        if not (runtime / 'bus').exists() and not any(p.exists() for p in legacy):
            continue
        env = {**os.environ, 'HOME': str(home), 'XDG_RUNTIME_DIR': str(runtime),
               'DBUS_SESSION_BUS_ADDRESS': f'unix:path={runtime}/bus'}
        for key in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME'):
            env.pop(key, None)
        command = ['runuser', '-u', account.pw_name, '--', '/usr/bin/python3', '-I',
                   str(Path(__file__).resolve())]
        if upgrade:
            command.append('--upgrade')
        if subprocess.run(command, env=env, check=False).returncode:
            result = 1
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--all-users', action='store_true')
    parser.add_argument('--upgrade', action='store_true')
    args = parser.parse_args()
    try:
        if args.all_users:
            raise SystemExit(all_users(args.upgrade))
        migrate(Path.home(), args.upgrade)
    except (OSError, ValueError, configparser.Error, subprocess.SubprocessError, RuntimeError) as error:
        print(f'rust-connect: {error}', flush=True)
        raise SystemExit(1)
