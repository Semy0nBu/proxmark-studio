"""Typed Proxmark3 CLI integration; the GUI never opens the serial port itself."""

from __future__ import annotations
from .i18n import tr

import codecs
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal
from serial.tools import list_ports

from .operations import OPERATION_TIMEOUT_MS, RECOVERY_OPERATION_KINDS, Operation
from .paths import application_root, is_frozen
from .process_env import external_process_libraries


ALLOWED_COMMANDS = frozenset({"hw version", "hf search", "lf search"})
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_RECOVERY_TIMEOUT_MS = 30 * 60 * 1000
RECOVERY_TAIL_CHARACTERS = 256 * 1024
MAX_REPEATED_AUTH2_ERRORS = 12
_AUTH2_ERROR = re.compile(r"\bAuth2 error len\s*=\s*1(?!\d)", re.I)
_NONCE_PROGRESS = re.compile(r"^\s*(?:\[[^\]\n]+\]\s*)?\d+(?:\.\d+)?\s*\|\s*(\d+)\s*\|")
_KEY_PROGRESS = re.compile(r"found valid key\s*\[\s*[0-9A-F]{12}\s*\]|Brute force completed\.\s*Key found:\s*[0-9A-F]{12}", re.I)
_MAX_PROBE_BYTES = 64 * 1024
_KILL_DELAY_MS = 1000


@dataclass
class DeviceSettings:
    client_path: str = ""
    port: str = ""
    working_dir: str = ""
    transport: str = "usb"
    keep_open: bool = False
    require_session: bool = False

    def __post_init__(self) -> None:
        _validate_transport(self.transport)
        if type(self.require_session) is not bool:raise ValueError('Invalid session requirement.')
        if type(self.keep_open) is not bool:
            raise ValueError(tr('Режим постоянного соединения должен быть логическим значением.'))


def _validate_transport(transport: str) -> None:
    if transport not in ("usb", "blueshark", "tcp", "offline"):
        raise ValueError(tr('Тип подключения должен быть USB или Blue Shark.'))


def operation_supports_bulk_cancel(operation: Operation) -> bool:
    """Exact acquisition commands covered by the native bulk-cancel extension."""
    if type(operation) is not Operation:
        raise ValueError(tr('Ожидалась типизированная операция клиента.'))
    return (operation.kind == 'lf_capture_search_buffer'
            or operation.kind == 'expert_tool' and operation.client_command in {
                'data hexsamples', 'data samples', 'lf read', 'lf sniff',
                'hf texkom reader', 'hf search'}
            or operation.kind == 'hf_tool' and operation.hf_command in {
                'hf texkom reader', 'hf search'}
            or operation.kind == 'lf_tool' and operation.lf_command in {'lf read', 'lf sniff'})


def _operation_preserves_recovery_tail(operation: Operation) -> bool:
    from .hf_catalog import is_recovery_tool
    return (operation.kind in RECOVERY_OPERATION_KINDS
            or operation.kind == 'hf_tool' and is_recovery_tool(operation.hf_command)
            or operation.kind == 'lf_tool' and operation.lf_command == 'lf em 410x sim'
            or operation.kind == 'expert_tool' and is_recovery_tool(operation.client_command))


def operation_requires_resync(operation: Operation) -> bool:
    """Identify interruptions that need a fresh device reply before continuing."""
    return operation_supports_bulk_cancel(operation) or _operation_preserves_recovery_tail(operation)


def available_ports() -> list[dict[str, str]]:
    """Enumerate ports without opening a connection or sending device commands."""
    ports = []
    for item in sorted(list_ports.comports(), key=lambda item: item.device):
        description = getattr(item, "description", "") or ""
        hwid = getattr(item, "hwid", "") or ""
        identity = " ".join((description, hwid))
        is_pm3_usb = ((getattr(item, "vid", None), getattr(item, "pid", None)) == (0x9AC4, 0x4B8F)
                      or re.search(r"\b(?:VID[:=_]?)?9AC4[:&+_ ](?:PID[:=_]?)?4B8F\b", hwid, re.I))
        is_bluetooth = re.search(r"bluetooth|bthenum|bthmodem|rfcomm|блютуз", identity, re.I)
        kind = "usb" if is_pm3_usb else "bluetooth" if is_bluetooth else "other"
        ports.append({"device": item.device, "description": description, "hwid": hwid, "kind": kind})
    return ports


