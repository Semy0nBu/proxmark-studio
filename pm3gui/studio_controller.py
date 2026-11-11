"""Shared asynchronous lifecycle for the native laboratory workbench."""
from __future__ import annotations
import json
import hashlib
import time
from pathlib import Path
from dataclasses import asdict, is_dataclass
from PySide6.QtCore import QTimer
from .operations import Operation
from .device import operation_supports_bulk_cancel
from .connection_checks import (version_probe_status, record_cleanup_version_check,
                                schedule_cleanup_version_check, stop_cleanup_version_checks)
from .studio_data import say


def _reportable(value):
    from .core import DumpData
    if isinstance(value, DumpData):
        return {'bytes': len(value.data), 'sha256': hashlib.sha256(value.data).hexdigest(),
                'format': value.format, 'block_size': value.block_size, 'metadata': _reportable(value.metadata)}
    if isinstance(value, bytes):return {'bytes': len(value), 'hex': value.hex() if len(value) <= 128 else None}
    if is_dataclass(value):return _reportable(asdict(value))
    if isinstance(value, dict):return {str(k): _reportable(v) for k, v in value.items() if k not in {'samples', 'data'}}
    if isinstance(value, (list, tuple)):return [_reportable(v) for v in value]
    if isinstance(value, Path):return str(value)
    return value


