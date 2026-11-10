"""Reviewed fullimage-only firmware updates for PM3 RDV4 over USB.

Preparing a profile reads local files only. Hardware access starts exclusively
when execute_flash/FirmwareWorker.run is explicitly invoked by the GUI.
"""
from __future__ import annotations
from .i18n import tr

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import time
import threading
from uuid import uuid4

from PySide6.QtCore import QThread, Signal
import serial
from serial.tools import list_ports

from .device import _client_environment, parse_connection_status
from .paths import default_data_dir, firmware_catalog_dir
from .process_env import external_process_libraries
from .firmware_output import FlashTranscript, FlashDiagnostics, flash_diagnostics

FLASH_BASE = 0x100000
OS_START = 0x102000
FLASH_END = 0x180000
INTERNAL_SIZE = 512 * 1024
SPI_SIZE = 256 * 1024
BOOTROM_SIZE = OS_START - FLASH_BASE
MAX_LOG_BYTES = 4 * 1024 * 1024
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_READ_FAILURE = re.compile(r'\b(?:error|failed|failure|timeout|timed out|nack)\b', re.I)
_PROFILES = {'usb': ('USB · без Blue Shark', 'gui-cancel', False),
             'blueshark': ('Blue Shark · Bluetooth SPP', 'gui-blueshark', True)}


class FirmwareError(RuntimeError):
    pass


class FirmwareCancelled(FirmwareError):
    pass


def _require(condition, message):
    if not condition:
        raise FirmwareError(message)


def _digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(data)
    return result.hexdigest()


def _json(path):
    path = Path(path)
    _require(path.is_file(), tr('Файл не найден: {0}', f'{path}'))
    _require(path.stat().st_size <= 2 * 1024 * 1024, tr('Слишком большой файл описания комплекта.'))
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise FirmwareError(tr('Не удалось прочитать manifest: {0}', f'{path}')) from exc
    _require(isinstance(value, dict), tr('Manifest должен содержать JSON-объект.'))
    return value


def _port(value):
    _require(isinstance(value, str) and re.fullmatch(r'COM[1-9][0-9]*', value, re.I), tr('Выберите USB COM-порт Proxmark3.'))
    return value.upper()


def _path(value, parent):
    _require(isinstance(value, str) and bool(value), tr('В manifest не указан путь к файлу.'))
    path = Path(value).expanduser()
    return (path if path.is_absolute() else parent / path).resolve()


def _hash_file(path, expected):
    _require(isinstance(expected, str) and _SHA.fullmatch(expected.lower()), tr('В manifest отсутствует корректный SHA256.'))
    _require(Path(path).is_file() and _digest(path) == expected.lower(), tr('Файл отсутствует или изменился: {0}', f'{path}'))


@dataclass(frozen=True)
class ElfSegment:
    address: int
    file_offset: int
    size: int


def inspect_fullimage(path: Path) -> tuple[ElfSegment, ...]:
    """Validate ARM ELF32 LOAD ranges before passing one image to the flasher."""
    path = Path(path)
    _require(52 <= path.stat().st_size <= 8 * 1024 * 1024, tr('Неверный размер ELF.'))
    data = path.read_bytes()
    _require(data[:7] == b'\x7fELF\x01\x01\x01', tr('Нужен little-endian ELF32.'))
    kind, machine = struct.unpack_from('<HH', data, 16)
    entry = struct.unpack_from('<I', data, 24)[0]
    offset = struct.unpack_from('<I', data, 28)[0]
    header_size, width, count = struct.unpack_from('<HHH', data, 40)
    _require(kind == 2 and machine == 40 and header_size == 52, tr('Нужен исполняемый ARM ELF.'))
    _require(width == 32 and 1 <= count <= 64 and offset >= 52 and offset + width*count <= len(data), tr('Повреждена таблица ELF program headers.'))
    segments, executable = [], []
    for index in range(count):
        typ, start, virtual, physical, size, memory, flags, alignment = struct.unpack_from('<8I', data, offset + index*width)
        if typ != 1 or size == 0:
            continue
        _require(size <= memory and start + size <= len(data), tr('Повреждён ELF LOAD segment.'))
        _require(OS_START <= physical < FLASH_END and physical + size <= FLASH_END,
                 tr('ELF затрагивает загрузчик или выходит за пределы 512 КиБ flash.'))
        segments.append(ElfSegment(physical, start, size))
        if flags & 1:
            executable.append((virtual, virtual + size))
    segments.sort(key=lambda segment: segment.address)
    _require(bool(segments) and segments[0].address == OS_START, tr('Ожидался fullimage с началом 0x102000.'))
    _require(all(a.address + a.size <= b.address for a, b in zip(segments, segments[1:])), tr('ELF LOAD segments перекрываются.'))
    _require(entry & 1 and any(start <= (entry & ~1) < end for start, end in executable), tr('Неверная ARM Thumb entry point.'))
    return tuple(segments)


