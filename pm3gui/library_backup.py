"""Versioned whole-profile backups. No device I/O and no archive code execution."""
from __future__ import annotations

from contextlib import contextmanager, closing
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import struct
import tempfile
import zipfile
import zlib

from Cryptodome.Cipher import AES
from Cryptodome.Protocol.KDF import scrypt
from Cryptodome.Random import get_random_bytes
from .i18n import tr
from .version import APP_VERSION

MAGIC = b'PM3BKUP\x01'
PARAMETERS = bytes((17, 8, 1))  # scrypt N=2**17, r=8, p=1; fixed in format v1.
HEADER_SIZE = len(MAGIC) + len(PARAMETERS) + 16 + 12
TAG_SIZE = 16
CHUNK = 1024 * 1024
MAX_FILES = 50000
MAX_BYTES = 16 * 1024**3
MAX_ARCHIVE_BYTES = MAX_BYTES + 64 * 1024**2
MAX_MANIFEST_BYTES = 32 * 1024**2
FORMAT = 'proxmark-studio-library-backup'
_SIDECARS = {'library.sqlite3-wal', 'library.sqlite3-shm', 'library.sqlite3-journal'}
_RESERVED = re.compile(r'^(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)', re.I)
_SCHEMA = {
    'cards': {'id', 'name', 'uid', 'protocol', 'notes', 'created_at', 'updated_at'},
    'revisions': {'id', 'card_id', 'data', 'format', 'metadata', 'block_size', 'sha256', 'created_at', 'note'},
    'mf_keys': {'id', 'key_hex', 'label', 'source', 'created_at', 'verified'},
    'mf_key_origins': {'key_id', 'label', 'source', 'created_at'},
    'mf_card_keys': {'id', 'uid', 'sector', 'key_type', 'key_id', 'source', 'verified', 'created_at', 'verified_at'},
}


class BackupError(ValueError):
    pass


class BackupCancelled(BackupError):
    pass


def _require(condition, message):
    if not condition:
        raise BackupError(message)


def _check(cancelled):
    if cancelled():
        raise BackupCancelled(tr('Резервное копирование или восстановление отменено.'))


def _regular(path):
    info = path.lstat()
    _require(stat.S_ISREG(info.st_mode) and not path.is_symlink()
             and not getattr(info, 'st_file_attributes', 0) & 0x400,
             tr('Ссылки и специальные файлы не поддерживаются: {0}', str(path)))
    return info


def _relative(value):
    _require(isinstance(value, str) and 0 < len(value.encode('utf-8')) <= 512,
             tr('Недопустимое имя файла в архиве.'))
    parts = value.split('/')
    _require(not PurePosixPath(value).is_absolute() and all(
        part and part not in {'.', '..'} and part == part.rstrip(' .')
        and not _RESERVED.match(part)
        and not any(ord(char) < 32 or char in '<>:"\\|?*' for char in part)
        for part in parts), tr('Недопустимое имя файла в архиве.'))
    return value


def _files(root):
    result, names = {}, set()
    pending = [root]
    while pending:
        for path in sorted(pending.pop().iterdir()):
            info = path.lstat()
            _require(not path.is_symlink() and not getattr(info, 'st_file_attributes', 0) & 0x400,
                     tr('Ссылки и специальные файлы не поддерживаются: {0}', str(path)))
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
            else:
                _regular(path)
                name = _relative(path.relative_to(root).as_posix())
                if name not in _SIDECARS:
                    _require(name.casefold() not in names,
                             tr('Имена файлов конфликтуют без учёта регистра.'))
                    names.add(name.casefold())
                    result[name] = path
                    _require(len(result) <= MAX_FILES, tr('Слишком много файлов в профиле.'))
    return result


