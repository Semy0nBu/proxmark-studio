"""Private Windows SPP readiness worker. No COM open, pairing or device changes.

The GUI runs this in a cancellable child process because Windows SDP calls can
block inside the Bluetooth stack. A fresh service response is preparation for
the native client's first COM open, never proof of a working Proxmark session.
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
_MAX_QUERIES = 16
_SPP_UUID = '00001101-0000-1000-8000-00805F9B34FB'
_FIELDS = {'proto', 'port', 'status', 'queries', 'elapsed_ms', 'last_error', 'channel'}
_STATUSES = {'ready', 'unavailable', 'not_bluetooth', 'error'}
_TRANSIENT = {10050, 10051, 10053, 10054, 10060, 10064, 10065, 10101, 10108, 10110}
# winsock2.h constants, checked against the SDK used by the native diagnostic.
# FLUSHPREVIOUS is0x2000 and would incorrectly skip the first SPP service record.
_LUP_RETURN_NAME = 0x0010
_LUP_RETURN_ADDR = 0x0100
_LUP_FLUSHCACHE = 0x1000
_SDP_FLAGS = _LUP_RETURN_NAME | _LUP_RETURN_ADDR | _LUP_FLUSHCACHE


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
    if type(result['proto']) is not int or result['proto'] != 1 or result['port'] != expected:
        raise ValueError('Readiness protocol or port mismatch.')
    if not isinstance(result['status'], str) or result['status'] not in _STATUSES:
        raise ValueError('Unknown readiness status.')
    if type(result['queries']) is not int or not 0 <= result['queries'] <= _MAX_QUERIES:
        raise ValueError('Invalid readiness query count.')
    if type(result['elapsed_ms']) is not int or not 0 <= result['elapsed_ms'] <= 60000:
        raise ValueError('Invalid readiness duration.')
    error, channel = result['last_error'], result['channel']
    if error is not None and (type(error) is not int or not 1 <= error <= 0xffffffff):
        raise ValueError('Invalid readiness error code.')
    if result['status'] == 'ready':
        if result['queries'] < 1 or error is not None or type(channel) is not int or not 1 <= channel <= 30:
            raise ValueError('Ready lacks a valid remote RFCOMM channel.')
    elif channel is not None:
        raise ValueError('An unsuccessful probe cannot supply a channel.')
    if result['status'] == 'not_bluetooth' and (result['queries'] != 0 or error is not None):
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


def _result(port, status, queries, started, clock, error=None, channel=None):
    return {'proto': 1, 'port': port, 'status': status, 'queries': queries,
            'elapsed_ms': max(0, int((clock()-started)*1000)), 'last_error': error, 'channel': channel}


def _probe(port, budget_ms, backend, *, clock=time.monotonic, sleep=time.sleep):
    """Backend is injected for offline tests; it must not open a COM/socket."""
    port, budget_ms = _port(port), _budget(budget_ms)
    started = clock(); queries = 0; last_error = None
    def finish(status, error=None, channel=None):
        return _result(port, status, queries, started, clock, error, channel)
    status, address, error = _resolve_port(port, backend.ports())
    if status != 'candidate':return finish(status, error)
    error, remembered, authenticated = backend.peer_info(address)
    if error:return finish('unavailable', error)
    if not remembered or not authenticated:return finish('unavailable')
    deadline = started+budget_ms/1000
    while clock() < deadline and queries < _MAX_QUERIES:
        queries += 1
        channel, last_error = backend.query(address)
        if clock() >= deadline:return finish('unavailable', 10060)
        if last_error is None:
            if type(channel) is not int or not 1 <= channel <= 30:return finish('error', 10022)
            current, current_address, _ = _resolve_port(port, backend.ports())
            if current != 'candidate' or current_address != address:return finish('error')
            return finish('ready', channel=channel)
        if last_error not in _TRANSIENT:return finish('error', last_error)
        remaining = deadline-clock()
        if remaining > 0:sleep(min(.65, remaining))
    return finish('unavailable', last_error)


# Fixed-width declarations keep Windows ABI layout testable on other platforms.
_DWORD, _WORD, _INT = c.c_uint32, c.c_uint16, c.c_int32


class _GUID(c.Structure):
    _fields_ = [('Data1', _DWORD), ('Data2', _WORD), ('Data3', _WORD), ('Data4', c.c_ubyte*8)]


class _SockaddrBth(c.Structure):
    _pack_ = 1
    _fields_ = [('family', _WORD), ('address', c.c_uint64), ('service', _GUID), ('port', _DWORD)]


class _SocketAddress(c.Structure):
    _fields_ = [('address', c.c_void_p), ('length', _INT)]


class _CsAddr(c.Structure):
    _fields_ = [('local', _SocketAddress), ('remote', _SocketAddress), ('socket_type', _INT), ('protocol', _INT)]


class _QuerySet(c.Structure):
    _fields_ = [('size', _DWORD), ('name', c.c_wchar_p), ('service', c.POINTER(_GUID)),
                ('version', c.c_void_p), ('comment', c.c_wchar_p), ('namespace', _DWORD),
                ('provider', c.c_void_p), ('context', c.c_wchar_p), ('protocol_count', _DWORD),
                ('protocols', c.c_void_p), ('query', c.c_wchar_p), ('address_count', _DWORD),
                ('addresses', c.POINTER(_CsAddr)), ('flags', _DWORD), ('blob', c.c_void_p)]


class _SystemTime(c.Structure):
    _fields_ = [(name, _WORD) for name in ('year', 'month', 'weekday', 'day', 'hour', 'minute', 'second', 'ms')]


class _DeviceInfo(c.Structure):
    _fields_ = [('size', _DWORD), ('address', c.c_uint64), ('device_class', _DWORD),
                ('connected', _INT), ('remembered', _INT), ('authenticated', _INT),
                ('seen', _SystemTime), ('used', _SystemTime), ('name', _WORD*248)]


class _WindowsBackend:
    """Lazy Windows calls. The socket APIs here perform SDP lookup only."""
    def __init__(self):
        if sys.platform != 'win32':raise OSError('Windows Bluetooth APIs are required.')
        self.ws = c.WinDLL('ws2_32', use_last_error=True)
        self.bt = c.WinDLL('bthprops.cpl', use_last_error=True)
        declarations = {
            'WSAStartup': ([ _WORD, c.c_void_p], _INT),
            'WSACleanup': ([], _INT), 'WSAGetLastError': ([], _INT),
            'WSAAddressToStringW': ([c.c_void_p, _DWORD, c.c_void_p, c.c_wchar_p, c.POINTER(_DWORD)], _INT),
            'WSALookupServiceBeginW': ([c.POINTER(_QuerySet), _DWORD, c.POINTER(c.c_void_p)], _INT),
            'WSALookupServiceNextW': ([c.c_void_p, _DWORD, c.POINTER(_DWORD), c.c_void_p], _INT),
            'WSALookupServiceEnd': ([c.c_void_p], _INT),
        }
        for name, (args, result) in declarations.items():
            function = getattr(self.ws, name);function.argtypes = args;function.restype = result
        self.bt.BluetoothGetDeviceInfo.argtypes = [c.c_void_p, c.POINTER(_DeviceInfo)]
        self.bt.BluetoothGetDeviceInfo.restype = _DWORD
        # WSADATA is opaque here;512bytes covers32/64-bit Windows layouts.
        self._data = c.create_string_buffer(512)
        code = self.ws.WSAStartup(0x202, self._data)
        if code:raise OSError(code, 'Winsock initialization failed.')
        self.active = True

    def close(self):
        if self.active:self.active = False;self.ws.WSACleanup()

    @staticmethod
    def ports():
        from serial.tools import list_ports
        return list_ports.comports()

    def peer_info(self, address):
        device = _DeviceInfo(size=c.sizeof(_DeviceInfo), address=address)
        code = int(self.bt.BluetoothGetDeviceInfo(None, c.byref(device)))
        if device.address != address:return 87, False, False
        return code, bool(device.remembered), bool(device.authenticated)

    @staticmethod
    def _channel(buffer, capacity, address):
        low = c.addressof(buffer); high = low+capacity
        def within(pointer, size):
            return pointer is not None and low <= pointer <= high and 0 <= size <= high-pointer
        answer = _QuerySet.from_buffer(buffer)
        count = int(answer.address_count)
        pointer = c.cast(answer.addresses, c.c_void_p).value
        if answer.size != c.sizeof(_QuerySet) or not 1 <= count <= 16 or not within(pointer, count*c.sizeof(_CsAddr)):
            return None, 10022
        channels = set()
        for index in range(count):
            item = _CsAddr.from_address(pointer+index*c.sizeof(_CsAddr))
            if item.remote.length < c.sizeof(_SockaddrBth) or not within(item.remote.address, c.sizeof(_SockaddrBth)):
                return None, 10022
            remote = _SockaddrBth.from_buffer_copy(c.string_at(item.remote.address, c.sizeof(_SockaddrBth)))
            if remote.family != 32 or remote.address != address or not 1 <= remote.port <= 30:
                return None, 10022
            channels.add(int(remote.port))
        return (channels.pop(), None) if len(channels) == 1 else (None, 10022)

    def query(self, address):
        import uuid
        service = _GUID.from_buffer_copy(uuid.UUID(_SPP_UUID).bytes_le)
        printable = _SockaddrBth(family=32, address=address)
        context = c.create_unicode_buffer(128); length = _DWORD(len(context))
        if self.ws.WSAAddressToStringW(c.byref(printable), c.sizeof(printable), None, context, c.byref(length)):
            return None, int(self.ws.WSAGetLastError())
        query = _QuerySet(size=c.sizeof(_QuerySet), service=c.pointer(service), namespace=16, context=context.value)
        lookup = c.c_void_p()
        # Match the reviewed native diagnostic; the provider ignores return
        # flags at Begin and FLUSHCACHE at Next, as documented by Microsoft.
        if self.ws.WSALookupServiceBeginW(c.byref(query), _SDP_FLAGS, c.byref(lookup)):
            return None, int(self.ws.WSAGetLastError())
        if not lookup.value:return None, 10022
        try:
            capacity = 8192
            for _ in range(3):
                buffer = c.create_string_buffer(capacity)
                _QuerySet.from_buffer(buffer).size = c.sizeof(_QuerySet)
                returned = _DWORD(capacity)
                if not self.ws.WSALookupServiceNextW(lookup, _SDP_FLAGS, c.byref(returned), buffer):
                    if not c.sizeof(_QuerySet) <= returned.value <= capacity:return None, 10022
                    return self._channel(buffer, int(returned.value), address)
                error = int(self.ws.WSAGetLastError())
                if error != 10014 or not capacity < returned.value <= 65536:return None, error
                capacity = int(returned.value)
            return None, 10014
        finally:self.ws.WSALookupServiceEnd(lookup)


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
            result = _result(port, 'not_bluetooth', 0, started, time.monotonic)
        else:
            backend = _WindowsBackend()
            result = _probe(port, budget, backend)
    except Exception as exc:
        code = getattr(exc, 'winerror', None) or getattr(exc, 'errno', None)
        if type(code) is not int or not 1 <= code <= 0xffffffff:code = 87 if isinstance(exc, ValueError) else 1
        result = _result(port, 'error', 0, started, time.monotonic, code)
    finally:
        if backend is not None:
            try:backend.close()
            except Exception:result = _result(port, 'error', 0, started, time.monotonic, 1)
    payload = json.dumps(result, ensure_ascii=True, separators=(',', ':')).encode('ascii')
    if result_path is not None:
        try:_write_result(result_path, payload)
        except (OSError, ValueError):
            result = _result(port, 'error', 0, started, time.monotonic, 1)
            payload = json.dumps(result, ensure_ascii=True, separators=(',', ':')).encode('ascii')
    if sys.stdout is not None:
        print(payload.decode('ascii'), flush=True)
    return 0 if result['status'] == 'ready' else 1