@dataclass(frozen=True)
class FirmwareProfile:
    key: str
    label: str
    available: bool
    reason: str
    manifest_path: Path


@dataclass(frozen=True)
class PreparedFlash:
    profile: FirmwareProfile
    version: str
    port: str
    image_path: Path
    image_sha256: str
    client_path: Path
    client_sha256: str
    runtime_dir: Path
    arm_checksum: str
    backup_directory: Path
    segments: tuple[ElfSegment, ...]
    manifest_bindings: tuple[tuple[Path, str], ...]
    runtime_bindings: tuple[tuple[Path, str], ...]

    def flash_argv(self) -> list[str]:
        return [str(self.client_path), '--incognito', '-p', self.port, '-f', '--flash', '--image',
                str(self.backup_directory / 'selected-fullimage.elf')]

    def review(self) -> dict:
        return {'profile': self.profile.key, 'label': self.profile.label, 'version': self.version,
                'port': self.port, 'client_path': str(self.client_path), 'image_path': str(self.image_path),
                'image_sha256': self.image_sha256, 'client_sha256': self.client_sha256,
                'arm_checksum': self.arm_checksum, 'backup_directory': str(self.backup_directory),
                'internal_backup_bytes': INTERNAL_SIZE, 'spi_backup_bytes': SPI_SIZE,
                'bootrom_preserved': True, 'bootrom_write_disabled': True, 'flash_scope': 'main-fullimage-only',
                'flash_argv': self.flash_argv()}


def _compatible_extension(path, image_sha, metadata_sha):
    if not path.is_file():
        return None
    value = _json(path)
    if value.get('status') != 'ready_offline_verified' or value.get('arm_and_metadata_unchanged') is not True:
        return None
    before, after = value.get('before', {}), value.get('after', {})
    image = 'armsrc/obj/fullimage.elf'
    if before.get(image, {}).get('sha256') != image_sha or after.get(image, {}).get('sha256') != image_sha:
        return None
    for name in ('armsrc/mifarecmd.c', 'common/default_version_pm3.c', 'armsrc/version_pm3.c'):
        first, second = before.get(name, {}), after.get(name, {})
        _require(first.get('exists') is True and second.get('exists') is True and first.get('sha256') == second.get('sha256')
                 and _SHA.fullmatch(str(first.get('sha256', ''))), tr('Повреждено подтверждение совместимости клиента.'))
    if metadata_sha:
        _require(before['common/default_version_pm3.c']['sha256'] == metadata_sha, tr('Клиент относится к другой конфигурации ARM.'))
    return value