def _fingerprint(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _database(path, cancelled):
    _regular(path)
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
            db.execute('PRAGMA trusted_schema=OFF')
            db.set_progress_handler(lambda: int(bool(cancelled())), 1000)
            _require(db.execute('PRAGMA user_version').fetchone()[0] == 1,
                     tr('Версия базы данных не поддерживается.'))
            _require(db.execute('PRAGMA integrity_check(1)').fetchone()[0] == 'ok',
                     tr('База данных повреждена.'))
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table, columns in _SCHEMA.items():
                _require(table in tables and columns <= {row[1] for row in db.execute('PRAGMA table_info(' + table + ')')},
                         tr('Структура библиотеки не поддерживается.'))
            _require(db.execute('PRAGMA foreign_key_check').fetchone() is None,
                     tr('Связи записей библиотеки повреждены.'))
            _require(db.execute('SELECT 1 FROM revisions WHERE length(data)>? OR length(metadata)>? LIMIT 1',
                                (16 * 1024**2, MAX_MANIFEST_BYTES)).fetchone() is None,
                     tr('Запись библиотеки превышает допустимый размер.'))
            for data, digest, metadata in db.execute('SELECT data,sha256,metadata FROM revisions'):
                _check(cancelled)
                _require(isinstance(data, bytes) and hashlib.sha256(data).hexdigest() == digest,
                         tr('Контрольная сумма версии карты не совпадает.'))
                _require(isinstance(json.loads(metadata), dict), tr('Метаданные карты повреждены.'))
            for value, verified in db.execute('SELECT key_hex,verified FROM mf_keys'):
                _require(isinstance(value, str) and re.fullmatch('[0-9A-F]{12}', value) and verified in (0, 1),
                         tr('Запись ключа MIFARE повреждена.'))
            return {name: db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                    for name, table in (('cards', 'cards'), ('revisions', 'revisions'), ('keys', 'mf_keys'))}
    except (sqlite3.Error, json.JSONDecodeError, TypeError) as exc:
        _check(cancelled)
        raise BackupError(tr('Не удалось проверить базу данных библиотеки.')) from exc


def _snapshot(source, target, cancelled):
    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as incoming:
        with closing(sqlite3.connect(target)) as outgoing:
            incoming.backup(outgoing, pages=256, progress=lambda *_: _check(cancelled), sleep=0.05)
            outgoing.execute('PRAGMA journal_mode=DELETE')
            outgoing.commit()


def _password(value):
    _require(isinstance(value, str) and 0 < len(value.encode('utf-8')) <= 4096,
             tr('Пароль должен содержать от 1 до 4096 байт UTF-8.'))
    return value.encode('utf-8')


class _EncryptedWriter:
    def __init__(self, output, cipher):
        self.output, self.cipher, self.position = output, cipher, 0

    def write(self, value):
        self.output.write(self.cipher.encrypt(value))
        self.position += len(value)
        return len(value)

    def tell(self):
        return self.position

    def flush(self):
        self.output.flush()


def _publish_file(source, destination):
    if os.name == 'nt':
        os.rename(source, destination)  # Windows rename never replaces a file.
        return
    try:
        os.link(source, destination)
        source.unlink()
    except OSError as exc:
        if exc.errno not in {errno.EPERM, errno.EOPNOTSUPP, errno.EXDEV}:
            raise
        with destination.open('xb') as output, source.open('rb') as incoming:
            try:
                shutil.copyfileobj(incoming, output, CHUNK)
            except BaseException:
                destination.unlink()
                raise
        source.unlink()


def create_backup(profile, archive, *, password=None, cancelled=lambda: False, progress=lambda *_: None):
    """Snapshot committed SQLite state and every regular profile file into one archive."""
    root, target = Path(profile).resolve(strict=True), Path(archive).absolute()
    _require(root.is_dir(), tr('Папка профиля не найдена.'))
    _require(not target.exists(), tr('Архив уже существует. Выберите новое имя.'))
    _require(not target.resolve().is_relative_to(root), tr('Сохраняйте архив вне папки текущего профиля.'))
    _require(target.parent.is_dir(), tr('Папка для архива не найдена.'))
    if password is not None:
        _password(password)
    source_files = _files(root)
    _require('library.sqlite3' in source_files, tr('В профиле нет базы данных библиотеки.'))
    _check(cancelled)
    with tempfile.TemporaryDirectory(prefix='.pm3-backup-', dir=target.parent) as work, \
            tempfile.TemporaryDirectory(prefix='pm3-library-snapshot-') as private:
        work = Path(work)
        # A password-protected export must not stage a plaintext database on
        # the archive's potentially shared/removable destination volume.
        database = Path(private) / 'library.sqlite3'
        progress(tr('Создаём снимок библиотеки…'), 0, 0)
        _snapshot(root / 'library.sqlite3', database, cancelled)
        summary = _database(database, cancelled)
        files = dict(source_files, **{'library.sqlite3': database})
        total = sum(path.stat().st_size for path in files.values())
        _require(total <= MAX_BYTES, tr('Профиль превышает лимит 16 ГиБ.'))
        manifest = {'format': FORMAT, 'version': 1, 'application_version': APP_VERSION,
                    'created_at': datetime.now(timezone.utc).isoformat(), 'summary': summary, 'files': []}
        pending = work / 'archive.part'
        with pending.open('xb') as output:
            cipher = None
            if password is not None:
                progress(tr('Защищаем архив паролем…'), 0, 0)
                salt, nonce = get_random_bytes(16), get_random_bytes(12)
                header = MAGIC + PARAMETERS + salt + nonce
                cipher = AES.new(scrypt(_password(password), salt, 32, 2**17, 8, 1), AES.MODE_GCM, nonce=nonce, mac_len=TAG_SIZE)
                cipher.update(header)
                output.write(header)
            writer = _EncryptedWriter(output, cipher) if cipher else output
            with zipfile.ZipFile(writer, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as zipped:
                for index, (name, path) in enumerate(sorted(files.items()), 1):
                    _check(cancelled)
                    progress(tr('Добавляем файлы профиля…'), index - 1, len(files))
                    before = _regular(path)
                    digest, count = hashlib.sha256(), 0
                    with path.open('rb') as incoming, zipped.open('profile/' + name, 'w', force_zip64=True) as member:
                        while chunk := incoming.read(CHUNK):
                            _check(cancelled)
                            digest.update(chunk); count += len(chunk); member.write(chunk)
                            _require(count <= before.st_size, tr('Файл профиля изменился во время копирования.'))
                    if name == 'settings.json':
                        _settings(path)
                    _require(count == before.st_size and _fingerprint(before) == _fingerprint(_regular(path)),
                             tr('Файл профиля изменился во время копирования.'))
                    manifest['files'].append({'path': name, 'bytes': count, 'sha256': digest.hexdigest()})
                _require(set(_files(root)) == set(source_files), tr('Состав профиля изменился во время копирования.'))
                encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode('utf-8')
                _require(len(encoded) <= MAX_MANIFEST_BYTES, tr('Слишком много метаданных в архиве.'))
                zipped.writestr('manifest.json', encoded)
            if cipher:
                output.write(cipher.digest())
            output.flush(); os.fsync(output.fileno())
        _check(cancelled)
        _require(pending.stat().st_size <= MAX_ARCHIVE_BYTES, tr('Архив превышает допустимый размер.'))
        progress(tr('Сохраняем проверенный архив…'), len(files), len(files))
        _publish_file(pending, target)
    return dict(summary, path=str(target), encrypted=password is not None, files=len(files), bytes=target.stat().st_size)


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, tr('Повторяющееся поле в описании архива.'))
            result[key] = value
        return result
    try:
        return json.loads(data.decode('utf-8'), object_pairs_hook=unique,
                          parse_constant=lambda *_: (_ for _ in ()).throw(BackupError(tr('Некорректное описание архива.'))))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BackupError(tr('Некорректное описание архива.')) from exc


def _settings(path):
    if path.exists():
        _regular(path)
        _require(path.stat().st_size <= MAX_MANIFEST_BYTES, tr('Файл настроек слишком велик.'))
        _require(isinstance(_json(path.read_bytes()), dict), tr('Файл настроек повреждён.'))


def _zip_bounds(path):
    """Bound the central directory before ZipFile allocates its entry list."""
    size = path.stat().st_size
    _require(22 <= size <= MAX_ARCHIVE_BYTES, tr('Размер архива не поддерживается.'))
    with path.open('rb') as stream:
        stream.seek(-22, 2)
        signature, disk, cd_disk, on_disk, count, cd_size, offset, comment = struct.unpack('<4s4H2LH', stream.read(22))
        _require(signature == b'PK\x05\x06' and disk == cd_disk == comment == 0 and on_disk == count,
                 tr('Структура ZIP-архива не поддерживается.'))
        boundary = size - 22
        if count == 65535 or cd_size == 0xFFFFFFFF or offset == 0xFFFFFFFF:
            _require(size >= 98, tr('Некорректная структура ZIP64.'))
            stream.seek(size - 42)
            marker, zip_disk, zip_offset, disks = struct.unpack('<4sLQL', stream.read(20))
            _require(marker == b'PK\x06\x07' and zip_disk == 0 and disks == 1 and zip_offset <= size - 98,
                     tr('Некорректная структура ZIP64.'))
            stream.seek(zip_offset)
            record = struct.unpack('<4sQ2H2L4Q', stream.read(56))
            _require(record[0] == b'PK\x06\x06' and record[1] == 44 and record[4] == record[5] == 0
                     and record[6] == record[7], tr('Некорректная структура ZIP64.'))
            count, cd_size, offset, boundary = record[7], record[8], record[9], zip_offset
        _require(1 <= count <= MAX_FILES + 1 and cd_size <= MAX_MANIFEST_BYTES and offset + cd_size == boundary,
                 tr('Слишком много записей или некорректный каталог архива.'))
        stream.seek(offset)
        seen = 0
        while stream.tell() < offset + cd_size:
            record = stream.read(46)
            _require(len(record) == 46 and record[:4] == b'PK\x01\x02' and seen < count,
                     tr('Некорректный каталог ZIP-архива.'))
            stream.seek(sum(struct.unpack_from('<3H', record, 28)), 1)
            seen += 1
        _require(seen == count and stream.tell() == offset + cd_size,
                 tr('Некорректный каталог ZIP-архива.'))


@contextmanager
def _opened_archive(path, password, work, cancelled, progress):
    _regular(path)
    _require(path.stat().st_size <= MAX_ARCHIVE_BYTES, tr('Архив превышает допустимый размер.'))
    with path.open('rb') as incoming:
        first = incoming.read(HEADER_SIZE)
        encrypted = first.startswith(MAGIC)
        if encrypted:
            _require(len(first) == HEADER_SIZE and first[8:11] == PARAMETERS,
                     tr('Параметры защищённого архива не поддерживаются.'))
            _require(password is not None, tr('Для этого архива нужен пароль.'))
            _require(path.stat().st_size > HEADER_SIZE + TAG_SIZE, tr('Защищённый архив повреждён.'))
            progress(tr('Проверяем пароль и целостность архива…'), 0, 0)
            cipher = AES.new(scrypt(_password(password), first[11:27], 32, 2**17, 8, 1), AES.MODE_GCM,
                             nonce=first[27:39], mac_len=TAG_SIZE)
            cipher.update(first)
            payload = work / 'verified.zip'
            remaining = path.stat().st_size - HEADER_SIZE - TAG_SIZE
            with payload.open('xb') as output:
                while remaining:
                    _check(cancelled)
                    chunk = incoming.read(min(CHUNK, remaining))
                    _require(bool(chunk), tr('Защищённый архив повреждён.'))
                    remaining -= len(chunk); output.write(cipher.decrypt(chunk))
                try:
                    cipher.verify(incoming.read(TAG_SIZE))
                except ValueError as exc:
                    raise BackupError(tr('Неверный пароль или архив повреждён.')) from exc
        else:
            _require(first.startswith(b'PK\x03\x04'), tr('Это не архив библиотеки Proxmark Studio.'))
            payload = path
    _check(cancelled)
    _zip_bounds(payload)
    try:
        with zipfile.ZipFile(payload) as zipped:
            yield zipped, encrypted
    except (zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError) as exc:
        raise BackupError(tr('Архив повреждён или его формат не поддерживается.')) from exc


def _manifest(zipped):
    infos = zipped.infolist()
    _require(1 <= len(infos) <= MAX_FILES + 1, tr('Слишком много файлов в архиве.'))
    names = set()
    for info in infos:
        name = _relative(info.filename)
        _require(info.orig_filename == name and name.casefold() not in names,
                 tr('Имена файлов в архиве конфликтуют.'))
        names.add(name.casefold())
        mode = info.external_attr >> 16
        _require(not info.is_dir() and (stat.S_IFMT(mode) in (0, stat.S_IFREG))
                 and not info.flag_bits & 1 and info.compress_type in (zipfile.ZIP_DEFLATED, zipfile.ZIP_STORED),
                 tr('В архиве есть ссылки, специальные файлы или неподдерживаемое сжатие.'))
    _require('manifest.json' in {info.filename for info in infos}, tr('В архиве нет описания библиотеки.'))
    _require(zipped.getinfo('manifest.json').file_size <= MAX_MANIFEST_BYTES, tr('Описание архива слишком велико.'))
    manifest = _json(zipped.read('manifest.json'))
    _require(isinstance(manifest, dict) and manifest.get('format') == FORMAT and type(manifest.get('version')) is int
             and manifest['version'] == 1 and isinstance(manifest.get('files'), list),
             tr('Версия или формат резервной копии не поддерживается.'))
    files, total = {}, 0
    for item in manifest['files']:
        _require(isinstance(item, dict), tr('Некорректное описание файла в архиве.'))
        name = _relative(item.get('path'))
        count, digest = item.get('bytes'), item.get('sha256')
        _require(type(count) is int and count >= 0 and isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest)
                 and name not in _SIDECARS and name not in files, tr('Некорректное описание файла в архиве.'))
        files[name] = item; total += count
        _require(total <= MAX_BYTES, tr('Данные архива превышают лимит 16 ГиБ.'))
    _require('library.sqlite3' in files and set(info.filename for info in infos) == {'manifest.json', *('profile/' + name for name in files)},
             tr('Состав архива не совпадает с его описанием.'))
    for name, item in files.items():
        _require(zipped.getinfo('profile/' + name).file_size == item['bytes'], tr('Размер файла в архиве не совпадает.'))
    return manifest, files