def parse_connection_status(output: str, expected_transport: str = "usb", returncode: int = 0,
                            *, verified_transport: str | None = None) -> dict:
    """Require device OS and an explicit negotiated channel from this one run.

    A client banner, exit0, selected COM name, or BT support compile flag alone
    never confirms that the device was reached. FPC UART confirms the BlueShark
    profile's transport; Windows pairing/port metadata identifies the accessory.
    """
    _validate_transport(expected_transport)
    text = _StreamCleaner().feed(output.replace("\r\n", "\n").replace("\r", "\n"))
    channels = re.findall(r"Communicating with PM3 over\s+(USB-CDC|FPC\s+(?:UART|USART))\b([^\n]*)", text, re.I)
    observed = {"tcp" if re.search(r'\bover TCP\b', suffix, re.I) else
                "usb" if value.upper() == "USB-CDC" else "blueshark" for value, suffix in channels}
    # Only an already verified, still-open client may supply its channel here.
    # A fresh OS reply remains mandatory for this command.
    if not observed and verified_transport in ("usb", "blueshark", "tcp"):
        observed.add(verified_transport)
    transport = next(iter(observed)) if len(observed) == 1 else None
    firmware_rows = re.findall(r"^\s*(?:\[[=+!]\]\s*)?OS\.{2,}\s+(.+?)\s*$", text, re.M)
    firmware = firmware_rows[-1].strip() or None if firmware_rows else None
    result = {"connected": False, "transport": transport, "firmware": firmware, "message": ""}
    if returncode != 0:
        result["message"] = tr('Проверка подключения завершилась с кодом {0}.', f'{returncode}')
    elif re.search(r"\bOFFLINE\s+mode\b|Capabilities structure version.*not the one expected", text, re.I):
        result["message"] = tr('Клиент не подтвердил рабочее соединение с устройством.')
    elif len(observed) > 1:
        result["message"] = tr('Получены противоречивые данные о канале подключения. Повторите проверку.')
    elif transport is None or firmware is None:
        result["message"] = tr('Устройство не подтвердило канал подключения и версию OS.')
    elif transport != expected_transport:
        requested = "TCP" if expected_transport == "tcp" else "Blue Shark" if expected_transport == "blueshark" else "USB"
        actual = "TCP" if transport == "tcp" else "UART (Blue Shark)" if transport == "blueshark" else "USB"
        result["message"] = tr('Выбран {0}, но устройство ответило через {1}. Проверьте COM-порт.', f'{requested}', f'{actual}')
    else:
        result["connected"] = True
        result["message"] = 'TCP connection verified.' if transport == 'tcp' else tr('Подключение через Blue Shark (UART) подтверждено.') if transport == "blueshark" else tr('Подключение через USB подтверждено.')
    return result


def discover_client(project_root: Path | None = None) -> str:
    """Look only in known project locations, never recursively search the disk."""
    root = Path(project_root) if project_root is not None else application_root()
    roots = list(dict.fromkeys([application_root(), root])) if is_frozen() else [root]
    candidates = [path for location in roots for path in
                  (location / "client" / "proxmark3.exe", location / "proxmark3.exe")]
    legacy = root / "reference" / "legacy"
    if not is_frozen() and legacy.is_dir():
        candidates.extend(sorted(legacy.glob("*/client/proxmark3.exe")))
    return next((str(path.resolve()) for path in candidates if path.is_file()), "")


