"""Read-only download of Flipper Zero NFC key files over its USB CDC CLI.

The framing follows the public Flipper storage CLI protocol: size-prefixed
``read_chunks`` data, per-chunk Ready handshakes and the final CLI prompt.
No storage mutation or firmware command is implemented here.
"""
from __future__ import annotations
from .i18n import tr

from collections import deque
import hashlib
from pathlib import Path, PurePosixPath
import re
import time
from uuid import uuid4

import serial
from serial.tools import list_ports


MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_FILES = 512
MAX_DEPTH = 3
MAX_DIRECTORIES = 128
CHUNK_SIZE = 4096
IO_TIMEOUT = 6.0
PROMPT = b">: "
USER_DICTIONARY = "/ext/nfc/assets/mf_classic_dict_user.nfc"
_UNSAFE_NAME = re.compile(r'[\x00-\x1f\x7f"\\/:;|&`<>*?]')
_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?\Z", re.I)


class FlipperError(RuntimeError):
    """A Flipper transport, protocol or local download error."""


class FlipperCancelled(FlipperError):
    """The caller cancelled before all requested files were downloaded."""


class _StorageError(FlipperError):
    pass


def available_flippers() -> list[dict]:
    """Enumerate recognized Flipper CDC ports without opening any port."""
    result = []
    for port in list_ports.comports():
        if port.vid != 0x0483 or port.pid != 0x5740:
            continue
        serial_number = str(port.serial_number or "")
        description = " ".join(str(getattr(port, field, "") or "")
                               for field in ("description", "product", "manufacturer"))
        if not serial_number.upper().startswith("FLIP_") and "flipper" not in description.casefold():
            continue
        result.append({"device": port.device, "description": port.description or "Flipper Zero",
                       "serial_number": serial_number})
    return sorted(result, key=lambda item: item["device"])


def _component(name: str) -> bool:
    return (isinstance(name, str) and bool(name) and name not in {".", ".."}
            and not name.endswith((".", " ")) and _UNSAFE_NAME.search(name) is None
            and _RESERVED.fullmatch(name) is None and len(name.encode("utf-8")) <= 255)


def _remote_path(path: str) -> str:
    value = PurePosixPath(path)
    if not value.is_absolute() or value.parts[:3] != ("/", "ext", "nfc"):
        raise FlipperError(tr('Разрешено чтение только папки NFC на microSD Flipper.'))
    if any(not _component(part) for part in value.parts[1:]):
        raise FlipperError(tr('Недопустимое имя файла Flipper.'))
    return str(value)