def _publish_profile(staging, destination):
    if os.name == 'nt':
        os.rename(staging, destination)  # Atomic, no replacement on Windows.
        return
    destination.mkdir(mode=0o700)  # Exclusive reservation on other platforms.
    created, directories = [], [destination]
    try:
        for path in sorted(staging.rglob('*')):
            target = destination / path.relative_to(staging)
            if path.is_dir():
                target.mkdir(mode=0o700); directories.append(target)
            else:
                with target.open('xb') as output, path.open('rb') as incoming:
                    created.append(target); shutil.copyfileobj(incoming, output, CHUNK)
    except BaseException:
        for path in reversed(created):path.unlink()
        for path in reversed(directories):
            try:path.rmdir()
            except OSError:pass  # Never remove another writer's unknown files.
        raise


def restore_backup(archive, destination, *, password=None, active_profile=None,
                   cancelled=lambda: False, progress=lambda *_: None):
    """Verify everything in staging, then publish a new profile; never merge."""
    archive, target = Path(archive).resolve(strict=True), Path(destination).absolute()
    _relative(target.name)
    _require(not target.exists(), tr('Профиль уже существует. Выберите новую папку.'))
    _require(target.parent.is_dir(), tr('Родительская папка профиля не найдена.'))
    resolved = target.resolve()
    if active_profile is not None:
        active = Path(active_profile).resolve()
        _require(not resolved.is_relative_to(active) and not active.is_relative_to(resolved),
                 tr('Новый профиль должен находиться отдельно от текущего.'))
    _require(not archive.is_relative_to(resolved), tr('Архив не должен находиться внутри нового профиля.'))
    _check(cancelled)
    with tempfile.TemporaryDirectory(prefix='.pm3-restore-', dir=target.parent) as work:
        work = Path(work)
        staging = work / 'profile'; staging.mkdir(mode=0o700)
        with _opened_archive(archive, password, work, cancelled, progress) as (zipped, encrypted):
            manifest, files = _manifest(zipped)
            for index, (name, item) in enumerate(files.items(), 1):
                _check(cancelled)
                progress(tr('Проверяем и восстанавливаем файлы…'), index - 1, len(files))
                target_file = staging / name
                target_file.parent.mkdir(parents=True, exist_ok=True)
                digest, count = hashlib.sha256(), 0
                with zipped.open('profile/' + name) as incoming, target_file.open('xb') as output:
                    while chunk := incoming.read(CHUNK):
                        _check(cancelled)
                        count += len(chunk)
                        _require(count <= item['bytes'], tr('Размер распакованного файла превышен.'))
                        digest.update(chunk); output.write(chunk)
                _require(count == item['bytes'] and digest.hexdigest() == item['sha256'],
                         tr('Контрольная сумма файла не совпадает.'))
        summary = _database(staging / 'library.sqlite3', cancelled)
        _settings(staging / 'settings.json')
        _check(cancelled)
        progress(tr('Создаём отдельный профиль…'), len(files), len(files))
        _publish_profile(staging, target)
    return dict(summary, path=str(target), encrypted=encrypted, files=len(files), created_at=manifest.get('created_at', ''))


def validate_profile(profile):
    """Read-only validation before switching to an existing, restored profile."""
    root = Path(profile).resolve(strict=True)
    _require(root.is_dir(), tr('Папка профиля не найдена.'))
    summary = _database(root / 'library.sqlite3', lambda: False)
    _settings(root / 'settings.json')
    return summary
