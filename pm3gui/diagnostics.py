"""Small allowlisted support packages. Never collect logs or library contents."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import zipfile

from .version import APP_VERSION


MAX_CLIENT_BYTES = 512 * 1024 * 1024
MAX_REPORT_BYTES = 32 * 1024
MAX_ERROR_CODES = 16

TRANSPORTS = frozenset({'usb', 'blueshark', 'tcp', 'offline', 'unknown'})
CONNECTIONS = frozenset({'connected', 'disconnected', 'uncertain', 'unknown'})
OPERATIONS = frozenset({'idle', 'running', 'review', 'cleanup', 'recovering', 'uncertain', 'unknown'})
JOB_KINDS = frozenset({'none', 'read', 'studio', 'hf_write', 'lf_write', 'emulation',
                       'resume', 'auto_capture', 'firmware', 'antenna', 'other'})
STAGES = frozenset({'idle', 'preparing', 'detecting', 'reading', 'writing',
                    'verifying', 'review', 'emulating', 'measuring', 'waiting',
                    'cleanup', 'recovering', 'uncertain', 'other'})
_STAGE_GROUPS = {
    'preparing': {'prepare', 'post_select', 'select', 'resume_select', 'emu_load', 'emu_prepare_break',
                  'emu_prepare_off', 'emu_prepare_confirm'},
    'detecting': {'detect', 'search', 'pre_select', 'info', 'identify_lf'},
    'reading': {'read', 'read_block', 'read_sector', 'read_blocks', 'read_pages', 'dump', 'keys',
                'check_keys', 'fchk', 'mf_check', 'partial_read', 'lf_identifier'},
    'writing': {'write', 'write_block', 'write_blocks', 'restore', 'write_transaction'},
    'verifying': {'verify', 'readback', 'emu_verify', 'confirm'},
    'review': {'review'}, 'emulating': {'emulating', 'emu_start', 'holding'},
    'cleanup': {'cleanup_break', 'cleanup_off', 'cleanup_version', 'cleanup_confirm',
                'auto_cleanup_off', 'auto_cleanup_version', 'cancel_wait', 'cancel_resync', 'off'},
    'recovering': {'recovery_resync_select', 'recovery_identity', 'recovery_resync', 'recover'},
    'measuring': {'measure'}, 'waiting': {'wait'},
    'uncertain': {'uncertain'},
}
_READ_KINDS = {'mfc', 'mfu', 'hf_read', 'lf_read', 'read'}
_JOB_MAP = {'hf_write': 'hf_write', 'write': 'hf_write', 'lf_write': 'lf_write',
            'studio': 'studio', 'emulation': 'emulation', 'resume': 'resume', 'auto_capture': 'auto_capture'}


def _choice(value, allowed, fallback):
    return value if type(value) is str and value in allowed else fallback


def _bounded_int(value, low=0, high=1_000_000):
    return value if type(value) is int and low <= value <= high else None


def _boolean(value):
    return value if type(value) is bool else None


def _codes(values):
    if not isinstance(values, (tuple, list)):
        return ()
    return tuple(value for value in values[-MAX_ERROR_CODES:]
                 if type(value) is int and -(2**31) <= value < 2**31)


@dataclass(frozen=True)
class DiagnosticSnapshot:
    """Input contract: arbitrary strings are reduced to fixed enums on render."""
    transport: str = 'unknown'
    connection: str = 'unknown'
    operation: str = 'unknown'
    job_kind: str = 'other'
    stage: str = 'other'
    rf_lease: bool | None = None
    firmware_busy: bool | None = None
    reconnect_attempts: int | None = None
    error_codes: tuple[int, ...] = ()


def snapshot_from_owner(owner):
    """Inspect in-memory state only; do not enumerate ports or query the device."""
    runner = getattr(owner, 'runner', None)
    job = getattr(owner, 'read_job', None)
    job = job if isinstance(job, dict) else {}
    antenna = getattr(owner, 'antenna_job', None)
    raw_kind = job.get('job_kind')
    if not job and isinstance(antenna, dict) and antenna:
        job = antenna
        raw_kind = 'antenna'
    raw_stage = job.get('stage')
    stage = next((name for name, values in _STAGE_GROUPS.items()
                  if type(raw_stage) is str and raw_stage in values), 'other')
    session = getattr(runner, 'session_open', None)
    connection = ('connected' if session is True else 'disconnected' if session is False else 'unknown')
    busy = getattr(runner, 'busy', None)
    operation = 'running' if busy is True else 'idle' if busy is False and not job else 'unknown'
    if job:
        operation = stage if stage in {'review', 'cleanup', 'recovering', 'uncertain'} else 'running'
    if stage == 'uncertain':
        connection = 'uncertain'
    if not job and busy is False:
        stage = 'idle'
    kind = raw_kind
    kind = ('read' if type(kind) is str and kind in _READ_KINDS else
            _JOB_MAP.get(kind, 'other') if type(kind) is str else 'other')
    firmware = _boolean(getattr(owner, 'firmware_busy', None))
    if firmware is True:
        kind, operation, stage = 'firmware', 'running', 'other'
    elif antenna is not None and raw_kind == 'antenna':
        kind = 'antenna'
    elif not job and busy is False:
        kind = 'none'
    reconnector = getattr(owner, 'reconnector', None)
    codes = getattr(owner, 'diagnostic_error_codes', ())
    # deque is deliberately copied, never iterating arbitrary application data.
    from collections import deque
    if isinstance(codes, deque):
        from itertools import islice
        codes = tuple(reversed(tuple(islice(reversed(codes), MAX_ERROR_CODES))))
    return DiagnosticSnapshot(
        transport=_choice(getattr(owner, '_active_transport', None), TRANSPORTS, 'unknown'),
        connection=connection, operation=operation, job_kind=kind, stage=stage,
        rf_lease=_boolean(getattr(runner, 'rf_lease', None)), firmware_busy=firmware,
        reconnect_attempts=_bounded_int(getattr(reconnector, 'attempts', None), 0, 3),
        error_codes=_codes(codes),
    )


def _numeric_version(values):
    result = []
    for value in values:
        value = _bounded_int(value, 0, 1_000_000)
        if value is None:
            return None
        result.append(value)
    return result


def _runtime():
    # platform.platform(), sys.version and Qt build strings can contain paths,
    # host names or compiler details. Only numeric versions are needed here.
    from PySide6.QtCore import qVersion
    qt = qVersion().split('.')
    qt_version = (_numeric_version([int(part) for part in qt])
                  if len(qt) == 3 and all(part.isascii() and part.isdecimal() for part in qt) else None)
    os_family = {'win32': 'windows', 'darwin': 'macos', 'linux': 'linux'}.get(sys.platform, 'other')
    os_version = None
    if sys.platform == 'win32' and hasattr(sys, 'getwindowsversion'):
        value = sys.getwindowsversion()
        os_version = _numeric_version([value.major, value.minor, value.build])
    return {'python': _numeric_version(sys.version_info[:3]), 'qt': qt_version,
            'os': {'family': os_family, 'version': os_version},
            'application_package': 'frozen' if getattr(sys, 'frozen', False) is True else 'source'}


class DiagnosticsCancelled(Exception):
    pass


def _client_fingerprint(path, cancelled):
    if path is None or path == '':
        return {'status': 'not_selected'}
    try:
        path = Path(path)
        if not path.is_file():
            return {'status': 'missing'}
        before = path.stat()
        if before.st_size > MAX_CLIENT_BYTES:
            return {'status': 'too_large'}
        digest, size = hashlib.sha256(), 0
        with path.open('rb') as stream:
            while True:
                if cancelled():
                    raise DiagnosticsCancelled()
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_CLIENT_BYTES:
                    return {'status': 'too_large'}
                digest.update(chunk)
        after = path.stat()
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or size != before.st_size):
            return {'status': 'changed_during_read'}
        return {'status': 'hashed', 'sha256': digest.hexdigest(), 'bytes': size}
    except (OSError, ValueError, TypeError):
        # Do not include an exception containing a private path.
        return {'status': 'unreadable'}


@dataclass(frozen=True)
class PreparedDiagnostics:
    """The exact, small UTF-8 document displayed before export."""
    document: bytes
    _factory_digest: str = field(default='', init=False, repr=False)


def prepare_diagnostics(snapshot, client_path=None, *, cancelled=lambda: False):
    if not isinstance(snapshot, DiagnosticSnapshot):
        raise TypeError('A DiagnosticSnapshot is required.')
    if cancelled():
        raise DiagnosticsCancelled()
    body = {
        'schema': 'proxmark-studio-diagnostics-v1',
        'created_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'application_version': APP_VERSION,
        'runtime': _runtime(),
        'client': _client_fingerprint(client_path, cancelled),
        'state': {
            'transport': _choice(snapshot.transport, TRANSPORTS, 'unknown'),
            'connection': _choice(snapshot.connection, CONNECTIONS, 'unknown'),
            'operation': _choice(snapshot.operation, OPERATIONS, 'unknown'),
            'job_kind': _choice(snapshot.job_kind, JOB_KINDS, 'other'),
            'stage': _choice(snapshot.stage, STAGES, 'other'),
            'rf_lease': _boolean(snapshot.rf_lease),
            'firmware_busy': _boolean(snapshot.firmware_busy),
        },
        'counters': {'reconnect_attempts': _bounded_int(snapshot.reconnect_attempts, 0, 3)},
        'recent_completion_codes': list(_codes(snapshot.error_codes)),
        'scope': {'raw_logs': False, 'commands': False, 'library': False, 'settings': False,
                  'card_identifiers': False, 'file_paths': False, 'device_identifiers': False,
                  'device_query_performed': False},
    }
    document = (json.dumps(body, ensure_ascii=True, sort_keys=True, indent=2) + '\n').encode('utf-8')
    if len(document) > MAX_REPORT_BYTES:
        raise ValueError('Diagnostic report is too large.')
    prepared = PreparedDiagnostics(document)
    object.__setattr__(prepared, '_factory_digest', hashlib.sha256(document).hexdigest())
    return prepared


_README = b'''Proxmark Studio diagnostic package

diagnostics.json contains application/runtime versions, the selected client
binary SHA256 (if readable), normalized operation/connection state, a bounded
reconnection counter and up to 16 numeric completion codes from this session.
null and unknown mean that information was not available.

No raw logs, command arguments, card IDs, dumps, keys, user/profile names,
personal paths, addresses, settings or library contents are included.
Creating this package does not query a device or change a library.

This package is a snapshot, not proof of device compatibility or a complete
operation history. No data is uploaded automatically. Share it yourself only
if you choose to. SHA256 entries in manifest.json check file integrity.
'''


def _validated_document(prepared):
    """Reject accidental caller replacement of the reviewed payload."""
    if not isinstance(prepared, PreparedDiagnostics) or type(prepared.document) is not bytes:
        raise TypeError('Prepared diagnostics are required.')
    if len(prepared.document) > MAX_REPORT_BYTES:
        raise ValueError('Diagnostic report is too large.')
    body = json.loads(prepared.document)
    expected = {'schema', 'created_utc', 'application_version', 'runtime', 'client', 'state',
                'counters', 'recent_completion_codes', 'scope'}
    if not isinstance(body, dict) or set(body) != expected:
        raise ValueError('Unrecognized diagnostic document.')
    state = body['state']
    if (not isinstance(state, dict) or set(state) != {'transport', 'connection', 'operation', 'job_kind',
            'stage', 'rf_lease', 'firmware_busy'} or
            state['transport'] not in TRANSPORTS or state['connection'] not in CONNECTIONS or
            state['operation'] not in OPERATIONS or state['job_kind'] not in JOB_KINDS or
            state['stage'] not in STAGES or any(value is not None and type(value) is not bool
                                              for value in (state['rf_lease'], state['firmware_busy']))):
        raise ValueError('Unrecognized diagnostic state.')
    # This detects accidental payload replacement between review and export;
    # it is not a signature or an authentication boundary.
    expected_digest = getattr(prepared, '_factory_digest', None)
    if expected_digest != hashlib.sha256(prepared.document).hexdigest():
        raise ValueError('Diagnostic document was not prepared by the collector.')
    return prepared.document


def validate_destination(destination, *, active_profile=None):
    """A support export must not replace or add files inside the active profile."""
    destination = Path(destination)
    if destination.suffix.casefold() != '.zip':
        raise ValueError('Choose a ZIP filename.')
    if active_profile is not None and destination.resolve().is_relative_to(Path(active_profile).resolve()):
        raise ValueError('Choose a destination outside the active profile.')
    return destination


def export_diagnostics(prepared, destination, *, active_profile=None):
    """Write exactly the reviewed payload; replace a target only after success."""
    destination = validate_destination(destination, active_profile=active_profile)
    document = _validated_document(prepared)
    blobs = {'diagnostics.json': document, 'README.txt': _README}
    manifest = {
        'schema': 'proxmark-studio-diagnostics-manifest-v1',
        'files': {name: {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
                  for name, data in sorted(blobs.items())},
    }
    blobs['manifest.json'] = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode('utf-8')
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as output:
        for name, data in sorted(blobs.items()):
            output.writestr(name, data)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.pm3-diagnostics-', suffix='.tmp',
                                          dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(archive.getvalue())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return {'files': len(blobs), 'bytes': len(archive.getvalue()), 'sha256': hashlib.sha256(archive.getvalue()).hexdigest()}