class FirmwareCatalog:
    def __init__(self, project_root: Path):
        self.project_root = Path(project_root).resolve()
        self.setup_dir = firmware_catalog_dir(self.project_root)

    def profiles(self) -> list[FirmwareProfile]:
        result = []
        for key, (label, stem, _) in _PROFILES.items():
            label=tr(label)
            manifest = self.setup_dir / f'{stem}-build-manifest.json'
            try:
                self._bundle(key, manifest)
                result.append(FirmwareProfile(key, label, True, '', manifest))
            except (FirmwareError, OSError, KeyError, TypeError, ValueError) as exc:
                result.append(FirmwareProfile(key, label, False, str(exc), manifest))
        return result

    def _bundle(self, key, manifest):
        _require(key in _PROFILES, tr('Неизвестный профиль прошивки.'))
        manifest = Path(manifest).resolve()
        value = _json(manifest)
        _require(value.get('platform') == 'PM3RDV4' and value.get('flash_capacity') == INTERNAL_SIZE,
                 tr('Комплект должен быть для PM3RDV4 с flash 512 КиБ.'))
        extras = value.get('platform_extras', [])
        extras = extras.split() if isinstance(extras, str) else extras
        _require(isinstance(extras, list) and ('BTADDON' in extras) == _PROFILES[key][2], tr('BTADDON не соответствует выбранному профилю.'))
        files = value.get('artifact_files', {})
        ready = value.get('ready_artifacts', {})
        image_entry = ready.get('firmware_elf') or files.get('fullimage.elf')
        _require(isinstance(image_entry, dict), tr('В manifest нет fullimage.elf.'))
        image = _path(image_entry.get('path') or image_entry.get('source'), manifest.parent)
        image_sha = str(image_entry.get('sha256', '')).lower()
        _hash_file(image, image_sha)
        if 'bytes' in image_entry:
            _require(image.stat().st_size == image_entry['bytes'], tr('Размер ELF не совпадает с manifest.'))
        segments = inspect_fullimage(image)
        version, arm = value.get('custom_version'), value.get('arm_sources_checksum')
        _require(isinstance(version, str) and 0 < len(version) < 200 and re.fullmatch('[0-9a-f]{9}', str(arm)), tr('Не указаны версия/checksum ARM.'))
        client_entry = files.get('proxmark3.exe', {})
        client_value = ready.get('matching_client_executable')
        if not client_value and value.get('matching_client_runtime_root'):
            client_value = str(_path(value['matching_client_runtime_root'], manifest.parent) / 'proxmark3.exe')
        client_value = client_value or client_entry.get('source') or client_entry.get('path')
        client_sha = ready.get('matching_client_sha256') or client_entry.get('sha256')
        bindings = [(manifest, _digest(manifest))]
        # A client-only extension is compatible only with this exact ELF AND
        # unchanged shared metadata, not merely its configuration-blind ARM hash.
        metadata_sha = files.get('default_version_pm3.c', {}).get('sha256')
        directories = list(dict.fromkeys((manifest.parent, self.setup_dir)))
        override = None
        for directory in directories:
            for name in ('gui-scoped-cancel-client-build-result.json', 'gui-pipe-client-build-result.json'):
                path = directory / name
                extension = _compatible_extension(path, image_sha, metadata_sha)
                if extension:
                    override = (path, extension)
                    break
            if override:
                break
        if override:
            path, extension = override
            client_value = str(_path(extension.get('client'), path.parent))
            client_sha = extension.get('new_exe_sha256')
            bindings.append((path, _digest(path)))
        client = _path(client_value, manifest.parent)
        client_sha = str(client_sha or '').lower()
        _hash_file(client, client_sha)
        runtime = client.parent / 'libs'
        _require(runtime.is_dir(), tr('Нужен полный клиент с папкой libs рядом с proxmark3.exe.'))
        runtime_bindings = []
        runtime_manifest = runtime / '.pm3gui-runtime-manifest.json'
        _require(runtime_manifest.is_file(), tr('Для прошивки нужен проверенный runtime manifest в client/libs. Укажите полный комплект клиента.'))
        deployed = _json(runtime_manifest)
        runtime_files = deployed.get('files')
        _require(deployed.get('kind') == 'proxmark3-gui-local-ucrt64-runtime-v1'
                 and isinstance(runtime_files, list) and 1 <= len(runtime_files) <= 1024,
                 tr('Неверный или пустой runtime manifest клиента.'))
        bindings.append((runtime_manifest, _digest(runtime_manifest)))
        for item in runtime_files:
            _require(isinstance(item, dict), tr('Неверный элемент runtime manifest.'))
            name = item.get('relative_target')
            _require(isinstance(name, str) and not Path(name).is_absolute(), tr('Неверный путь DLL в runtime manifest.'))
            target = (runtime / name).resolve()
            _require(target.is_relative_to(runtime.resolve()), tr('Путь DLL выходит за runtime.'))
            expected = str(item.get('sha256', '')).lower()
            _hash_file(target, expected)
            runtime_bindings.append((target, expected))
        return dict(version=version, image_path=image, image_sha256=image_sha, client_path=client,
                    client_sha256=client_sha, runtime_dir=runtime, arm_checksum=arm, segments=segments,
                    manifest_bindings=tuple(bindings), runtime_bindings=tuple(runtime_bindings))

    def prepare(self, profile_key: str, port='COM3', backup_root: Path | None = None,
                manifest_path: Path | None = None) -> PreparedFlash:
        _require(profile_key in _PROFILES, tr('Неизвестный профиль прошивки.'))
        port = _port(port)
        label, stem, _ = _PROFILES[profile_key]
        label=tr(label)
        manifest = Path(manifest_path).resolve() if manifest_path else self.setup_dir / f'{stem}-build-manifest.json'
        try:
            bundle = self._bundle(profile_key, manifest)
            if backup_root is None:
                backup_root = (default_data_dir() / 'firmware-backups' if getattr(sys, 'frozen', False)
                               else self.project_root / 'data/firmware-backups')
            backup_root = Path(backup_root).resolve()
            directory = backup_root / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + profile_key + '-' + uuid4().hex[:8])
            _require(not directory.exists(), tr('Папка резервной копии уже существует.'))
            return PreparedFlash(FirmwareProfile(profile_key, label, True, '', manifest), port=port,
                                 backup_directory=directory, **bundle)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise FirmwareError(tr('Не удалось подготовить комплект: {0}', f'{exc}')) from exc