def build_invocation(
    settings: DeviceSettings, command: str, offline: bool = False,
) -> tuple[str, list[str], str, QProcessEnvironment]:
    """Validate one catalog operation and create a child-only environment.

    Incognito is added by ClientRunner after querying this executable's help.
    Proxmark resolves packaged resources relative to its executable; cwd controls
    output files and can therefore be a dedicated per-session directory.
    """
    if not isinstance(command, str) or command not in ALLOWED_COMMANDS:
        raise ValueError(tr('Разрешены только hw version, hf search и lf search.'))
    if offline and command != "hw version":
        raise ValueError(tr('Поиск карты требует подключения к устройству.'))
    return _build_invocation(settings, command, offline)


def build_operation_invocation(settings: DeviceSettings, operation: Operation) -> tuple[str, list[str], str, QProcessEnvironment]:
    """A separate typed boundary: arbitrary strings remain forbidden by run()."""
    if type(operation) is not Operation:
        raise ValueError(tr('Ожидалась типизированная операция чтения.'))
    if settings.transport == 'offline':
        from .client_catalog import COMMANDS
        command = operation.client_command
        permitted = command.startswith(('data ', 'analyse ', 'trace ', 'wiegand ')) or command in {'mad decode','mad encode','mad test','nfc encode','nfc decode','script list'}
        if operation.kind != 'expert_tool' or not permitted or not COMMANDS.get(command, {}).get('offline'):
            raise ValueError('Choose a supported offline analysis command.')
    invocation = _build_invocation(settings, operation.render(), False)
    if operation.kind == 'expert_tool':
        # Plot-oriented client actions stay offscreen; the workbench owns plots.
        invocation[3].insert('QT_QPA_PLATFORM', 'offscreen')
    if operation.kind == "antenna_sweep":
        # hw tune invokes its legacy plot window unconditionally on Windows.
        # Render it offscreen; the GUI plots the exported typed .pm3 artifact.
        # The matching client's libs/platforms must contain qoffscreen.dll.
        invocation[3].insert("QT_QPA_PLATFORM", "offscreen")
    if operation.kind == "mfc_recover":
        # autopwn derives filenames from RF UID. Require a fresh suffix in the
        # session so an old result cannot be mistaken for this recovery run.
        directory = Path(invocation[2])
        if any(directory.glob(f"hf-mf-*-key-{operation.output_suffix}.bin")) or any(directory.glob(f"hf-mf-*-dump-{operation.output_suffix}.*")):
            raise ValueError(tr('Файлы восстановления уже существуют. Создайте новую папку операции или суффикс.'))
    return invocation


def _build_invocation(settings: DeviceSettings, command: str, offline: bool) -> tuple[str, list[str], str, QProcessEnvironment]:
    _validate_transport(settings.transport)
    if not settings.client_path:
        raise ValueError(tr('Укажите путь к клиенту proxmark3.exe.'))
    executable = Path(settings.client_path).expanduser().resolve()
    if not executable.is_file():
        raise ValueError(tr('Файл клиента Proxmark3 не найден.'))
    cwd = Path(settings.working_dir).expanduser().resolve() if settings.working_dir else executable.parent
    if not cwd.is_dir():
        raise ValueError(tr('Рабочая папка клиента не существует.'))
    args: list[str] = []
    if not offline and settings.transport != 'offline':
        # Windows USB-CDC and Bluetooth SPP both use an explicitly selected COM.
        # SPP's virtual baud has no effect on the BlueShark's internal UART.
        # Reject options, shell delimiters and control characters at this boundary.
        port = settings.port
        if settings.transport == 'tcp':
            match = re.fullmatch(r'tcp:([A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:]+\]):([0-9]{1,5})', port)
            if not match or not 1 <= int(match[2]) <= 65535:
                raise ValueError('TCP endpoint must be tcp:host:port, with port 1–65535.')
        elif (not port or not port.upper().startswith("COM")
                or not port[3:].isascii() or not port[3:].isdigit()
                or int(port[3:]) < 1):
            raise ValueError(tr('Выберите COM-порт устройства, например COM3.'))
        args.extend(["-p", port])
    args.extend(["-f", "-c", command])

    return str(executable), args, str(cwd), _client_environment(executable)


