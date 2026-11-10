"""Framed stdin session for our client-only --gui-pipe extension.

One client owns the port. Only validated catalog commands reach this class.
EOF closes the session through Iceman's normal cleanup. A killed/failed RF
command is never replayed. Idle checks verify the link without energising RF.
"""
from __future__ import annotations
from .i18n import tr

from pathlib import Path
import re
from uuid import uuid4

from PySide6.QtCore import QObject, QProcess, QTimer

from .device import _Run, _MAX_PROBE_BYTES, parse_connection_status
from .connection_checks import version_probe_status
from .process_env import external_process_libraries

HEARTBEAT_INTERVAL_MS = 20000
HEARTBEAT_TIMEOUT_MS = 8000
CANCEL_TIMEOUT_MS = 8000
BLUESHARK_OPEN_RETRY_DELAY_MS = 1500
BLUESHARK_OPEN_RETRY_LIMIT = 2
MAX_FRAME_FIELD_BYTES = 8192
_END_CODE = re.compile(rb"-?\d{1,10}\Z")
_BLUESHARK_OPEN_UNAVAILABLE = re.compile(
    rb"\bUART[ \t]+CreateFile[ \t]+failed\b[^\r\n]*\(Win32[ \t]+error[ \t]+(?P<code>1168|121)\)", re.I)


def _help_has_option(probe, option):
    return bool(re.search(rb'(?<![\w-])' + re.escape(option) + rb'(?![\w-])', probe))


