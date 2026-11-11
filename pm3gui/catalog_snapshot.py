"""Version-bound command help, collected without opening a reader connection.

Only the client's early-return --help/--version/--fulltext switches are used.
Snapshots describe advertised syntax, never hardware support or RF validation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from types import MappingProxyType
from typing import Mapping

MAX_CATALOG_BYTES = 16 * 1024 * 1024
MAX_EXECUTABLE_BYTES = 512 * 1024 * 1024
MAX_COMMANDS = 20_000
_FORMAT = 'ProxmarkStudio.ClientCatalog.v1'
_COMMAND = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_ .+/-]{0,159}\Z')
_ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)')

_ERROR_RU = {
    'Client catalog update cancelled.':'Обновление каталога клиента отменено.',
    'Choose a client executable file.':'Выберите исполняемый файл клиента.',
    'The client changed while its fingerprint was being read.':'Файл клиента изменился при вычислении контрольной суммы.',
    'Client help exceeds the catalog size limit.':'Справка клиента превышает допустимый размер каталога.',
    'The client help dump is incomplete; the previous catalog was kept.':'Справка клиента получена не полностью; предыдущий каталог сохранён.',
    'The client returned an invalid command name.':'Клиент вернул некорректное имя команды.',
    'The client returned conflicting help for one command.':'Клиент вернул противоречащие варианты справки одной команды.',
    'The client returned too many commands.':'Клиент вернул слишком много команд.',
    'The client did not return a recognizable command catalog.':'Клиент не вернул распознаваемый каталог команд.',
    'Invalid client catalog fingerprint.':'Некорректная контрольная сумма каталога клиента.',
    'Invalid client catalog identity.':'Некорректные сведения о клиенте в каталоге.',
    'Invalid command catalog size.':'Некорректный размер каталога команд.',
    'Invalid cached command entry.':'Некорректная запись команды в сохранённом каталоге.',
    'Unknown client catalog cache format.':'Формат сохранённого каталога клиента не поддерживается.',
    'Invalid cached command catalog.':'Некорректный сохранённый каталог команд.',
    'Duplicate cached command entries.':'Повторяющиеся команды в сохранённом каталоге.',
    'Invalid client catalog cache.':'Некорректный файл каталога клиента.',
    'Client catalog cache exceeds the size limit.':'Файл каталога клиента превышает допустимый размер.',
    'The saved client catalog could not be read.':'Не удалось прочитать сохранённый каталог клиента.',
    'The client changed. Update its command catalog before using these tools.':'Клиент изменился. Перед использованием инструментов обновите каталог команд.',
    'Only offline client-help switches may be probed.':'Для проверки разрешены только параметры справки без подключения к плате.',
    'The client help request timed out.':'Истекло время получения справки клиента.',
    'Choose a catalog timeout between 0 and 180 seconds.':'Укажите время получения каталога больше 0 и не более 180 секунд.',
    'Choose an existing client executable file.':'Выберите существующий исполняемый файл клиента.',
    'This client does not advertise an offline full-help export.':'Этот клиент не заявляет экспорт полной справки без подключения к плате.',
    'The client did not return a recognizable version.':'Клиент не вернул распознаваемую версию.',
    'The client changed during the help export. Update the catalog again.':'Клиент изменился во время получения справки. Обновите каталог ещё раз.',
    'Update the command catalog for the selected client before using these tools.':'Перед использованием инструментов обновите каталог команд выбранного клиента.',
    'The selected client does not advertise this command.':'Выбранный клиент не заявляет эту команду.',
    'The selected client does not provide syntax help for this command.':'Выбранный клиент не предоставляет справку о синтаксисе этой команды.',
    'This command syntax changed. Its existing form is not confirmed for the selected client.':'Синтаксис команды изменился. Существующая форма не подтверждена для выбранного клиента.',
    'Command arguments exceed the supported length.':'Параметры команды превышают допустимую длину.',
    'Only one client command with balanced quotes is allowed.':'Разрешена одна команда клиента с закрытыми кавычками.',
}


class CatalogError(ValueError):
    def __str__(self):
        from .i18n import language
        message = super().__str__()
        if language() != 'ru':return message
        if message in _ERROR_RU:return _ERROR_RU[message]
        match = re.fullmatch(r'The client could not export its command help \(code (-?\d+)\)\.', message)
        return ('Клиент не смог экспортировать справку команд (код '+match[1]+').' if match else message)


class CatalogCancelled(CatalogError):
    pass


def _check_cancelled(cancelled):
    if cancelled and cancelled():
        raise CatalogCancelled('Client catalog update cancelled.')


def executable_sha256(path, cancelled=None):
    executable = Path(path).expanduser().resolve(strict=True)
    if not executable.is_file():
        raise CatalogError('Choose a client executable file.')
    before = executable.stat()
    if before.st_size > MAX_EXECUTABLE_BYTES:
        raise CatalogError('Choose a client executable file.')
    digest = hashlib.sha256()
    size = 0
    with executable.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            _check_cancelled(cancelled)
            size += len(chunk)
            if size > MAX_EXECUTABLE_BYTES:raise CatalogError('Choose a client executable file.')
            digest.update(chunk)
    after = executable.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise CatalogError('The client changed while its fingerprint was being read.')
    return digest.hexdigest()


def _clean(value):
    value = _ANSI.sub('', value.replace('\r\n', '\n').replace('\r', '\n'))
    return ''.join(ch for ch in value if ch in '\n\t' or ord(ch) >= 32 and ord(ch) != 127)


def decode_help(raw):
    """Preserve UTF-8 and the CP1252 micro sign emitted by older Windows builds."""
    if isinstance(raw, str):
        return _clean(raw)
    # surrogateescape preserves valid UTF-8 runs without mojibake in the rest.
    decoded = raw.decode('utf-8-sig', errors='surrogateescape')
    decoded = ''.join(bytes([ord(ch)-0xDC00]).decode('cp1252', errors='replace')
                      if 0xDC80 <= ord(ch) <= 0xDCFF else ch for ch in decoded)
    return _clean(decoded)


def version_identity(raw):
    """Drop session-log paths from native --version startup chatter."""
    text = decode_help(raw)
    match = re.search(r'^\s*Client:\s*(.+)$', text, re.M)
    if match:
        return 'Client: '+match[1].strip()
    lines = [line.strip() for line in text.splitlines()
             if line.strip() and not re.search(r'\bsession\s+log\b', line, re.I)]
    return '\n'.join(lines)


def _without_category_banner(help_text):
    # dumpCommandsRecursive emits this between leaf command blocks. It must
    # also be removed from older bundled catalogs made by a simpler extractor.
    category = re.search(r'(?:^|\n)={20,}\n[A-Za-z0-9_][A-Za-z0-9_ .+/-]{0,159}\n\s*\{[^\n]*\}', help_text)
    return (help_text[:category.start()] if category else help_text).strip()


def syntax_fingerprint(help_text):
    """Descriptions may change freely; generated forms require the same grammar."""
    text = _without_category_banner(decode_help(help_text))
    usage = re.search(r'^usage:\s*\n(.*?)(?=^options:|\Z)', text, re.M | re.S | re.I)
    options = re.search(r'^options:\s*\n(.*?)(?=^examples/notes:|\Z)', text, re.M | re.S | re.I)
    grammar = [' '.join(usage[1].split()) if usage else '']
    if options:
        for line in options[1].splitlines():
            match = re.match(r'^\s+((?:--?[^\s,<>]+(?:,\s*--?[^\s,<>]+)*|<[^>]+>)(?:\s+<[^>]+>)?)\s{2,}', line)
            if match:
                grammar.append(' '.join(match[1].split()))
    # Legacy commands without usage/options cannot acquire trust from a menu.
    if not any(grammar):
        grammar = [text.strip()]
    return hashlib.sha256('\n'.join(grammar).encode('utf-8')).hexdigest()


def parse_fulltext(raw):
    if len(raw.encode('utf-8') if isinstance(raw, str) else raw) > MAX_CATALOG_BYTES:
        raise CatalogError('Client help exceeds the catalog size limit.')
    text = decode_help(raw)
    if not re.search(r'^Full help dump done\.\s*\Z', text, re.M):
        raise CatalogError('The client help dump is incomplete; the previous catalog was kept.')
    blocks = re.finditer(r'^-{20,}\n([^\n]+)\navailable offline: ([^\n]+)\n(.*?)(?=^-{20,}\n|^Full help dump done\.)', text, re.M | re.S)
    rows = {}
    block_count = 0
    for match in blocks:
        block_count += 1
        command = match[1].strip()
        if not _COMMAND.fullmatch(command) or '  ' in command or command.startswith('-') or match[2] not in {'yes', 'no'}:
            raise CatalogError('The client returned an invalid command name.')
        help_text = _without_category_banner(match[3])
        # dumpCommandsRecursive prints category banners between leaf entries.
        # They belong to neither leaf; retaining them creates false changes on
        # the last help/menu command of a category when another category moves.
        row = {'command': command, 'offline': match[2] == 'yes',
               'description': help_text.split('\n', 1)[0], 'help': help_text}
        if command in rows and rows[command] != row:
            raise CatalogError('The client returned conflicting help for one command.')
        rows[command] = row
        if len(rows) > MAX_COMMANDS:
            raise CatalogError('The client returned too many commands.')
    if not rows or 'help' not in rows or block_count != len(re.findall(r'^-{20,}\n', text, re.M)):
        raise CatalogError('The client did not return a recognizable command catalog.')
    return rows


@dataclass(frozen=True)
class CatalogSnapshot:
    client_path: str
    client_sha256: str
    version: str
    captured_at: str
    commands: Mapping[str, Mapping]

    def __post_init__(self):
        if not isinstance(self.client_sha256, str) or not re.fullmatch(r'[a-f0-9]{64}', self.client_sha256):
            raise CatalogError('Invalid client catalog fingerprint.')
        if (not isinstance(self.client_path, str) or not 1 <= len(self.client_path) <= 4096
                or any(ord(ch) < 32 for ch in self.client_path)
                or not isinstance(self.version, str) or not 1 <= len(self.version) <= 4096
                or any(ord(ch) < 32 for ch in self.version)
                or not isinstance(self.captured_at, str) or not 1 <= len(self.captured_at) <= 80):
            raise CatalogError('Invalid client catalog identity.')
        try:
            self.client_path.encode('utf-8');self.version.encode('utf-8')
            datetime.fromisoformat(self.captured_at)
        except (UnicodeError, ValueError) as exc:
            raise CatalogError('Invalid client catalog identity.') from exc
        if not isinstance(self.commands, Mapping) or not 1 <= len(self.commands) <= MAX_COMMANDS:
            raise CatalogError('Invalid command catalog size.')
        rows = {}
        for name, row in self.commands.items():
            if (not isinstance(name, str) or not _COMMAND.fullmatch(name)
                    or not isinstance(row, Mapping) or row.get('command') != name
                    or type(row.get('offline')) is not bool
                    or not isinstance(row.get('help'), str)
                    or not isinstance(row.get('description'), str)):
                raise CatalogError('Invalid cached command entry.')
            try:
                row['help'].encode('utf-8');row['description'].encode('utf-8')
            except UnicodeError as exc:
                raise CatalogError('Invalid cached command entry.') from exc
            rows[name] = MappingProxyType({key: row[key] for key in ('command', 'offline', 'help', 'description')})
        object.__setattr__(self, 'commands', MappingProxyType(rows))

    def matches(self, executable):
        try:
            return (os.path.normcase(str(Path(executable).expanduser().resolve()))
                    == os.path.normcase(self.client_path)
                    and executable_sha256(executable) == self.client_sha256)
        except (OSError, ValueError):
            return False

    def to_dict(self):
        return {'format': _FORMAT, 'client_path': self.client_path,
                'client_sha256': self.client_sha256, 'version': self.version,
                'captured_at': self.captured_at,
                'commands': [dict(self.commands[name]) for name in sorted(self.commands)]}

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or value.get('format') != _FORMAT:
            raise CatalogError('Unknown client catalog cache format.')
        rows = value.get('commands')
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_COMMANDS:
            raise CatalogError('Invalid cached command catalog.')
        try:
            commands = {row['command']: row for row in rows}
            if len(commands) != len(rows):
                raise CatalogError('Duplicate cached command entries.')
            return cls(value['client_path'], value['client_sha256'], value['version'], value['captured_at'], commands)
        except (KeyError, TypeError) as exc:
            raise CatalogError('Invalid client catalog cache.') from exc


@dataclass(frozen=True)
class CatalogDiff:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]


def compare_catalogs(previous, current):
    before = previous.commands if isinstance(previous, CatalogSnapshot) else previous
    after = current.commands if isinstance(current, CatalogSnapshot) else current
    return CatalogDiff(tuple(sorted(after.keys() - before.keys())),
                       tuple(sorted(before.keys() - after.keys())),
                       tuple(sorted(name for name in before.keys() & after.keys()
                                    if syntax_fingerprint(before[name]['help']) != syntax_fingerprint(after[name]['help'])
                                    or before[name]['offline'] != after[name]['offline'])))


def save_catalog(snapshot, path):
    path = Path(path)
    raw = json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2).encode('utf-8')
    if len(raw) > MAX_CATALOG_BYTES:
        raise CatalogError('Client catalog cache exceeds the size limit.')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name+'.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_catalog(path, executable=None):
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(MAX_CATALOG_BYTES + 1)
        if len(raw) > MAX_CATALOG_BYTES:
            raise CatalogError('Client catalog cache exceeds the size limit.')
        snapshot = CatalogSnapshot.from_dict(json.loads(raw))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise CatalogError('The saved client catalog could not be read.') from exc
    if executable is not None and not snapshot.matches(executable):
        raise CatalogError('The client changed. Update its command catalog before using these tools.')
    return snapshot


def _run_offline(executable, argument, timeout, cancelled=None):
    """Drain bounded output to files; never pass serial or interactive commands."""
    if argument not in {'--help', '--version', '--fulltext'}:
        raise CatalogError('Only offline client-help switches may be probed.')
    env = dict(os.environ)
    for key in ('QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH', 'PYTHONHOME', 'PYTHONPATH'):
        env.pop(key, None)
    client_dir = Path(executable).parent
    env['PATH'] = os.pathsep.join((str(client_dir), str(client_dir/'libs'), env.get('PATH', '')))
    env['QT_QPA_PLATFORM'] = 'offscreen'
    with tempfile.TemporaryDirectory(prefix='pm3-catalog-') as folder:
        output, errors = Path(folder)/'stdout', Path(folder)/'stderr'
        with output.open('wb') as stdout, errors.open('wb') as stderr:
            process = subprocess.Popen([str(executable), argument], cwd=folder, env=env,
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            deadline = time.monotonic() + timeout
            try:
                while True:
                    _check_cancelled(cancelled)
                    if output.stat().st_size + errors.stat().st_size > MAX_CATALOG_BYTES:
                        raise CatalogError('Client help exceeds the catalog size limit.')
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CatalogError('The client help request timed out.')
                    try:
                        process.wait(timeout=min(0.05, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
        if output.stat().st_size + errors.stat().st_size > MAX_CATALOG_BYTES:
            raise CatalogError('Client help exceeds the catalog size limit.')
        if process.returncode:
            raise CatalogError('The client could not export its command help (code '+str(process.returncode)+').')
        return output.read_bytes()


def capture_catalog(executable, *, cancelled=None, progress=None, timeout=60, probe=None):
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 180:
        raise CatalogError('Choose a catalog timeout between 0 and 180 seconds.')
    try:
        executable = Path(executable).expanduser().resolve(strict=True)
    except (OSError, TypeError) as exc:
        raise CatalogError('Choose an existing client executable file.') from exc
    before = executable_sha256(executable, cancelled)
    runner = probe or _run_offline
    deadline = time.monotonic() + timeout
    def request(argument):
        _check_cancelled(cancelled)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CatalogError('The client help request timed out.')
        if progress:
            progress(argument)
        return runner(executable, argument, remaining, cancelled)
    basic_help = decode_help(request('--help'))
    if not re.search(r'(?<![\w-])--fulltext(?![\w-])', basic_help):
        raise CatalogError('This client does not advertise an offline full-help export.')
    rows = parse_fulltext(request('--fulltext'))
    version = version_identity(request('--version'))
    if not version or len(version) > 4096:
        raise CatalogError('The client did not return a recognizable version.')
    after = executable_sha256(executable, cancelled)
    if before != after:
        raise CatalogError('The client changed during the help export. Update the catalog again.')
    return CatalogSnapshot(str(executable), before, version,
                           datetime.now(timezone.utc).isoformat(timespec='seconds'), rows)


def require_confirmed_command(snapshot, executable, command, arguments='', *, reference=None):
    """Bind existing forms to a selected EXE, refusing removed or changed syntax."""
    if not isinstance(snapshot, CatalogSnapshot) or not snapshot.matches(executable):
        raise CatalogError('Update the command catalog for the selected client before using these tools.')
    if not isinstance(command, str) or not _COMMAND.fullmatch(command):
        raise CatalogError('The client returned an invalid command name.')
    row = snapshot.commands.get(command)
    if row is None:
        raise CatalogError('The selected client does not advertise this command.')
    if not row['help'].strip():
        raise CatalogError('The selected client does not provide syntax help for this command.')
    if reference is not None:
        known = reference.get(command)
        if (known is None or known['offline'] != row['offline']
                or syntax_fingerprint(known['help']) != syntax_fingerprint(row['help'])):
            raise CatalogError('This command syntax changed. Its existing form is not confirmed for the selected client.')
    # Retain the existing one-command lexer boundary even when no arguments exist.
    if not isinstance(arguments, str) or len(arguments.encode('utf-8')) > 3000:
        raise CatalogError('Command arguments exceed the supported length.')
    if any(ord(ch) < 32 or ch in ';|&`' for ch in arguments) or arguments.count('"') % 2:
        raise CatalogError('Only one client command with balanced quotes is allowed.')
    return row