def _crc_a_wire(data):
    crc = 0x6363
    for byte in data:
        value = byte ^ (crc & 255)
        value ^= (value << 4) & 255
        crc = ((crc >> 8) ^ (value << 8) ^ (value << 3) ^ (value >> 4)) & 65535
    return ((crc & 255) << 8) | (crc >> 8)


def read_internal_flash(port: str, *, serial_factory=None, cancelled=None, timeout=35.0, clock=None) -> bytes:
    """Bounded read-only OLD download; never enter bootloader or use RAW mode."""
    port = _port(port)
    _require(0 < timeout <= 60, tr('Неверный тайм-аут резервного чтения.'))
    factory = serial_factory or serial.Serial
    cancelled = cancelled or (lambda: False)
    clock = clock or time.monotonic
    output, coverage = bytearray(INTERNAL_SIZE), bytearray(INTERNAL_SIZE)
    buffer = bytearray()
    deadline = clock() + timeout
    with factory(port, baudrate=115200, timeout=0.15, write_timeout=3) as stream:
        stream.reset_input_buffer()
        request = struct.pack('<4Q', 0x010A, 0, INTERNAL_SIZE, 0) + bytes(512)
        _require(stream.write(request) == len(request), tr('Неполная отправка запроса резервного чтения.'))
        while clock() < deadline:
            if cancelled():
                raise FirmwareCancelled(tr('Прошивка отменена до записи.'))
            buffer.extend(stream.read(max(1, min(int(stream.in_waiting), 8192))))
            _require(len(buffer) <= 32768, tr('Переполнение буфера резервного чтения.'))
            while len(buffer) >= 4:
                if buffer[:4] == b'PM3b':
                    if len(buffer) < 10:
                        break
                    length_flags, status, reason, command = struct.unpack_from('<HbbH', buffer, 4)
                    length = length_flags & 0x7fff
                    _require(length <= 8192, tr('Неверная длина NG-пакета.'))
                    if len(buffer) < 12 + length:
                        break
                    frame = bytes(buffer[:12+length])
                    del buffer[:12+length]
                    crc = struct.unpack_from('<H', frame, 10+length)[0]
                    _require(crc == 0x3362 or crc == _crc_a_wire(frame[:10+length]), tr('Повреждён CRC NG-пакета.'))
                    _require(status == 0, tr('Устройство сообщило ошибку резервного чтения.'))
                    if command in (0x0100, 0x0101):
                        _require(not _READ_FAILURE.search(frame[10:10+length].decode('utf-8', errors='replace')),
                                 tr('Устройство сообщило ошибку резервного чтения.'))
                        continue
                    if command == 0x00ff:
                        _require(not length_flags & 0x8000 and length >= 24 and struct.unpack_from('<Q', frame, 10)[0] == 1
                                 and all(coverage), tr('Резервная копия не полна или ACK неверен.'))
                        return bytes(output)
                    raise FirmwareError(tr('Неожиданный NG-ответ при резервном чтении.'))
                if len(buffer) < 544:
                    break
                command, offset, count, flags = struct.unpack_from('<4Q', buffer)
                payload = bytes(buffer[32:544])
                del buffer[:544]
                if command == 0x010b:
                    _require(0 < count <= 512 and offset + count <= INTERNAL_SIZE, tr('Пакет чтения выходит за память устройства.'))
                    for index in range(count):
                        position = offset + index
                        _require(not coverage[position] or output[position] == payload[index], tr('Повторные пакеты содержат разные данные.'))
                    output[offset:offset+count] = payload[:count]
                    coverage[offset:offset+count] = b'\1' * count
                elif command == 0x00ff:
                    _require(offset == 1 and all(coverage), tr('Резервная копия не полна или ACK неверен.'))
                    return bytes(output)
                elif command in (0x0100, 0x0101):
                    _require(not _READ_FAILURE.search(payload.decode('utf-8', errors='replace')),
                             tr('Устройство сообщило ошибку резервного чтения.'))
                    continue
                else:
                    raise FirmwareError(tr('Устройство отклонило чтение или прислало неожиданный OLD-ответ.'))
    raise FirmwareError(tr('Истёк срок резервного чтения: {0}/{1} байт.', f'{sum(coverage)}', f'{INTERNAL_SIZE}'))