class StudioControllerMixin:
    def open_classic_workbench(self):
        from .writing import source_family
        if self.loaded_dump is not None and source_family(self.loaded_dump) == 'mfc':
            self.studio.guard(self.studio.from_editor)
        self.navigate(5);self.studio.sections.setCurrentRow(0);self.studio.classic_tabs.setCurrentIndex(0)

    def open_classic_attack(self, method='Nested', from_snapshot=False):
        from .classic_ui import ClassicAttackDialog
        previous = getattr(self, 'classic_attack_dialog', None)
        if previous is not None and previous.owned():previous.show();previous.raise_();return previous
        if previous is not None:previous.close();previous.deleteLater()
        detected = getattr(self, 'detected', None)
        uid, size, dump = '', '1k', None
        if detected is not None and detected.family == 'mfc':uid, size = detected.uid, detected.classic_size or '1k'
        if from_snapshot and self.studio.dump is not None:
            uid, size, dump = self.studio.record.get('uid', ''), self.studio.classic_size, self.studio.dump
        self.classic_attack_dialog = dialog = ClassicAttackDialog(self, method, uid=uid, size=size, dump=dump)
        dialog.show();return dialog

    def open_family_wizard(self, profile='', source=None, operation=None, source_record=None):
        from .family_wizard import FamilyWizard
        previous=getattr(self,'family_wizard',None)
        if previous is not None and previous.owned():previous.show();previous.raise_();return previous
        if previous is not None:previous.close();previous.deleteLater()
        self.family_wizard=dialog=FamilyWizard(self,profile,source,source_record=source_record)
        if operation is not None:dialog.operation.setCurrentIndex(dialog.operation.findData(operation))
        dialog.show();return dialog

    def _build_studio_page(self):
        from .studio_ui import StudioPage
        self.studio = StudioPage(self)
        return self.studio

    def start_studio(self, flow, title, *, rf=False, on_done=None, hold=False, duration_ms=None, offline=False, on_review=None, on_finished=None):
        started = False
        try:
            self._lab_available()
            self.current_session = self._new_session()
            iterator = iter(flow(self.current_session))
            profile = dict(self._device_profile())
            if offline:
                if rf or hold:raise ValueError('Offline tasks cannot reserve RF.')
                profile.update(transport='offline', port='')
            # Trace/GraphBuffer live in the host process, not just on the board.
            # All steps of a laboratory transaction must share that process.
            profile['keep_open'] = True
            self.read_job = {'job_kind': 'studio', 'stage': 'running', 'session': self.current_session,
                             'flow': iterator, 'title': title, 'rf': rf, 'hold': hold,
                             'on_done': on_done, 'on_review': on_review, 'on_finished': on_finished, 'events': [], 'has_sent': False, 'result': None,
                             'may_have_written': False, 'cancelled': False, 'device_profile': profile}
            started = True
            if not hasattr(self, 'studio_deadline'):
                self.studio_deadline = QTimer(self);self.studio_deadline.setSingleShot(True)
                self.studio_deadline.timeout.connect(self._studio_timeout)
            self.studio_deadline.start(1800000)
            if rf:self.runner.set_rf_lease(True)
            if duration_ms is not None:
                if type(duration_ms) is not int or not 1000 <= duration_ms <= 1200000:raise ValueError('Invalid RF duration.')
                if not hasattr(self, 'studio_timer'):
                    self.studio_timer = QTimer(self);self.studio_timer.setSingleShot(True)
                    self.studio_timer.timeout.connect(lambda:self._cancel_studio() if self.read_job and self.read_job.get('job_kind') == 'studio' else None)
                self.studio_timer.start(duration_ms)
            self.studio.status.setText(title)
            self._studio_next()
        except (ValueError, OSError, KeyError, TypeError) as exc:
            if started and self.read_job and self.read_job.get('job_kind') == 'studio':self._studio_failed(exc)
            else:self.studio.status.setText(str(exc))

    def _studio_report(self, status):
        job = self.read_job
        report = {k: _reportable(job[k]) for k in ('title', 'events', 'may_have_written', 'cancelled', 'result')}
        report.update(status=status, error=job.get('error', ''), physical_verification=job.get('result', {}).get('verification', '') if isinstance(job.get('result'), dict) else '')
        report['manual_power_reset_acknowledged'] = bool(job.get('manual_power_reset'))
        if job.get('review') is not None:report['review'] = _reportable(job['review'])
        report['review_approved'] = bool(job.get('review_approved'))
        if job.get('cleanup_version_checks'):report['cleanup_version_checks'] = job['cleanup_version_checks']
        temporary = job['session']/'studio-report.tmp'
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        temporary.replace(job['session']/'studio-report.json')

    def _studio_timeout(self):
        if self.read_job and self.read_job.get('job_kind') == 'studio':
            self.read_job['error'] = say('Истёк общий срок операции (30 минут).', 'The overall 30-minute operation deadline expired.')
            self._cancel_studio()

    def _studio_next(self, response=None):
        job = self.read_job
        if job.get('cancelled'):
            self._studio_cleanup();return
        try:
            request = next(job['flow']) if response is None else job['flow'].send(response)
        except StopIteration as done:
            job['result'] = done.value or {}
            if job.get('on_done'):job['on_done'](job['result'])
            if job.get('hold'):
                job['stage'] = 'holding'
                self._studio_report('running-on-device')
                self.studio.status.setText(say('Режим платы запущен. Для завершения нажмите «Остановить».', 'Device mode started. Use Stop to finish.'))
                self._busy_changed(False)
            else:self._studio_cleanup()
            return
        from .wizard_plans import ReviewGate
        if isinstance(request, ReviewGate):
            if not job.get('on_review'):raise ValueError('This workflow requires a review-capable wizard.')
            job['stage'] = 'review';job['review'] = request
            self._studio_report('awaiting-approval')
            job['on_review'](request)
            self.studio.status.setText(say('Копия сохранена. Проверьте план в мастере.', 'Backup saved. Review the plan in the wizard.'))
            self._busy_changed(False)
            return
        job['request'] = request
        operation = request.operation()
        # BigBuf replies have relative offsets but no request IDs. A failed
        # non-RF console download needs a fresh device reply before another
        # request may use the same persistent transport.
        job['bulk_cancel_operation'] = operation_supports_bulk_cancel(operation)
        job['may_have_written'] |= request.modifying
        job['events'].append({'command': request.line, 'state': 'dispatched', 'mutating': request.modifying})
        self._studio_report('running')
        job['has_sent'] = True
        self._busy_changed(True)
        self._start_step(operation, request.timeout)

    def approve_studio_review(self, digest):
        job = self.read_job
        if not job or job.get('job_kind') != 'studio' or job['stage'] != 'review' or self.runner.busy:return
        try:
            if digest != job['review'].digest:raise ValueError('The approved plan does not match the prepared transaction.')
            if job.get('review_guard') and not job['review_guard']():
                raise ValueError(say('Источник или параметры изменились. Проверьте заготовку и подготовьте новый план.',
                    'The source or settings changed. Check the blank and prepare a new plan.'))
            job['stage'] = 'running';job['review_approved'] = True
            self._studio_report('approved')
            self._studio_next(True)
        except (ValueError, OSError, KeyError, TypeError) as exc:self._studio_failed(exc)

    def _advance_studio(self, output, code):
        job = self.read_job
        if job['stage'].startswith('cleanup_'):
            if code:
                self._studio_uncertain(say('Остановка не подтверждена: проверьте связь и повторите остановку.', 'Stop was not confirmed. Check the link and retry Stop.'));return
            if job['stage'] == 'cleanup_break':
                job['stage'] = 'cleanup_off';self._start_step(Operation('antenna_field_off'), 15000)
            elif job['stage'] == 'cleanup_off':
                job['stage'] = 'cleanup_version';job['cleanup_version_deadline'] = time.monotonic()+20
                job['cleanup_version_probe_count'] = 1
                self._start_step('hw version', 20000)
            else:
                connection = version_probe_status(output, job['device_profile']['transport'], code,
                                                  verified_transport=self.runner.connection_transport)
                record_cleanup_version_check(job, output, code, connection)
                if connection['read_only_recheck_allowed'] and schedule_cleanup_version_check(self,job,job['stage'],self._studio_uncertain):return
                if not connection['connected']:self._studio_uncertain(connection['message']);return
                self._finish_studio()
            return
        request = job['request']
        job['events'][-1].update(state='finished', exit_code=code)
        self.studio.last_output = output
        self.studio.output.appendPlainText(output)
        if self.studio.sections.currentRow() in (6,7):self.studio.output_disclosure.setExpanded(True)
        capture_stop = code == -2 and job.pop('capture_stop', False) and request.allow_capture_stop
        if job.get('cancelled'):
            self._studio_cleanup();return
        bulk_failure = (not job['rf'] and job.get('bulk_cancel_operation') and code != 0)
        if bulk_failure or (code in {-2, -3, -4, -5, -8} and not capture_stop) or code and not (request.allow_failure or capture_stop):
            self._studio_failed(say(f'Этап завершён с кодом {code}. См. журнал.', f'Step ended with code {code}. See the log.'));return
        self._studio_next((output, code))

    def _cancel_studio(self):
        job = self.read_job
        if job['stage'].startswith('cleanup_'):return
        if job.get('capture_stop'):return
        request = job.get('request')
        if self.runner.busy and request and request.allow_capture_stop and not job.get('cancelled'):
            job['capture_stop'] = True
            self.runner.cancel()
            return
        job['cancelled'] = True
        self.studio.status.setText(say('Останавливаем операцию…', 'Stopping operation…'))
        if self.runner.busy:self.runner.cancel()
        else:self._studio_cleanup()

    def _studio_failed(self, error):
        job = self.read_job
        job['error'] = str(error)
        if job['stage'].startswith('cleanup_'):
            self._studio_uncertain(str(error));return
        job['cancelled'] = True
        if self.runner.busy:self.runner.cancel()
        else:self._studio_cleanup()

    def _studio_cleanup(self):
        job = self.read_job
        stop_cleanup_version_checks(job)
        if hasattr(self, 'studio_timer'):self.studio_timer.stop()
        if hasattr(self, 'studio_deadline'):self.studio_deadline.stop()
        try:job['flow'].close()
        except (OSError, ValueError) as exc:job['error'] = str(exc)
        bulk_fence = (job.get('bulk_cancel_operation') and (job.get('cancelled') or job.get('error'))
                      and job['device_profile']['transport'] != 'offline')
        if not job['has_sent'] or not (job['rf'] or bulk_fence):
            self._finish_studio();return
        job['cleanup_version_attempt'] = job.get('cleanup_version_attempt', 0)+1
        job['cleanup_version_recheck_used'] = False
        job.pop('cleanup_version_deadline', None)
        if not job['rf']:
            job['stage'] = 'cleanup_version'
            job['cleanup_version_deadline'] = time.monotonic()+20
            job['cleanup_version_probe_count'] = 1
            self.studio.status.setText(say('Проверяем отклик платы перед продолжением…', 'Checking the device before continuing…'))
            try:self._start_step('hw version', 20000)
            except (ValueError, OSError) as exc:self._studio_uncertain(str(exc))
            return
        job['stage'] = 'cleanup_break'
        self.studio.status.setText(say('Выключаем RF и проверяем отклик платы…', 'Turning RF off and checking the device…'))
        try:self._start_step(Operation('emulation_break'), 15000)
        except (ValueError, OSError) as exc:self._studio_uncertain(str(exc))

    def _studio_uncertain(self, message):
        job = self.read_job;job['stage'] = 'uncertain';job['error'] = message
        stop_cleanup_version_checks(job)
        self.studio.status.setText(message)
        self.studio.reset_button.show()
        try:self._studio_report('stop-unconfirmed')
        except OSError:pass
        self._busy_changed(False)

    def acknowledge_studio_reset(self):
        job = self.read_job
        if not job or job.get('job_kind') != 'studio' or job['stage'] != 'uncertain' or self.runner.busy:return
        from PySide6.QtWidgets import QMessageBox
        if QMessageBox.question(self, say('Сброс питания', 'Power reset'),
                say('USB отключён, Battery и BTpower были выключены? Снять блокировку операции?',
                    'Was USB disconnected and were Battery and BTpower switched off? Release this operation?'),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:return
        self.runner.shutdown();job['manual_power_reset'] = True
        self._finish_studio()

    def _finish_studio(self):
        job = self.read_job
        stop_cleanup_version_checks(job)
        status = 'failed' if job.get('error') else 'cancelled' if job['cancelled'] else 'completed'
        try:self._studio_report(status)
        except OSError as exc:job['error'] = str(exc)
        self.read_job = None
        self.runner.set_rf_lease(False)
        if not self.keep_connection.isChecked() and self.runner._session.has_process:self.runner.disconnect()
        self.studio.reset_button.hide()
        self.studio.last_session = job['session']
        self.studio.status.setText(job.get('error') or (say('Остановлено. Данные сеанса сохранены.', 'Stopped. Session data saved.') if job['cancelled'] else
                                                        say('Готово. Результат и отчёт сохранены.', 'Done. Result and report saved.')))
        callback = job.get('on_finished')
        if callback:callback({'status': 'failed' if job.get('error') else status, 'error': job.get('error',''),
                              'result': job.get('result') or {}, 'session': job['session'],
                              'may_have_written': job['may_have_written'], 'manual_power_reset': job.get('manual_power_reset',False)})
        self._busy_changed(self.runner.busy)
