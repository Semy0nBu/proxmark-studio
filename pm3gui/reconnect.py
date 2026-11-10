"""Bounded idle reconnection. Only fresh version probes may be dispatched."""
from copy import deepcopy
import time
from PySide6.QtCore import QObject, QTimer
from .connection_checks import version_probe_status
from .studio_data import say


class IdleReconnect(QObject):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner=owner
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self._dispatch)
        self.generation=0;self.profile=None;self.attempts=0;self.probing=False
        self.deadline=0;self.identity=None;self.token=None
        self.clock=time.monotonic

    def cancel(self):
        self.timer.stop();self.generation+=1;self.profile=None;self.probing=False;self.token=None

    def lost(self, message=''):
        if not self._available():return
        self.cancel()
        self.profile=deepcopy(self.owner._device_profile())
        self.identity=deepcopy(getattr(self.owner,'_connected_usb_identity',None))
        self.attempts=0;self.deadline=self.clock()+45
        self._schedule()

    def _available(self):
        return (not getattr(self.owner,'_closing',False) and self.owner.keep_connection.isChecked()
                and not self.owner.runner.busy and self.owner.read_job is None
                and self.owner.antenna_job is None and not self.owner.firmware_busy
                and not self.owner.runner.rf_lease)

    def _schedule(self):
        if self.profile is None or not self._available() or self.owner._device_profile()!=self.profile:
            self.cancel();return
        if self.attempts>=3 or self.clock()+20>self.deadline:
            self.cancel()
            self.owner.connection_status.setText(say('Связь не восстановлена. Проверьте питание и порт, затем нажмите «Подключить».',
                'Connection was not restored. Check power and the port, then select Connect.'))
            return
        delay=(1000,2000,4000)[self.attempts]
        self.timer.start(delay)
        self.owner.connection_status.setText(say('Связь потеряна. Проверка подключения {0}/3…',
            'Connection lost. Reconnection check {0}/3…').format(self.attempts+1))

    def _dispatch(self):
        if (self.profile is None or not self._available() or self.owner._device_profile()!=self.profile
                or self.clock()+20>self.deadline):
            self.cancel();return
        # A changed USB COM is followed only with a previously observed serial.
        if self.profile['transport']=='usb' and self.identity:
            resolved=self.owner._resolve_reconnect_usb(self.profile,self.identity)
            if resolved is None:
                self.attempts+=1;self._schedule();return
            self.profile=resolved
        self.attempts+=1;self.probing=True
        try:
            self.owner.current_session=self.owner._new_session()
            self.owner._start_step('hw version',20000,_reconnect=True)
            self.token=self.owner._step_token
        except (OSError,ValueError) as exc:
            self.completed('',-5)

    def completed(self, output, code):
        if not self.probing or self.profile is None:return
        self.probing=False;self.token=None
        if self.owner._device_profile()!=self.profile:
            self.cancel();return
        result=version_probe_status(output,self.profile['transport'],code,
                                    verified_transport=self.owner.runner.connection_transport)
        if result['connected']:
            self.cancel()
            self.owner.connection_status.setText(result['message'])
            self.owner.device_info.setText(say('Соединение восстановлено. Операции не повторялись.',
                                              'Connection restored. No operations were replayed.'))
        else:self._schedule()