@dataclass(frozen=True)
class ClientResult:
    returncode: int
    output: str
    log_truncated: bool = False
    output_bytes: int = 0
    log_error: str = ''
    flash_diagnostics: FlashDiagnostics | None = None


class _SystemFlashIO:
    def ports(self):
        return list(list_ports.comports())

    def ensure_exclusive(self):
        _require(os.name == 'nt', tr('Автоматическая прошивка этого комплекта поддерживается в Windows.'))
        tasklist = Path(os.environ.get('SystemRoot', r'C:\Windows'))/'System32/tasklist.exe'
        _require(tasklist.is_file(), tr('Не найден системный tasklist.exe для проверки занятости клиента.'))
        result = subprocess.run([str(tasklist), '/FI', 'IMAGENAME eq proxmark3.exe', '/FO', 'CSV', '/NH'],
                                capture_output=True, text=True, errors='replace', timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        _require(result.returncode == 0, tr('Не удалось проверить занятость клиента Proxmark3.'))
        _require(not any(row and row[0].casefold() == 'proxmark3.exe' for row in csv.reader(result.stdout.splitlines())),
                 tr('Другой proxmark3.exe ещё работает. Отключите его и подготовьте операцию заново.'))

    def read_internal(self, port, cancelled):
        return read_internal_flash(port, cancelled=cancelled)

    def run_client(self, prepared, arguments, cwd, logfile, timeout):
        _hash_file(prepared.client_path, prepared.client_sha256)
        native_environment = _client_environment(prepared.client_path)
        environment = {key: native_environment.value(key) for key in native_environment.keys()}
        windows = Path(os.environ.get('SystemRoot', r'C:\Windows'))
        native_paths = [prepared.client_path.parent, prepared.runtime_dir]
        python_bin = prepared.client_path.parent / 'python/bin'
        if python_bin.is_dir():
            native_paths.append(python_bin)
        environment['PATH'] = os.pathsep.join(map(str, (*native_paths, windows/'System32', windows)))
        environment.pop('QT_QPA_PLATFORM', None)
        transcript = FlashTranscript(MAX_LOG_BYTES,allow_transition='--flash' in arguments)
        log_errors = [];capture_errors=[]
        with Path(logfile).open('xb', buffering=0) as output:
            with external_process_libraries():
                process = subprocess.Popen([str(prepared.client_path), *arguments], cwd=cwd, env=environment,
                                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           bufsize=0, creationflags=subprocess.CREATE_NO_WINDOW)
            def drain():
                stored = 0
                while True:
                    try:data = process.stdout.read(65536)
                    except OSError as exc:
                        capture_errors.append(str(exc));return
                    if not data:return
                    transcript.feed(data)
                    if stored<MAX_LOG_BYTES and not log_errors:
                        try:
                            part=data[:MAX_LOG_BYTES-stored];output.write(part);stored+=len(part)
                        except OSError as exc:log_errors.append(str(exc))
            reader = threading.Thread(target=drain,name='firmware-log-drain',daemon=True)
            reader.start()
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    _require(time.monotonic() < deadline, tr('Клиент прошивки превысил время ожидания.'))
                    time.sleep(0.05)
            except BaseException:
                process.kill()
                process.wait(timeout=5)
                raise
            finally:
                reader.join(5)
                if reader.is_alive():
                    process.stdout.close();reader.join(1)
                else:process.stdout.close()
            _require(not reader.is_alive(), tr('Не удалось завершить чтение журнала клиента.'))
            _require(not capture_errors, tr('Не удалось полностью прочитать вывод клиента прошивки.'))
        diagnostics = transcript.finish()
        retained = transcript.retained()
        if diagnostics.fatal_lines:
            retained+=b'\n[GUI: retained diagnostic evidence]\n'+'\n'.join(diagnostics.fatal_lines).encode('utf-8','replace')
        try:Path(logfile).write_bytes(retained)
        except OSError as exc:log_errors.append(str(exc))
        return ClientResult(process.returncode, retained.decode('utf-8','replace'),transcript.truncated,
                            transcript.total_bytes,'; '.join(log_errors)[:1024],diagnostics)

    def wait_usb(self, port, serial_number):
        deadline = time.monotonic() + 30
        candidate=None;since=0
        while time.monotonic() < deadline:
            matches = [p for p in self.ports() if getattr(p, 'vid', None) == 0x9AC4 and getattr(p, 'pid', None) == 0x4B8F
                       and str(getattr(p, 'serial_number', '') or '') == serial_number]
            if len(matches) == 1:
                found=_port(matches[0].device)
                if candidate!=found:candidate=found;since=time.monotonic()
                elif time.monotonic()-since>=0.8:return found
            else:candidate=None
            time.sleep(0.2)
        raise FirmwareError(tr('Тот же Proxmark3 не появился по USB после записи. Сохранены резервные копии.'))


def _check_cancelled(cancelled):
    if cancelled():
        raise FirmwareCancelled(tr('Прошивка отменена до записи.'))


def _revalidate(prepared):
    _require(type(prepared) is PreparedFlash and prepared.profile.key in _PROFILES, tr('Ожидался подготовленный план прошивки.'))
    _port(prepared.port)
    for path, digest in prepared.manifest_bindings + prepared.runtime_bindings:
        _hash_file(path, digest)
    _hash_file(prepared.image_path, prepared.image_sha256)
    _hash_file(prepared.client_path, prepared.client_sha256)
    _require(inspect_fullimage(prepared.image_path) == prepared.segments, tr('ELF изменился после подготовки.'))


def execute_flash(prepared: PreparedFlash, *, progress=None, cancelled=None, io=None) -> dict:
    """Explicit write entry point. Test I/O can be injected without any hardware."""
    progress, cancelled = progress or (lambda text: None), cancelled or (lambda: False)
    io = io or _SystemFlashIO()
    _revalidate(prepared)
    _check_cancelled(cancelled)
    io.ensure_exclusive()
    matches = [p for p in io.ports() if p.device.upper() == prepared.port]
    _require(len(matches) == 1 and getattr(matches[0], 'vid', None) == 0x9AC4 and getattr(matches[0], 'pid', None) == 0x4B8F,
             tr('Выбранный COM-порт не принадлежит USB Proxmark3 RDV4.'))
    device_serial = str(getattr(matches[0], 'serial_number', '') or '')
    _require(bool(device_serial), tr('Не подтверждён серийный номер USB Proxmark3.'))
    directory = prepared.backup_directory
    directory.mkdir(parents=True, exist_ok=False)
    report_path = directory / 'report.json'
    state = dict(prepared.review(), status='preflight', verified=False, firmware_flashed=False,
                 bootrom_preserved=None, bootrom_unchanged=None, spi_unchanged=None,
                 device_serial=device_serial, flash_attempts=0, flash_may_have_written=False,
                 report_path=str(report_path))

    def save():
        state['updated_at'] = datetime.now(timezone.utc).isoformat()
        temporary = report_path.with_suffix('.new')
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(report_path)

    def command(command_text, name, timeout=45, port=None):
        result = io.run_client(prepared, ['--incognito', '-p', port or prepared.port, '-f', '-c', command_text],
                               directory, directory/(name+'.log'), timeout)
        _require(result.returncode == 0, tr('Команда {0} не завершилась успешно. См. журнал в папке резервной копии.', f'{name}'))
        _require('Communicating with PM3 over USB-CDC' in result.output, tr('Клиент не подтвердил USB-канал.'))
        return result.output

    def spi(name, port=None):
        target = directory/(name+'.bin')
        _require(not target.exists(), tr('Файл резервной копии уже существует.'))
        output = command(f'mem dump -f {name}', name, 60, port)
        _require(target.is_file() and target.stat().st_size == SPI_SIZE and re.search(r'\bSaved\s+262144\s+bytes\b', output),
                 tr('Клиент не подтвердил полное чтение SPI 256 КиБ.'))
        _require(not _READ_FAILURE.search(output), tr('Клиент/устройство сообщили ошибку чтения SPI; файл не принят как резервная копия.'))
        return target.read_bytes()

    save()
    try:
        progress(tr('Проверяем USB, модель и версию устройства…'))
        before_version = command('hw version', 'before-version')
        _require(parse_connection_status(before_version, 'usb')['connected']
                 and re.search(r'\bDevice\.{2,}\s+RDV4\b', before_version)
                 and 'AT91SAM7S512' in before_version, tr('Не подтверждён Proxmark3 RDV4 с flash 512 КиБ.'))
        _check_cancelled(cancelled)
        progress(tr('Резервная копия внутренней памяти: два полных чтения по 512 КиБ…'))
        first = io.read_internal(prepared.port, cancelled)
        _check_cancelled(cancelled)
        second = io.read_internal(prepared.port, cancelled)
        _require(len(first) == len(second) == INTERNAL_SIZE and first == second, tr('Два чтения внутренней памяти не совпали; запись отменена.'))
        (directory/'before-internal.bin').write_bytes(first)
        state['before_internal_sha256'] = hashlib.sha256(first).hexdigest()
        save()
        _check_cancelled(cancelled)
        progress(tr('Резервная копия SPI: два полных чтения по 256 КиБ…'))
        spi_first = spi('before-spi-1')
        _check_cancelled(cancelled)
        spi_second = spi('before-spi-2')
        _require(spi_first == spi_second, tr('Два чтения SPI не совпали; запись отменена.'))
        state['before_spi_sha256'] = hashlib.sha256(spi_first).hexdigest()
        state['backups_verified'] = True
        save()
        _check_cancelled(cancelled)
        _revalidate(prepared)
        io.ensure_exclusive()
        current = [p for p in io.ports() if p.device.upper() == prepared.port and getattr(p, 'vid', None) == 0x9AC4
                   and getattr(p, 'pid', None) == 0x4B8F and str(getattr(p, 'serial_number', '') or '') == device_serial]
        _require(len(current) == 1, tr('Перед записью не подтверждено то же USB-устройство.'))
        image = prepared.image_path.read_bytes()
        staged = directory/'selected-fullimage.elf'
        with staged.open('xb') as output:
            output.write(image)
        _hash_file(staged, prepared.image_sha256)
        _require(_digest(directory/'before-internal.bin') == state['before_internal_sha256']
                 and _digest(directory/'before-spi-1.bin') == state['before_spi_sha256'], tr('Резервные файлы изменились.'))
        _check_cancelled(cancelled)
        # From this point cancellation is deliberately ignored: finish the one
        # already-authorized write and verify it, never retry or unlock bootrom.
        state.update(status='flashing_main_fullimage', flash_attempts=1, flash_started=True,
                     flash_may_have_written=True, firmware_flashed=None)
        save()
        progress(tr('Записываем только основную прошивку. Дождитесь завершения проверки…'))
        result = io.run_client(prepared, prepared.flash_argv()[1:], directory, directory/'flash.log', 180)
        state['flash_exit_code'] = result.returncode
        diagnostics = result.flash_diagnostics or flash_diagnostics(result.output)
        state.update(flash_log_truncated=result.log_truncated, flash_output_bytes=result.output_bytes,
                     flash_log_error=result.log_error, bootloader_reconnect_messages=diagnostics.reconnect_lines,
                     flash_diagnostics=list(diagnostics.fatal_lines))
        save()
        _require(result.returncode == 0, tr('Клиент прошивки сообщил ошибку. Повторная запись не запускается.'))
        # Some pinned RRG failure paths exit0 and can even print All done.
        # Reject negative diagnostics; only actual readback proves installation.
        _require(not diagnostics.fatal_lines and diagnostics.all_done,
                 tr('Клиент не подтвердил завершение записи или сообщил ошибку. Повторная запись не запускается.'))
        state.update(status='verifying_readback', flash_process_completed=True)
        save()
        progress(tr('Проверяем записанный ELF, загрузчик и SPI…'))
        actual_port = io.wait_usb(prepared.port, device_serial)
        actual = io.read_internal(actual_port, lambda: False)
        _require(len(actual) == INTERNAL_SIZE, tr('Неполное чтение после прошивки.'))
        (directory/'after-internal.bin').write_bytes(actual)
        _require(actual[:BOOTROM_SIZE] == first[:BOOTROM_SIZE], tr('Загрузчик отличается от резервной копии.'))
        for segment in prepared.segments:
            start = segment.address-FLASH_BASE
            _require(actual[start:start+segment.size] == image[segment.file_offset:segment.file_offset+segment.size],
                     tr('Записанный ELF не совпал с чтением памяти по адресу {0}.', f'{segment.address:#x}'))
        _require(spi('after-spi', actual_port) == spi_first, tr('SPI-память отличается от резервной копии.'))
        after_version = command('hw version', 'after-version', 45, actual_port)
        after_status = parse_connection_status(after_version, 'usb')
        _require(after_status['connected'] and prepared.version in str(after_status['firmware'])
                 and prepared.arm_checksum in str(after_status['firmware']), tr('Устройство не подтвердило выбранную версию OS.'))
        capability = re.search(r'FPC USART for BT add-on\.*\s+(present|absent)\b', after_version, re.I)
        _require(capability and (capability.group(1).lower() == 'present') == _PROFILES[prepared.profile.key][2],
                 tr('Поддержка Blue Shark не соответствует выбранному профилю.'))
        state.update(status='installed_readback_verified', verified=True, firmware_flashed=True, port=actual_port,
                     client_path=str(prepared.client_path), bootrom_preserved=True, bootrom_unchanged=True, spi_unchanged=True,
                     compared_elf_bytes=sum(segment.size for segment in prepared.segments),
                     after_internal_sha256=hashlib.sha256(actual).hexdigest())
        save()
        progress(tr('Основная прошивка и резервные копии проверены. Загрузчик и SPI сохранены.'))
        return dict(state)
    except Exception as exc:
        state.update(status='failed_after_flash' if state.get('flash_started') else 'cancelled_before_flash' if isinstance(exc, FirmwareCancelled) else 'failed_before_flash',
                     error=str(exc), verified=False)
        save()
        raise FirmwareError(tr('{0}\nОтчёт и резервные копии: {1}', f'{exc}', f'{report_path}')) from exc


class FirmwareWorker(QThread):
    progress = Signal(str)
    completed = Signal(dict)
    failed = Signal(str)

    def __init__(self, prepared: PreparedFlash, parent=None, *, io=None):
        super().__init__(parent)
        self.prepared = prepared
        self.io = io

    def cancel(self):
        self.requestInterruption()

    def run(self):
        try:
            result = execute_flash(self.prepared, progress=self.progress.emit,
                                   cancelled=self.isInterruptionRequested, io=self.io)
            self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))
