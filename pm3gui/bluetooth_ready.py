"""Private Windows paired-SPP endpoint resolver. No COM/socket open or discovery.

The GUI runs this in a cancellable child process. Only the selected COM mapping
and cached authenticated peer are inspected. Resolution does not prove radio
readiness; the native client must establish RFCOMM and obtain a fresh OS reply.
"""
from __future__ import annotations

import argparse
import ctypes as c
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

_MAX_OUTPUT = 8192
_SPP_UUID = '00001101-0000-1000-8000-00805F9B34FB'
_FIELDS = {'proto', 'port', 'status', 'queries', 'elapsed_ms', 'last_error', 'channel', 'endpoint'}
_STATUSES = {'resolved', 'unavailable', 'not_bluetooth', 'error'}
# The existing Iceman Linux Bluetooth transport defaults to RFCOMM channel1.
# This is a transport default, not a claim that an SDP query succeeded.
_RFCOMM_CHANNEL = 1


def _port(value):
    if not isinstance(value, str) or not re.fullmatch(r'COM[1-9][0-9]{0,4}', value, re.I) or int(value[3:]) > 65535:
        raise ValueError('A single COM port is required.')
    return value.upper()


def _budget(value):
    if type(value) is not int or not 1 <= value <= 7000:
        raise ValueError('The readiness budget must be 1–7000 ms.')
    return value


def preparation_invocation(port: str, budget_ms: int = 7000, result_path=None) -> tuple[str, list[str], str]:
    port, budget_ms = _port(port), _budget(budget_ms)
    frozen = bool(getattr(sys, 'frozen', False))
    program = sys.executable
    if sys.platform == 'win32' and not frozen:
        # A Windows venv python.exe is a redirector that spawns the real Python
        # process. Owning the base executable lets Stop/timeout kill the worker
        # itself. The caller supplies trusted __PYVENV_LAUNCHER__ for its venv.
        program = getattr(sys, '_base_executable', None)
        if not program or not Path(program).is_file():
            raise ValueError('The underlying Python executable is unavailable.')
    executable = str(Path(program).resolve())
    if frozen:
        cwd = str(Path(executable).parent)
        arguments = []
    else:
        root = Path(__file__).resolve().parents[1]
        cwd = str(root)
        arguments = [str(root/'run_gui.py')]
    arguments += ['--bluetooth-service-probe', '--port', port, '--budget-ms', str(budget_ms)]
    if result_path is not None:
        path = Path(result_path)
        if not path.is_absolute() or any(c in str(path) for c in '\0\r\n'):
            raise ValueError('The readiness result requires an absolute path.')
        arguments += ['--result', str(path)]
    return executable, arguments, cwd


def _unique_pairs(items):
    result = {}
    for key, value in items:
        if key in result:raise ValueError('Duplicate readiness field.')
        result[key] = value
    return result