class PersistentSession(QObject):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.process = None
        self.connected = False
        self.idle_loss = False
        self.transport = None
        self.signature = None
        self.token = ''
        self.phase = 'closed'
        self.buffer = b''
        self.fragment = False
        self.startup = bytearray()
        self.frame = None
        self.frame_output = bytearray()
        self.heartbeat_recheck_used = False
        self.pending = None
        self.user_commands = []
        self.cancel_run = None
        self.open_retry_count = 0
        self.open_retry_run = None
        self.ready_seen = False
        self.open_retry_timer = QTimer(self)
        self.open_retry_timer.setSingleShot(True)
        self.open_retry_timer.timeout.connect(self._retry_open)
        self.cancel_timer = QTimer(self)
        self.cancel_timer.setSingleShot(True)
        self.cancel_timer.timeout.connect(self._cancel_expired)
        self.heartbeat = QTimer(self)
        self.heartbeat.setSingleShot(True)
        self.heartbeat.timeout.connect(self._ping)
        self.ping_timeout = QTimer(self)
        self.ping_timeout.setSingleShot(True)
        self.ping_timeout.timeout.connect(lambda: self._lost(tr('Плата не ответила на проверку связи.')))
        self.close_timer = QTimer(self)
        self.close_timer.setSingleShot(True)
        self.close_timer.timeout.connect(self._kill_closing)

    @property
    def has_process(self):
        return self.process is not None

    def begin(self, invocation, settings, timeout_ms, *, preserve_tail=False, bulk_cancel=False):
        executable, arguments, cwd, environment = invocation
        command = arguments[arguments.index('-c') + 1]
        # Validate the complete wire request before reserving/starting anything.
        self._wire_field(command)
        self._wire_field(cwd)
        stat = Path(executable).stat()
        key = (executable, stat.st_mtime_ns, stat.st_size)
        signature = (*key, settings.port.upper(), settings.transport)
        run = _Run(executable, arguments, cwd, environment, key,
                   preserve_tail=preserve_tail, bulk_cancel=bulk_cancel)
        run.completion_token = self.owner.completion_token
        run.require_session = settings.require_session
        self.owner._active = run
        run.timeout = QTimer(self.owner)
        run.timeout.setSingleShot(True)
        run.timeout.timeout.connect(lambda: self.owner._stop(run, 'timeout'))
        run.timeout.start(timeout_ms)
        self.owner.busy_changed.emit(True)
        # Qt slots run synchronously: a listener may cancel/shut down as soon
        # as busy is announced. Do not submit its command after that result.
        if self.owner._active is not run or run.stop_reason:
            return
        self.open_retry_timer.stop()
        self.open_retry_run = None
        self.open_retry_count = 0
        self.pending = (run, command, settings.port, settings.transport, signature)
        # Operation.render alone may produce a fixed command sequence (tune +
        # save). The typed boundary forbids semicolons inside user paths.
        self.user_commands = [part.strip() for part in command.split(';') if part.strip()]
        self.heartbeat.stop()
        if self.process is not None:
            run.process = self.process
            if self.signature == signature and self.connected and self.phase == 'idle':
                self._send_user()
            elif self.signature == signature and self.frame and self.frame[1] == 'ping':
                pass  # Finish the already running idle ping, then send this command.
            else:
                self._close(tr('Меняются настройки подключения.'), restart=True)
        else:
            self._start_pending()

    @staticmethod
    def _wire_field(value):
        if not isinstance(value, str) or any(c in value for c in '\0\r\n'):
            raise ValueError(tr('Недопустимое поле команды постоянного сеанса.'))
        raw = value.encode('utf-8')
        if not 1 <= len(raw) <= MAX_FRAME_FIELD_BYTES:
            raise ValueError(tr('Команда или путь постоянного сеанса слишком длинные.'))
        return raw.hex()

    def _start_pending(self):
        if self.pending is None:
            return
        run, _, port, transport, signature = self.pending
        if self.owner._active is not run or run.stop_reason:
            return
        self.signature = signature
        self.transport = transport
        self.buffer, self.fragment = b'', False
        self.startup.clear()
        self.frame = None
        self.ready_seen = False
        self.token = uuid4().hex
        if run.cache_key not in self.owner._pipe_capabilities:
            self.phase = 'probe'
            run.probing = True
            self._spawn(run, ['--help'])
        elif not self.owner._pipe_capabilities[run.cache_key]:
            self._fallback(run)
        else:
            self.phase = 'starting'
            run.probing = False
            run.environment.insert('QT_QPA_PLATFORM', 'offscreen')
            args = (['-p', port] if transport != 'offline' else []) + ['-f', '--gui-pipe', self.token]
            if self.owner._capabilities.get(run.cache_key):
                args.insert(0, '--incognito')
            self._spawn(run, args)

    def _spawn(self, run, arguments):
        if self.owner._active is not run or run.stop_reason:
            return
        process = QProcess(self.owner)
        self.process = run.process = process
        process.setProgram(run.executable)
        process.setArguments(self.owner._argument_prefix + arguments)
        process.setWorkingDirectory(run.cwd)
        process.setProcessEnvironment(run.environment)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.readyReadStandardOutput.connect(lambda: self._read(process))
        process.finished.connect(lambda code, status: self._ended(process, code, status))
        process.errorOccurred.connect(lambda error: self._error(process, error))
        try:
            with external_process_libraries():
                process.start()
        except OSError as exc:
            self.process = None
            self._lost(tr('Не удалось запустить клиент: ') + str(exc))

    def _fallback(self, run):
        self.pending = None
        self.phase = 'closed'
        if getattr(run, 'require_session', False):
            self.owner._complete(run, -5, 'This transaction requires the matching --gui-pipe client to preserve buffers between steps.')
            return
        self.owner.session_changed.emit(False, tr('Клиент не поддерживает постоянный сеанс; выполняется отдельная команда.'))
        self.owner._start_command(run)

    def _read(self, process):
        data = bytes(process.readAllStandardOutput())
        if self.process is not process:
            return
        run = self.owner._active
        if self.phase == 'probe':
            if run:
                run.probe.extend(data[:max(0, _MAX_PROBE_BYTES - len(run.probe))])
            return
        self.buffer += data.replace(b'\r', b'')
        while b'\n' in self.buffer:
            line, self.buffer = self.buffer.split(b'\n', 1)
            self._line(line, marker_allowed=not self.fragment)
            self.fragment = False
        # A client's unbounded line must not bypass the existing output limit.
        if len(self.buffer) > 16384:
            self._ordinary(self.buffer[:-1024])
            self.buffer = self.buffer[-1024:]
            self.fragment = True

    def _line(self, line, *, marker_allowed=True):
        prefix = f'[PM3GUI {self.token} '.encode()
        if marker_allowed and line == prefix + b'READY]':
            self.ready_seen = True
            if self.phase != 'starting' or self.pending is None:
                self._lost(tr('Нарушен порядок запуска клиентского сеанса.'))
                return
            if self.transport == 'offline':
                self.connected = True
                self.phase = 'idle'
                self._send_user()
            else:self._send('hw version', 'bootstrap', self.pending[0].cwd)
            return
        if marker_allowed and self.frame:
            frame_id, kind, begun = self.frame
            frame_prefix = prefix + frame_id.encode() + b' '
            if line == frame_prefix + b'BEGIN]':
                if begun:
                    self._lost(tr('Клиент повторил начало команды.'))
                else:
                    self.frame = (frame_id, kind, True)
                return
            if line.startswith(frame_prefix + b'END ') and line.endswith(b']'):
                raw_code = line[len(frame_prefix) + 4:-1]
                if not begun or not _END_CODE.fullmatch(raw_code):
                    self._lost(tr('Клиент не подтвердил завершение команды.'))
                    return
                self._frame_done(kind, int(raw_code))
                return
        self._ordinary(line + b'\n')

    def _ordinary(self, data):
        if self.phase == 'starting' or (self.frame and self.frame[1] == 'bootstrap'):
            self.startup.extend(data[:max(0, 256 * 1024 - len(self.startup))])
        if self.frame and self.frame[1] == 'ping':
            self.frame_output.extend(data[:max(0, 65536 - len(self.frame_output))])
            return
        run = self.owner._active
        if run and not run.probing:
            self.owner._consume_output(run, data)

    def _send(self, command, kind, cwd):
        if kind in {'bootstrap', 'user'}:
            run = self.owner._active
            if not self.pending or self.pending[0] is not run or run.stop_reason:
                return
        if self.process is None:
            self._lost(tr('Клиентский сеанс закрыт.'))
            return
        frame_id = uuid4().hex
        wire = f'{frame_id} {self._wire_field(cwd)} {self._wire_field(command)}\n'.encode('ascii')
        self.frame = (frame_id, kind, False)
        self.frame_output.clear()
        self.phase = 'command'
        if self.process.write(wire) != len(wire):
            self._lost(tr('Не удалось передать команду клиенту.'))

    def _send_user(self):
        if self.pending:
            run, *_ = self.pending
            if self.owner._active is not run or run.stop_reason or not self.user_commands:
                return
            run.process = self.process
            self._send(self.user_commands.pop(0), 'user', run.cwd)

    def _frame_done(self, kind, code):
        self.frame = None
        self.phase = 'idle'
        run = self.owner._active
        if run and run.stop_reason:
            if self.cancel_run is run and kind == 'user':
                self.cancel_timer.stop()
                self.cancel_run = None
                self.pending = None
                self.user_commands = []
                self.heartbeat.start(HEARTBEAT_INTERVAL_MS)
                run.process = None
                self.owner.output.emit(tr('\n[=] Клиент завершил отменённую операцию; COM-порт остаётся открыт. Проверка платы выполняется перед продолжением.\n'))
                result_code, _ = self.owner._stop_result(run.stop_reason)
                self.owner._complete(run, result_code, tr('Операция прервана; клиентский сеанс сохранён.'))
            return  # A hard stop still completes only after the child exits.
        if kind == 'bootstrap':
            result = parse_connection_status(self.startup.decode('utf-8', 'replace'), self.transport, code)
            if not result['connected']:
                self._lost(result['message'])
                return
            self.connected = True
            endpoint = self.signature[-2]
            name = 'TCP · ' + endpoint if self.transport == 'tcp' else ('Blue Shark · ' if self.transport == 'blueshark' else 'USB · ') + endpoint
            self.owner.session_changed.emit(True, tr('Постоянное соединение открыто: ') + name + '.')
            if self.owner._active is not run or run.stop_reason:
                return
            if self.pending and self.pending[1] == 'hw version':
                self._finish_user(code)
            else:
                self._send_user()
        elif kind == 'ping':
            output = self.frame_output.decode('utf-8', 'replace')
            if self.transport == 'blueshark':
                # A real idle Blue Shark can miss its first VERSION request,
                # then answer a second in the same process. Only that read-only
                # liveness check may be sent once more. The original 8-second
                # timer keeps running; user/RF commands still await fresh OS.
                result = version_probe_status(output, 'blueshark', code, verified_transport=self.transport)
                acknowledged = result['connected']
                if (result['read_only_recheck_allowed']
                        and not self.heartbeat_recheck_used
                        and self.ping_timeout.isActive() and self.ping_timeout.remainingTime() > 0):
                    self.heartbeat_recheck_used = True
                    self._send('hw version', 'ping', self.process.workingDirectory())
                    return
            else:
                # USB/TCP retain the independent payload-echo check.
                acknowledged = (code == 0 and 'Ping response received' in output
                                and re.search(r'content\s*\(\s*ok\s*\)', output, re.I))
            self.ping_timeout.stop()
            if not acknowledged:
                self._lost(tr('Потеряно соединение: плата не подтвердила контрольный запрос.'))
            elif self.pending:
                self._send_user()
            else:
                self.heartbeat.start(HEARTBEAT_INTERVAL_MS)
        else:
            if code == 0 and self.user_commands:
                self._send_user()
            else:
                self._finish_user(code)

    def _finish_user(self, code):
        run = self.owner._active
        self.pending = None
        self.user_commands = []
        if self.transport != 'offline':self.heartbeat.start(HEARTBEAT_INTERVAL_MS)
        if run:
            run.process = None  # The session, rather than this command, owns it.
            message = tr('Команда завершена; соединение сохранено.') if code == 0 else tr('Команда завершилась с кодом {0}; соединение сохранено.', f'{code}')
            self.owner._complete(run, code, message)

    def _ping(self):
        if self.transport == 'offline':return
        if self.owner.rf_lease:return
        if self.connected and self.owner._active is None and self.phase == 'idle':
            self.heartbeat_recheck_used = False
            command = 'hw version' if self.transport == 'blueshark' else 'hw ping --len 32'
            self._send(command, 'ping', self.process.workingDirectory())
            self.ping_timeout.start(HEARTBEAT_TIMEOUT_MS)

    def request_cancel(self, run, reason):
        """Cancel this one RF acquisition, without queuing another RF command.

        The native client consumes this control in its response wait and sends
        CMD_BREAK_LOOP. Only its matching END may release the command. Older
        clients/startup/unsupported operations retain the existing hard stop.
        Bulk acquisition has its own capability: the older cancellation
        extension only covers recovery waits, not BigBuf downloads.
        """
        if self.open_retry_run is run:
            # No process or frame exists during backoff. Let ClientRunner
            # finish the cancellation, and prevent the delayed open entirely.
            self.open_retry_timer.stop()
            self.open_retry_run = None
            self.pending = None
            self.user_commands = []
            self.phase = 'closed'
            return False
        capable = ((run.preserve_tail and self.owner._cancel_capabilities.get(run.cache_key, False))
                   or (run.bulk_cancel and self.owner._bulk_cancel_capabilities.get(run.cache_key, False)))
        if (self.process is not run.process or not self.connected or not self.frame
                or self.frame[1:] != ('user', True) or not capable):
            return False
        run.stop_reason = reason
        self.cancel_run = run
        if run.timeout:
            run.timeout.stop()
        wire = f'!CANCEL {self.token} {self.frame[0]}\n'.encode('ascii')
        if self.process.write(wire) != len(wire):
            self._cancel_expired()
        else:
            self.cancel_timer.start(CANCEL_TIMEOUT_MS)
        return True

    def _cancel_expired(self):
        if self.cancel_run is None:
            return
        run = self.cancel_run
        self.cancel_run = None
        message = tr('Клиент не подтвердил отмену за 8 секунд; соединение закрыто принудительно.')
        run.session_error = message
        self.owner.output.emit('\n[!] ' + message + '\n')
        self._set_closed(message)
        if self.process is not None:
            self.process.kill()

    def _lost(self, message):
        self.idle_loss = (self.connected and self.owner._active is None and not self.owner.rf_lease
                          and not self.owner._shutting_down and self.transport != 'offline')
        self._set_closed(message)
        self.pending = None
        self.frame = None
        self.phase = 'lost'
        run = self.owner._active
        if run:
            run.session_error = message
        if self.process is not None:
            self.process.kill()
        elif run:
            self.owner._complete(run, -5, message)
        elif self.idle_loss:
            self.idle_loss=False;self.owner.idle_link_lost.emit(message)

    def _set_closed(self, message):
        self.connected = False
        self.heartbeat.stop()
        self.ping_timeout.stop()
        self.cancel_timer.stop()
        self.cancel_run = None
        self.open_retry_timer.stop()
        self.open_retry_run = None
        self.owner.session_changed.emit(False, message)

    def disconnect(self):
        self.idle_loss=False
        self.pending = None
        self._close(tr('Постоянное соединение отключено.'))

    def _close(self, message, *, restart=False):
        self._set_closed(message)
        self.frame = None
        self.phase = 'restart' if restart else 'closing'
        if self.process is not None:
            self.process.closeWriteChannel()
            self.close_timer.start(3000)
        elif restart:
            self._start_pending()

    def _kill_closing(self):
        if self.process is not None:
            self.process.kill()

    def _error(self, process, error):
        if self.process is process and error == QProcess.ProcessError.FailedToStart:
            self._ended(process, -1, QProcess.ExitStatus.CrashExit)

    def _can_retry_open(self, phase, run):
        # Windows SPP can briefly return ERROR_NOT_FOUND after the previous
        # handle closed or ERROR_SEM_TIMEOUT while the radio reconnects.
        # Only retry these exact port-open failures before READY: no
        # bootstrap/user command has been sent, so no RF operation is replayed.
        return (phase == 'starting' and not self.ready_seen and self.frame is None
                and self.transport == 'blueshark'
                and self.pending is not None and self.pending[0] is run
                and run is not None and not run.stop_reason
                and self.owner._active is run and not self.owner._shutting_down
                and self.open_retry_count < BLUESHARK_OPEN_RETRY_LIMIT
                and _BLUESHARK_OPEN_UNAVAILABLE.search(bytes(self.startup) + self.buffer))

    def _retry_open(self):
        run = self.open_retry_run
        self.open_retry_run = None
        if (run is not None and self.phase == 'retry_wait'
                and self.pending is not None and self.pending[0] is run
                and self.owner._active is run and not run.stop_reason
                and not self.owner._shutting_down and self.process is None):
            self._start_pending()

    def _ended(self, process, code, status):
        if self.process is not process:
            return
        self._read(process)
        phase = self.phase
        idle_loss = self.idle_loss or (phase=='idle' and self.connected and self.owner._active is None
                                      and not self.owner.rf_lease and self.transport!='offline')
        self.idle_loss=False
        self.process = None
        self.close_timer.stop()
        process.close()
        process.deleteLater()
        run = self.owner._active
        open_error = self._can_retry_open(phase, run)
        if open_error:
            self.open_retry_count += 1
            run.process = None
            self.open_retry_run = run
            self.phase = 'retry_wait'
            self.open_retry_timer.start(BLUESHARK_OPEN_RETRY_DELAY_MS)
            message = tr('Порт Blue Shark временно недоступен (Win32 {0}). Повторное открытие {1}/{2}…',
                         open_error.group('code').decode('ascii'),
                         str(self.open_retry_count), str(BLUESHARK_OPEN_RETRY_LIMIT))
            self.owner.output.emit('\n[=] ' + message + '\n')
            self.owner.session_changed.emit(False, message)
            return
        if phase == 'probe' and run and not run.stop_reason and code == 0:
            self.owner._capabilities[run.cache_key] = _help_has_option(run.probe, b'--incognito')
            self.owner._pipe_capabilities[run.cache_key] = _help_has_option(run.probe, b'--gui-pipe')
            self.owner._cancel_capabilities[run.cache_key] = _help_has_option(run.probe, b'--gui-pipe-cancel')
            self.owner._bulk_cancel_capabilities[run.cache_key] = _help_has_option(run.probe, b'--gui-pipe-bulk-cancel')
            run.process = None
            self._start_pending()
            return
        if phase == 'restart' and run and not run.stop_reason:
            run.process = None
            self._start_pending()
            return
        self.pending = None
        self.frame = None
        message = getattr(run, 'session_error', '') if run else ''
        message = message or (tr('Соединение отключено.') if phase == 'closing' else tr('Соединение потеряно: клиент завершился. Подключитесь снова.'))
        self._set_closed(message)
        self.phase = 'closed'
        if run:
            run.process = None
            if run.stop_reason:
                self.owner._complete(run, *self.owner._stop_result(run.stop_reason))
            else:
                self.owner._complete(run, -5, message)
        elif idle_loss and not self.owner._shutting_down:
            self.owner.idle_link_lost.emit(message)

    def shutdown(self):
        self.idle_loss=False
        self.pending = None
        self.frame = None
        self.close_timer.stop()
        self._set_closed(tr('Соединение закрыто.'))
        self.phase = 'closing'
        process = self.process
        if process is not None:
            process.closeWriteChannel()
            if not process.waitForFinished(1500):
                process.kill()
                process.waitForFinished(1000)