class _CLI:
    def __init__(self, stream, cancelled=None):
        self.stream = stream
        self.buffer = bytearray()
        self.cancelled = cancelled or (lambda: False)

    def check_cancelled(self):
        if self.cancelled():
            raise FlipperCancelled(tr('Получение файлов Flipper отменено.'))

    def _pull(self):
        self.check_cancelled()
        try:
            data = self.stream.read(max(1, min(self.stream.in_waiting, 8192)))
        except (OSError, serial.SerialException) as exc:
            raise FlipperError(tr('Соединение с Flipper прервано. Проверьте USB и повторите импорт.')) from exc
        self.buffer.extend(data)
        return bool(data)

    def until(self, marker: bytes, timeout=IO_TIMEOUT, limit=256 * 1024) -> bytes:
        deadline = time.monotonic() + timeout
        while True:
            self.check_cancelled()
            index = self.buffer.find(marker)
            if index >= 0:
                result = bytes(self.buffer[:index])
                del self.buffer[:index + len(marker)]
                return result
            if len(self.buffer) > limit:
                raise FlipperError(tr('Ответ Flipper превышает допустимый размер; импорт остановлен.'))
            if time.monotonic() >= deadline:
                raise FlipperError(tr('Flipper не отвечает. Закройте qFlipper/терминал и повторите импорт.'))
            self._pull()

    def exact(self, size: int) -> bytes:
        result = bytearray()
        deadline = time.monotonic() + IO_TIMEOUT
        while len(result) < size:
            self.check_cancelled()
            count = min(size - len(result), len(self.buffer))
            if count:
                result.extend(self.buffer[:count])
                del self.buffer[:count]
                deadline = time.monotonic() + IO_TIMEOUT
                continue
            if time.monotonic() >= deadline:
                raise FlipperError(tr('Передача файла Flipper прервана или не завершилась.'))
            self._pull()
        return bytes(result)

    def send(self, value: bytes):
        self.check_cancelled()
        try:
            written = self.stream.write(value)
        except (OSError, serial.SerialException) as exc:
            raise FlipperError(tr('Не удалось обратиться к Flipper через USB.')) from exc
        if written != len(value):
            raise FlipperError(tr('Команда Flipper отправлена не полностью.'))

    def start(self):
        # Opening CDC provides a banner/prompt. Do not send an extra newline:
        # it can queue a second prompt and desynchronise the next response.
        self.until(PROMPT)
        self.buffer.clear()
        self.stream.reset_input_buffer()
        info = self.command("device_info")
        if b"hardware_model" not in info:
            raise FlipperError(tr('Устройство не ответило как CLI Flipper Zero.'))

    def command(self, command: str) -> bytes:
        self.send(command.encode("utf-8") + b"\r")
        data = self.until(PROMPT)
        lines = data.split(b"\r\n")
        if lines and lines[0].strip() == command.encode("utf-8"):
            lines.pop(0)
        body = b"\r\n".join(lines)
        self._check_error(body)
        return body

    @staticmethod
    def _check_error(data: bytes):
        match = re.search(rb"Storage error:\s*([^\r\n]+)", data)
        if match:
            reason = match.group(1).decode("utf-8", "replace")
            raise _StorageError(reason)

    def stat(self, remote: str) -> int:
        body = self.command(f'storage stat "{_remote_path(remote)}"')
        match = re.search(rb"File,\s*size:\s*(\d+)b?\s*(?:\r?\n|$)", body)
        if not match:
            raise _StorageError(tr('не является обычным файлом'))
        return int(match.group(1))

    def list(self, remote: str) -> list[dict]:
        body = self.command(f'storage list "{_remote_path(remote)}"')
        result = []
        for raw in body.split(b"\r\n"):
            try:
                line = raw.decode("utf-8").strip()
            except UnicodeError:
                continue
            directory = re.fullmatch(r"\[D\]\s+(.+)", line)
            file = re.fullmatch(r"\[F\]\s+(.+)\s+(\d+)b?", line)
            if directory:
                result.append({"directory": True, "name": directory.group(1)})
            elif file:
                result.append({"directory": False, "name": file.group(1), "size": int(file.group(2))})
        return result

    def read_file(self, remote: str, expected_size: int) -> bytes:
        path = _remote_path(remote)
        command = f'storage read_chunks "{path}" {CHUNK_SIZE}'
        self.send(command.encode("utf-8") + b"\r")
        while True:
            header = self.until(b"\r\n", limit=4096).strip()
            if header and header != command.encode("utf-8"):
                break
        if b"Storage error:" in header:
            self.until(PROMPT)
            self._check_error(header)
        match = re.fullmatch(rb"Size:\s*(\d+)", header)
        if not match:
            raise FlipperError(tr('Flipper не подтвердил размер загружаемого файла.'))
        size = int(match.group(1))
        if size != expected_size or size > MAX_FILE_BYTES:
            raise FlipperError(tr('Размер файла Flipper изменился или превышает ограничение 8 МиБ.'))
        data = bytearray()
        while len(data) < size:
            ready = self.until(b"Ready?\r\n", limit=4096)
            if ready.strip():
                raise FlipperError(tr('Неожиданный ответ Flipper при подготовке части файла.'))
            self.send(b"y")
            data.extend(self.exact(min(CHUNK_SIZE, size - len(data))))
        self.until(PROMPT, limit=4096)
        digest_response = self.command(f'storage md5 "{path}"')
        digest_lines = [line.strip() for line in digest_response.split(b"\r\n") if line.strip()]
        if len(digest_lines) != 1 or re.fullmatch(rb"[0-9A-Fa-f]{32}", digest_lines[0]) is None:
            raise FlipperError(tr('Flipper не подтвердил контрольную сумму файла.'))
        if hashlib.md5(data).hexdigest().encode("ascii") != digest_lines[0].lower():
            raise _StorageError(tr('файл изменился при чтении или контрольная сумма не совпала'))
        return bytes(data)