def parse_probe_result(output: bytes, port: str) -> dict:
    expected = _port(port)
    if not isinstance(output, bytes) or not 1 <= len(output) <= _MAX_OUTPUT:
        raise ValueError('Invalid readiness output size.')
    try:
        result = json.loads(output.decode('utf8'), object_pairs_hook=_unique_pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite JSON.')))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('Invalid readiness JSON.') from exc
    if not isinstance(result, dict) or set(result) != _FIELDS:
        raise ValueError('Unexpected readiness fields.')
    if type(result['proto']) is not int or result['proto'] != 2 or result['port'] != expected:
        raise ValueError('Readiness protocol or port mismatch.')
    if not isinstance(result['status'], str) or result['status'] not in _STATUSES:
        raise ValueError('Unknown readiness status.')
    if type(result['queries']) is not int or result['queries'] != 0:
        raise ValueError('Invalid readiness query count.')
    if type(result['elapsed_ms']) is not int or not 0 <= result['elapsed_ms'] <= 60000:
        raise ValueError('Invalid readiness duration.')
    error, channel, endpoint = result['last_error'], result['channel'], result['endpoint']
    if error is not None and (type(error) is not int or not 1 <= error <= 0xffffffff):
        raise ValueError('Invalid endpoint-resolution error code.')
    if result['status'] == 'resolved':
        if error is not None or type(channel) is not int or channel != _RFCOMM_CHANNEL:
            raise ValueError('Resolved endpoint has an invalid RFCOMM default.')
        if not isinstance(endpoint, str) or not re.fullmatch(r'bt:[0-9A-F]{12}', endpoint):
            raise ValueError('Invalid private Bluetooth endpoint.')
        if not 0 < int(endpoint[3:], 16) < 0xffffffffffff:
            raise ValueError('Invalid remote Bluetooth address.')
    elif channel is not None or endpoint is not None:
        raise ValueError('An unsuccessful resolution cannot supply an endpoint.')
    if result['status'] == 'not_bluetooth' and error is not None:
        raise ValueError('An unrelated port cannot have a Bluetooth query result.')
    return result


def _resolve_port(port, ports):
    matches = [item for item in ports if str(getattr(item, 'device', '')).upper() == port]
    if not matches:return 'unavailable', None, 1168
    if len(matches) != 1:return 'error', None, None
    hwid = str(getattr(matches[0], 'hwid', '')).upper()
    if not hwid.startswith('BTHENUM\\') or _SPP_UUID not in hwid:
        return 'not_bluetooth', None, None
    addresses = re.findall(r'(?<![0-9A-F])([0-9A-F]{12})(?=_C[0-9A-F]+(?:$|[\\\s]))', hwid)
    if len(addresses) != 1:return 'error', None, None
    address = int(addresses[0], 16)
    if not 0 < address < 0xffffffffffff:return 'not_bluetooth', None, None
    return 'candidate', address, None


def _result(port, status, started, clock, error=None, endpoint=None):
    return {'proto': 2, 'port': port, 'status': status, 'queries': 0,
            'elapsed_ms': max(0, int((clock()-started)*1000)), 'last_error': error,
            'channel': _RFCOMM_CHANNEL if endpoint is not None else None, 'endpoint': endpoint}


def _probe(port, budget_ms, backend, *, clock=time.monotonic):
    """Resolve only cached identity. Backend injection keeps tests offline."""
    port, budget_ms = _port(port), _budget(budget_ms)
    started = clock()
    def finish(status, error=None, endpoint=None):
        return _result(port, status, started, clock, error, endpoint)
    status, address, error = _resolve_port(port, backend.ports())
    if status != 'candidate':return finish(status, error)
    error, remembered, authenticated = backend.peer_info(address)
    if error:return finish('unavailable', error)
    if not remembered or not authenticated:return finish('unavailable')
    current, current_address, _ = _resolve_port(port, backend.ports())
    if current != 'candidate' or current_address != address:return finish('error')
    if (clock()-started)*1000 >= budget_ms:return finish('unavailable')
    return finish('resolved', endpoint=f'bt:{address:012X}')


# Fixed-width declarations keep Windows ABI layout testable on other platforms.
_DWORD, _WORD, _INT = c.c_uint32, c.c_uint16, c.c_int32


class _SystemTime(c.Structure):
    _fields_ = [(name, _WORD) for name in ('year', 'month', 'weekday', 'day', 'hour', 'minute', 'second', 'ms')]


class _DeviceInfo(c.Structure):
    _fields_ = [('size', _DWORD), ('address', c.c_uint64), ('device_class', _DWORD),
                ('connected', _INT), ('remembered', _INT), ('authenticated', _INT),
                ('seen', _SystemTime), ('used', _SystemTime), ('name', _WORD*248)]


class _WindowsBackend:
    """Lazy cached Bluetooth metadata only; no Winsock, SDP or device writes."""
    def __init__(self):
        if sys.platform != 'win32':raise OSError('Windows Bluetooth APIs are required.')
        self.bt = c.WinDLL('bthprops.cpl', use_last_error=True)
        self.bt.BluetoothGetDeviceInfo.argtypes = [c.c_void_p, c.POINTER(_DeviceInfo)]
        self.bt.BluetoothGetDeviceInfo.restype = _DWORD

    def close(self):
        pass  # No radio, lookup, socket or COM handle was acquired.

    @staticmethod
    def ports():
        from serial.tools import list_ports
        return list_ports.comports()

    def peer_info(self, address):
        device = _DeviceInfo(size=c.sizeof(_DeviceInfo), address=address)
        code = int(self.bt.BluetoothGetDeviceInfo(None, c.byref(device)))
        if device.address != address:return 87, False, False
        return code, bool(device.remembered), bool(device.authenticated)


class _Parser(argparse.ArgumentParser):
    def error(self, message):raise ValueError('Invalid readiness arguments.')


def _write_result(path, payload):
    """Commit a new file atomically; an existing file/link is never replaced."""
    path = Path(path)
    if not path.is_absolute() or any(char in str(path) for char in '\0\r\n'):
        raise ValueError('Invalid readiness result path.')
    parent = path.parent.resolve(strict=True)
    if not parent.is_dir():raise ValueError('Result directory is missing.')
    target = parent/path.name
    if target.exists() or target.is_symlink():raise FileExistsError('Readiness result already exists.')
    fd, temporary = tempfile.mkstemp(prefix='.pm3-bt-', suffix='.tmp', dir=parent)
    try:
        with os.fdopen(fd, 'wb') as stream:stream.write(payload)
        if os.name == 'nt':
            os.rename(temporary, target)  # Windows rename fails if target exists.
        else:
            os.link(temporary, target)  # POSIX rename would replace target.
    finally:
        Path(temporary).unlink(missing_ok=True)


def probe_main(argv=None) -> int:
    """Machine-only entry point; never imports Qt or opens a user profile."""
    started = time.monotonic();port = '';backend = None;result_path = None
    try:
        parser = _Parser(add_help=False)
        parser.add_argument('--port', required=True)
        parser.add_argument('--budget-ms', type=int, default=7000)
        parser.add_argument('--result', type=Path)
        args = parser.parse_args(argv)
        result_path = args.result
        port, budget = _port(args.port), _budget(args.budget_ms)
        if sys.platform != 'win32':
            result = _result(port, 'not_bluetooth', started, time.monotonic)
        else:
            backend = _WindowsBackend()
            result = _probe(port, budget, backend)
    except Exception as exc:
        code = getattr(exc, 'winerror', None) or getattr(exc, 'errno', None)
        if type(code) is not int or not 1 <= code <= 0xffffffff:code = 87 if isinstance(exc, ValueError) else 1
        result = _result(port, 'error', started, time.monotonic, code)
    finally:
        if backend is not None:
            try:backend.close()
            except Exception:result = _result(port, 'error', started, time.monotonic, 1)
    # A resolved address is private IPC data. It never reaches stdout/stderr,
    # including when this helper is invoked manually from a console.
    if result['status'] == 'resolved' and result_path is None:
        result = _result(port, 'error', started, time.monotonic, 87)
    payload = json.dumps(result, ensure_ascii=True, separators=(',', ':')).encode('ascii')
    if result_path is not None:
        try:_write_result(result_path, payload)
        except (OSError, ValueError):
            result = _result(port, 'error', started, time.monotonic, 1)
            payload = json.dumps(result, ensure_ascii=True, separators=(',', ':')).encode('ascii')
    if sys.stdout is not None and result['status'] != 'resolved':
        print(payload.decode('ascii'), flush=True)
    return 0 if result['status'] == 'resolved' else 1
