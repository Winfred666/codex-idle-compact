"""Small lifecycle hook, private signal files, and owner-socket verification."""
import fcntl
import json
import math
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import sys
import time
import uuid

EVENTS = {'SessionStart', 'UserPromptSubmit', 'Stop', 'SessionEnd', 'PreCompact', 'PostCompact', 'Interrupt'}


def codex_home():
    return Path(os.environ.get('CODEX_HOME', '~/.codex')).expanduser()


def settings():
    path = codex_home() / 'codex-idle-compact.json'
    config = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(config, dict):
        raise ValueError('configuration must be an object')
    minutes = config.get('idleMinutes', 20)
    if type(minutes) not in (int, float) or not math.isfinite(minutes * 60) or minutes <= 0:
        raise ValueError('idleMinutes must be finite and positive')
    endpoint = Path(config.get('socket', str(codex_home() / 'app-server-control/app-server-control.sock'))).expanduser()
    if not endpoint.is_absolute():
        raise ValueError('socket must be absolute')
    return minutes * 60, endpoint


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('plugin data must be an owned directory')
    path.chmod(0o700)
    return path


def lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    handle = os.fdopen(fd, 'w')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        handle.close()
        raise
    return handle


def owner_socket(path):
    """Return a connected socket and kernel-reported peer PID; never guess ownership."""
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.settimeout(1)
        sock.connect(str(path))
        if sys.platform == 'darwin':
            pid = sock.getsockopt(0, 2)  # SOL_LOCAL, LOCAL_PEERPID (Darwin).
        elif sys.platform.startswith('linux'):
            pid, uid, _ = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid():
                raise ValueError('server is owned by another user')
        else:
            raise ValueError('Linux and macOS are supported')
        if pid <= 0:
            raise ValueError('owner PID unavailable')
        return sock, pid
    except BaseException:
        sock.close()
        raise


def ancestors():
    pid = os.getppid()
    result = set()
    while pid > 1 and pid not in result:
        result.add(pid)
        if sys.platform.startswith('linux'):
            raw = Path('/proc/{}/stat'.format(pid)).read_text()
            pid = int(raw.rsplit(')', 1)[1].split()[1])
        else:
            raw = subprocess.run(['ps', '-o', 'ppid=', '-p', str(pid)], capture_output=True,
                                 text=True, check=True, timeout=1).stdout.strip()
            pid = int(raw)
    return result


def read_signal(path):
    if path.stat().st_size > 4096:
        raise ValueError('oversized hook signal')
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError('signal must be an object')
    if (str(uuid.UUID(value['threadId'])) != value['threadId'] or value['event'] not in EVENTS
            or type(value['ownerPid']) is not int or value['ownerPid'] <= 0
            or type(value['stamp']) is not int or type(value['at']) not in (int, float)
            or not math.isfinite(value['at']) or not isinstance(value['socket'], str)):
        raise ValueError('invalid hook signal')
    if value.get('turnId') is not None:
        uuid.UUID(value['turnId'])
    return value


def hook():
    payload = {}
    try:
        payload = json.loads(sys.stdin.buffer.read(1024 * 1024))
        event = payload['hook_event_name']
        if event not in EVENTS:
            return {}
        thread = str(uuid.UUID(payload['session_id']))
        turn = payload.get('turn_id')
        if turn is not None:
            turn = str(uuid.UUID(turn))
        _, endpoint = settings()
        sock, pid = owner_socket(endpoint)
        sock.close()
        if pid not in ancestors():
            raise ValueError('socket does not belong to this session')
        data = private_directory(Path(os.environ.get('PLUGIN_DATA', str(codex_home() / 'codex-idle-compact'))))
        signals = private_directory(data / 'sessions')
        value = {'threadId': thread, 'turnId': turn, 'event': event, 'socket': str(endpoint),
                 'ownerPid': pid, 'at': time.monotonic(), 'stamp': time.time_ns()}
        path = signals / (thread + '.json')
        temporary = signals / (thread + '.' + str(os.getpid()) + '.tmp')
        try:
            with temporary.open('x') as output:
                json.dump(value, output)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        try:
            with lock(data / 'watcher.lock'):
                pass
        except BlockingIOError:
            return {}
        subprocess.Popen([sys.executable, str(Path(__file__).with_name('codex_idle_compact.py')),
                          'watch', '--data', str(data)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
        return {}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        if isinstance(payload, dict) and payload.get('hook_event_name') == 'SessionStart':
            return {'systemMessage': 'codex-idle-compact: this session has no verified owning daemon socket or valid plugin configuration. Start a new daemon-backed Codex session to enable idle compaction. No session was restarted. See the plugin README.'}
        return {}