def fetch_key_files(port: str, destination: Path, *, progress=None, cancelled=None) -> dict:
    """Download user dictionary, key caches, NFC originals and shadow copies.

    Returned ``files`` contain only complete, size- and MD5-verified downloads.
    Files already present locally are never replaced. The caller owns database
    import and can run this synchronous function in a worker thread.
    """
    if cancelled and cancelled():
        raise FlipperCancelled(tr('Получение файлов Flipper отменено.'))
    matches = [item for item in available_flippers() if item["device"] == port]
    if len(matches) != 1:
        raise FlipperError(tr('Выбранный Flipper Zero не найден по USB. Подключите его и обновите список.'))
    destination = Path(destination).expanduser().resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise FlipperError(tr('Для загрузки Flipper нужна новая или пустая папка.'))
    destination.mkdir(parents=True, exist_ok=True)
    report = {"port": port, "files": [], "skipped": [], "errors": []}
    notify = progress or (lambda message: None)
    try:
        stream = serial.Serial(port, 115200, timeout=0.15, write_timeout=3)
    except (OSError, serial.SerialException) as exc:
        raise FlipperError(tr('Не удалось открыть Flipper. Закройте qFlipper и другие программы, использующие его USB-порт.')) from exc
    try:
        cli = _CLI(stream, cancelled)
        cli.start()
        notify(tr('Flipper подключён. Поиск словаря и файлов NFC…'))
        candidates = [USER_DICTIONARY]
        visited = set()
        pending = deque([("/ext/nfc", 0), ("/ext/nfc/.cache", 0)])
        while pending:
            cli.check_cancelled()
            remote, depth = pending.popleft()
            if remote in visited:
                continue
            visited.add(remote)
            if len(visited) > MAX_DIRECTORIES:
                report["skipped"].append({"remote": remote, "reason": tr('достигнут предел папок')})
                continue
            try:
                entries = cli.list(remote)
            except _StorageError as exc:
                target = "skipped" if "not exist" in str(exc).lower() else "errors"
                report[target].append({"remote": remote, "reason": str(exc), "error": str(exc)})
                continue
            for entry in entries:
                name = entry["name"]
                if not _component(name):
                    report["skipped"].append({"remote": remote, "reason": tr('имя несовместимо с локальным путём')})
                    continue
                child = remote + "/" + name
                if entry["directory"]:
                    if name.startswith(".") or (remote == "/ext/nfc" and name == "assets"):
                        continue
                    if depth < MAX_DEPTH and remote != "/ext/nfc/.cache":
                        pending.append((child, depth + 1))
                    else:
                        report["skipped"].append({"remote": child, "reason": tr('достигнута глубина поиска')})
                elif (remote == "/ext/nfc/.cache" and name.lower().endswith(".keys")) or (
                        remote != "/ext/nfc/.cache" and name.lower().endswith((".nfc", ".shd"))):
                    candidates.append(child)
        total = 0
        for remote in dict.fromkeys(candidates):
            cli.check_cancelled()
            if len(report["files"]) >= MAX_FILES:
                report["skipped"].append({"remote": remote, "reason": tr('достигнут предел числа файлов')})
                continue
            try:
                size = cli.stat(remote)
                if size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES:
                    report["skipped"].append({"remote": remote, "reason": tr('достигнут предел 8 МиБ')})
                    continue
                relative = PurePosixPath(remote).relative_to("/ext")
                local = destination.joinpath(*relative.parts).resolve()
                if not local.is_relative_to(destination) or local.exists():
                    raise FlipperError(tr('Локальный путь файла небезопасен или уже существует.'))
                notify(tr('Загрузка файла {0}: {1}', f"{len(report['files']) + 1}", f'{remote}'))
                data = cli.read_file(remote, size)
                cli.check_cancelled()
                local.parent.mkdir(parents=True, exist_ok=True)
                temporary = local.with_name(f".{local.name}.{uuid4().hex}.part")
                temporary.write_bytes(data)
                temporary.rename(local)
                report["files"].append({"remote": remote, "local": str(local), "size": size,
                                         "md5": hashlib.md5(data).hexdigest(),
                                         "sha256": hashlib.sha256(data).hexdigest(),
                                         "verified_by": "size+remote_md5"})
                total += size
            except _StorageError as exc:
                target = "skipped" if "not exist" in str(exc).lower() else "errors"
                report[target].append({"remote": remote, "reason": str(exc), "error": str(exc)})
        notify(tr('Загружено файлов Flipper: {0}.', f"{len(report['files'])}"))
        return report
    except OSError as exc:
        raise FlipperError(tr('Не удалось сохранить загруженный файл. Проверьте папку и свободное место на диске.')) from exc
    finally:
        try:
            stream.close()
        except (OSError, serial.SerialException):
            pass