def _client_environment(executable: Path) -> QProcessEnvironment:
    """Keep the native client's Qt/Python runtime separate from the GUI's."""
    executable = Path(executable)
    environment = QProcessEnvironment.systemEnvironment()
    libs = executable.parent / "libs"
    shell = libs / "shell"
    path_items = [str(executable.parent)]
    if libs.is_dir():
        path_items.append(str(libs))
    if shell.is_dir():
        path_items.append(str(shell))
        environment.insert("MSYSTEM", "MINGW64")
    python = executable.parent / 'python'
    if any(python.glob('lib/python3.*/encodings/__init__.py')):
        python_bin = python / 'bin'
        if python_bin.is_dir():
            path_items.append(str(python_bin))
        environment.insert('PYTHONHOME', str(python))
        environment.remove('VIRTUAL_ENV')
        environment.remove('PYTHONPATH')
        scripts = executable.parent / 'pyscripts'
        if scripts.is_dir():
            environment.insert('PYTHONPATH', str(scripts))
        environment.insert('PYTHONNOUSERSITE', '1')
        environment.insert('PYTHONDONTWRITEBYTECODE', '1')
    path_items.append(environment.value("PATH"))
    environment.insert("PATH", os.pathsep.join(path_items))
    # GUI Qt plugins must not leak into the native client's separate runtime.
    if libs.is_dir():
        environment.insert("QT_PLUGIN_PATH", str(libs))
        platforms = libs / "platforms"
        environment.insert("QT_QPA_PLATFORM_PLUGIN_PATH", str(platforms if platforms.is_dir() else libs))
    else:
        for key in ("QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
            environment.remove(key)
    return environment


class _StreamCleaner:
    """Remove ANSI CSI/OSC/string controls even when split between reads."""

    def __init__(self) -> None:
        self.state = "text"

    def feed(self, text: str) -> str:
        result: list[str] = []
        for char in text:
            if self.state == "text":
                if char == "\x1b":
                    self.state = "escape"
                elif char == "\x9b":
                    self.state = "csi"
                elif char in "\n\t" or (ord(char) >= 32 and not 127 <= ord(char) <= 159):
                    result.append(char)
            elif self.state == "escape":
                if char == "[":
                    self.state = "csi"
                elif char in "]P^_X":
                    self.state = "string"
                elif " " <= char <= "/":
                    self.state = "intermediate"
                else:
                    self.state = "text"
            elif self.state == "intermediate":
                if "0" <= char <= "~":
                    self.state = "text"
            elif self.state == "csi":
                if "@" <= char <= "~":
                    self.state = "text"
            elif self.state == "string":
                if char == "\x07":
                    self.state = "text"
                elif char == "\x1b":
                    self.state = "string_escape"
            elif self.state == "string_escape":
                self.state = "text" if char == "\\" else "string"
        return "".join(result)


@dataclass
class _Run:
    executable: str
    arguments: list[str]
    cwd: str
    environment: QProcessEnvironment
    cache_key: tuple[str, int, int]
    process: QProcess | None = None
    timeout: QTimer | None = None
    kill_timer: QTimer | None = None
    probing: bool = False
    probe: bytearray = field(default_factory=bytearray)
    decoder: object = field(default_factory=lambda: codecs.getincrementaldecoder("utf-8")("replace"))
    cleaner: _StreamCleaner = field(default_factory=_StreamCleaner)
    output_bytes: int = 0
    truncated: bool = False
    stop_reason: str = ""
    preserve_tail: bool = False
    bulk_cancel: bool = False
    tail_decoder: object = field(default_factory=lambda: codecs.getincrementaldecoder("utf-8")("replace"))
    tail_cleaner: _StreamCleaner = field(default_factory=_StreamCleaner)
    tail_text: str = ""
    guard_line_buffer: str = ""
    auth2_errors: int = 0
    last_nonce_count: int = 0


class ClientRunner(QObject):
    output = Signal(str)
    finished = Signal(int, str)
    busy_changed = Signal(bool)
    session_changed = Signal(bool, str)

    def __init__(self, parent: QObject | None = None, *, argument_prefix: Sequence[str] = ()) -> None:
        super().__init__(parent)
        # Allows launching a wrapper executable (and a real fake client in tests).
        # This is argv, never a shell command.
        self._argument_prefix = list(argument_prefix)
        self._active: _Run | None = None
        self._capabilities: dict[tuple[str, int, int], bool] = {}
        self._pipe_capabilities: dict[tuple[str, int, int], bool] = {}
        self._cancel_capabilities: dict[tuple[str, int, int], bool] = {}
        self._bulk_cancel_capabilities: dict[tuple[str, int, int], bool] = {}
        from .persistent import PersistentSession
        self._session = PersistentSession(self)
        self.rf_lease=False
        self._shutting_down = False

    def set_rf_lease(self,active):
        self.rf_lease=bool(active)
        if active:self._session.heartbeat.stop()
        elif self._session.connected and self._session.transport != 'offline' and not self.busy:self._session.heartbeat.start(20000)

    @property
    def busy(self) -> bool:
        return self._active is not None

    @property
    def session_open(self) -> bool:
        return self._session.connected and self._session.transport != 'offline'

    @property
    def connection_transport(self) -> str | None:
        return self._session.transport if self.session_open else None

    def disconnect(self) -> None:
        if self.busy:
            raise ValueError(tr('Сначала остановите текущую операцию.'))
        self._session.disconnect()

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        try:
            run = self._active
            if run is not None:
                if not run.stop_reason:
                    run.stop_reason = 'cancel'
                for timer in (run.timeout, run.kill_timer):
                    if timer is not None:
                        timer.stop()
            self._session.shutdown()
            # A one-shot client is not owned by PersistentSession. Wait for its
            # actual exit before releasing busy/COM ownership or emitting a
            # final result. Callbacks cannot start a replacement during cleanup.
            if run is not None and self._active is run:
                process = run.process
                if process is not None and process.state() != QProcess.ProcessState.NotRunning:
                    process.terminate()
                    if not process.waitForFinished(1500):
                        process.kill()
                        process.waitForFinished(1000)
                if self._active is run:
                    self._complete(run, *self._stop_result(run.stop_reason))
        finally:
            self._shutting_down = False

    def run(self, settings: DeviceSettings, command: str, *, offline: bool = False,
            timeout_ms: int = 45000) -> None:
        self._validate_start(timeout_ms)
        invocation = build_invocation(settings, command, offline)
        if settings.keep_open and not offline:
            self._session.begin(invocation, settings, timeout_ms, bulk_cancel=command == 'hf search')
        else:
            if self._session.has_process:
                raise ValueError(tr('Сначала отключите постоянный сеанс.'))
            self._begin(invocation, timeout_ms)

    def run_operation(self, settings: DeviceSettings, operation: Operation, *, timeout_ms: int | None = None) -> None:
        if type(operation) is not Operation:
            raise ValueError(tr('Ожидалась типизированная операция клиента.'))
        recovery = _operation_preserves_recovery_tail(operation)
        bulk_cancel = operation_supports_bulk_cancel(operation)
        maximum = OPERATION_TIMEOUT_MS.get(operation.kind, 300000)
        if timeout_ms is None:
            timeout_ms = operation.recommended_timeout_ms
        self._validate_start(timeout_ms, maximum)
        invocation = build_operation_invocation(settings, operation)
        if settings.keep_open:
            self._session.begin(invocation, settings, timeout_ms, preserve_tail=recovery,
                                bulk_cancel=bulk_cancel)
        else:
            if self._session.has_process:
                raise ValueError(tr('Сначала отключите постоянный сеанс.'))
            self._begin(invocation, timeout_ms, preserve_tail=recovery)

    def _validate_start(self, timeout_ms: int, maximum_ms: int = 300000) -> None:
        if self.busy or self._shutting_down:
            raise ValueError(tr('Дождитесь завершения текущей операции.'))
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 1 <= timeout_ms <= maximum_ms:
            raise ValueError(tr('Тайм-аут должен быть от 1 до {0} мс.', f'{maximum_ms}'))

    def _begin(self, invocation: tuple[str, list[str], str, QProcessEnvironment], timeout_ms: int,
               *, preserve_tail: bool = False) -> None:
        executable, arguments, cwd, environment = invocation
        stat = Path(executable).stat()
        key = (executable, stat.st_mtime_ns, stat.st_size)
        run = _Run(executable, arguments, cwd, environment, key, preserve_tail=preserve_tail)
        self._active = run
        run.timeout = QTimer(self)
        run.timeout.setSingleShot(True)
        run.timeout.timeout.connect(lambda: self._stop(run, "timeout"))
        run.timeout.start(timeout_ms)
        self.busy_changed.emit(True)
        if key in self._capabilities:
            self._start_command(run)
        else:
            run.probing = True
            self._start_process(run, ["--help"])

    def cancel(self) -> None:
        if self._active is not None:
            self._stop(self._active, "cancel")

    def _start_command(self, run: _Run) -> None:
        args = list(run.arguments)
        if self._capabilities.get(run.cache_key, False):
            args.insert(0, "--incognito")
        run.probing = False
        self._start_process(run, args)

    def _start_process(self, run: _Run, arguments: list[str]) -> None:
        if self._active is not run:
            return
        process = QProcess(self)
        run.process = process
        process.setProgram(run.executable)
        process.setArguments(self._argument_prefix + arguments)
        process.setWorkingDirectory(run.cwd)
        process.setProcessEnvironment(run.environment)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.readyReadStandardOutput.connect(lambda: self._read(run, process))
        process.finished.connect(lambda code, status: self._process_finished(run, process, code, status))
        process.errorOccurred.connect(lambda error: self._process_error(run, process, error))
        # QProcess starts Windows children without a separate console by default.
        # PySide6 does not expose setCreateProcessArgumentsModifier on all builds.
        try:
            with external_process_libraries():
                process.start()
        except OSError as exc:
            self._complete(run, -1, tr('Не удалось запустить клиент: ') + str(exc))

    def _read(self, run: _Run, process: QProcess) -> None:
        data = bytes(process.readAllStandardOutput())
        if self._active is not run or run.process is not process:
            return
        if run.probing:
            run.probe.extend(data[:max(0, _MAX_PROBE_BYTES - len(run.probe))])
            return
        self._consume_output(run, data)

    def _consume_output(self, run: _Run, data: bytes) -> None:
        if run.preserve_tail:
            text = run.tail_cleaner.feed(run.tail_decoder.decode(data))
            run.tail_text = (run.tail_text + text)[-RECOVERY_TAIL_CHARACTERS:]
            self._guard_recovery_output(run, text)
        remaining = max(0, MAX_OUTPUT_BYTES - run.output_bytes)
        accepted = data[:remaining]
        run.output_bytes += len(accepted)
        self._emit_text(run, run.decoder.decode(accepted))
        if len(data) > remaining and not run.truncated:
            run.truncated = True
            self.output.emit(tr('\n[Вывод сокращён: достигнут лимит 4 МиБ.]\n'))

    def _guard_recovery_output(self, run: _Run, text: str) -> None:
        """Bound an unproductive nonce loop without interrupting other commands.

        A one-byte Auth2 response is shorter than the expected encrypted nonce;
        the client does not log that byte, so it does not prove an ACL or card
        capacity diagnosis. Only this recovery run is stopped. Actual nonce
        growth or an explicit key result resets the consecutive-error count.
        """
        if run.stop_reason:
            return
        lines = (run.guard_line_buffer + text).split("\n")
        run.guard_line_buffer = lines.pop()[-2048:]
        for line in lines:
            progress = _NONCE_PROGRESS.match(line)
            if _KEY_PROGRESS.search(line):
                run.auth2_errors = 0
            elif progress and int(progress.group(1)) > run.last_nonce_count:
                run.last_nonce_count = int(progress.group(1))
                run.auth2_errors = 0
            elif _AUTH2_ERROR.search(line):
                run.auth2_errors += 1
                if run.auth2_errors >= MAX_REPEATED_AUTH2_ERRORS:
                    self.output.emit(tr('\n[!] Повторяется Auth2 error len=1 без новых nonce. Запрошена остановка текущей операции; затем проверим отклик платы. Проверьте доступность целевого сектора и профиль карты.\n'))
                    self._stop(run, "repeated_auth2")
                    return

    def _emit_text(self, run: _Run, text: str) -> None:
        cleaned = run.cleaner.feed(text)
        if cleaned:
            self.output.emit(cleaned)

    def _process_error(self, run: _Run, process: QProcess, error: QProcess.ProcessError) -> None:
        if self._active is run and run.process is process and error == QProcess.ProcessError.FailedToStart:
            self._complete(run, -1, tr('Не удалось запустить клиент: ') + process.errorString())

    def _process_finished(self, run: _Run, process: QProcess, code: int,
                          status: QProcess.ExitStatus) -> None:
        if self._active is not run or run.process is not process:
            process.deleteLater()
            return
        self._read(run, process)
        if run.stop_reason:
            self._complete(run, *self._stop_result(run.stop_reason))
            return
        if status == QProcess.ExitStatus.CrashExit:
            self._complete(run, code or -1, tr('Клиент аварийно завершился.'))
            return
        if run.probing:
            if code:
                self._complete(run, code, tr('Проверка клиента завершилась с кодом {0}.', f'{code}'))
                return
            self._capabilities[run.cache_key] = b"--incognito" in run.probe
            process.deleteLater()
            self._start_command(run)
            return
        if code:
            message = tr('Клиент завершился с кодом {0}; проверьте журнал.', f'{code}')
        else:
            message = tr('Клиент завершил команду; результат поиска смотрите в журнале.')
        self._complete(run, code, message)

    def _stop(self, run: _Run, reason: str) -> None:
        if self._active is not run or run.stop_reason:
            return
        if self._session.request_cancel(run, reason):
            return
        run.stop_reason = reason
        process = run.process
        if process is None or process.state() == QProcess.ProcessState.NotRunning:
            self._complete(run, *self._stop_result(reason))
            return
        process.terminate()
        timer = QTimer(self)
        run.kill_timer = timer
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._kill(run, process))
        timer.start(_KILL_DELAY_MS)

    @staticmethod
    def _stop_result(reason: str) -> tuple[int, str]:
        return {
            "cancel": (-2, tr('Операция отменена.')),
            "timeout": (-3, tr('Превышено время ожидания клиента.')),
            "repeated_auth2": (-4, tr('Клиент остановлен: повторные короткие ответы Auth2 без продвижения; освобождение устройства не подтверждено.')),
        }[reason]

    def _kill(self, run: _Run, process: QProcess) -> None:
        if self._active is run and run.process is process and process.state() != QProcess.ProcessState.NotRunning:
            process.kill()

    def _complete(self, run: _Run, code: int, message: str) -> None:
        if self._active is not run:
            return
        for timer in (run.timeout, run.kill_timer):
            if timer is not None:
                timer.stop()
                timer.deleteLater()
        if not run.probing:
            self._emit_text(run, run.decoder.decode(b"", final=True))
            if run.preserve_tail:
                final = run.tail_cleaner.feed(run.tail_decoder.decode(b"", final=True))
                run.tail_text = (run.tail_text + final)[-RECOVERY_TAIL_CHARACTERS:]
                if run.truncated and run.tail_text:
                    self.output.emit(tr('\n[=] Последний фрагмент восстановления (ограничен 256 КиБ текста):\n'))
                    self.output.emit(run.tail_text)
        if run.process is not None:
            run.process.deleteLater()
        self._active = None
        self.busy_changed.emit(False)
        self.finished.emit(code, message)
