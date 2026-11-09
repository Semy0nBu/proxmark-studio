"""Continuous HF/LF intake using the normal reader, runner and hardware lock."""
from copy import deepcopy
from datetime import datetime, timezone
from dataclasses import replace
import json
import sqlite3
import time
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox
from .auto_capture import PresenceGate, scan_card, MultipleCards, UnclearScan, transport_failed
from .device import parse_connection_status, DeviceSettings, build_invocation
from .operations import Operation
from .core import export_dump
from .studio_data import say, structured_dump
from .studio_flows import Request, read_hf_profile, read_lf_profile
from .lf_capture import validate_lf_search_capture
from .connection_checks import (version_probe_status, record_cleanup_version_check,
                                schedule_cleanup_version_check, stop_cleanup_version_checks)


class AutoCaptureMixin:
    def _auto_owned(self):
        return bool(self.auto_intake is not None and self.read_job and self.read_job.get('auto_capture'))

    def toggle_auto_capture(self, checked):
        if not checked:
            if self._auto_owned():self._stop_auto_capture()
            return
        if self.auto_intake is not None:
            return
        try:
            self._lab_available()
            directory = self._new_session()
            profile = dict(self._device_profile(), keep_open=True)
            # Validate configuration without launching a process or claiming RF.
            build_invocation(DeviceSettings(profile['client_path'], profile['port'], str(directory), profile['transport'], True, True), 'hw version')
            self.auto_intake = {'root': directory, 'profile': profile, 'gate': PresenceGate(), 'band': 'hf',
                                'stopping': False, 'saved': 0, 'attempts': 0, 'polls': 0, 'unclear': 0,
                                'events': [], 'candidate': None, 'error': '', 'recover': self.recovery_enabled.isChecked()}
            self.auto_capture_button.blockSignals(True);self.auto_capture_button.setChecked(True);self.auto_capture_button.blockSignals(False)
            self.auto_capture_button.setText(say('Остановить автоприём', 'Stop automatic intake'))
            if not hasattr(self, 'auto_poll_timer'):
                self.auto_poll_timer = QTimer(self);self.auto_poll_timer.setSingleShot(True)
                self.auto_poll_timer.timeout.connect(self._auto_poll)
                self.auto_card_timer = QTimer(self);self.auto_card_timer.setSingleShot(True)
                self.auto_card_timer.timeout.connect(lambda:self._auto_fail(say('Истекло время чтения карты.', 'Card read deadline expired.')))
            self.runner.set_rf_lease(True)
            self._auto_job('connect')
            self._busy_changed(False)
            self._auto_message(say('Проверяем связь…', 'Checking connection…'))
            self._start_step('hw version', 20000)
        except (OSError, ValueError, sqlite3.Error) as exc:
            if self._auto_owned():self._auto_fail(str(exc))
            else:
                self.auto_intake = None
                self.auto_capture_button.blockSignals(True);self.auto_capture_button.setChecked(False);self.auto_capture_button.blockSignals(False)
                self.auto_capture_status.setText(str(exc))

    def _auto_job(self, stage):
        state = self.auto_intake
        self.current_session = state['root']
        self.read_job = {'job_kind': 'auto_capture', 'auto_capture': True, 'stage': stage,
                         'session': state['root'], 'device_profile': state['profile']}

    def _auto_message(self, message):
        self.auto_capture_status.setText(say('Сохранено: ', 'Saved: ') + str(self.auto_intake['saved']) + ' · ' + message)
        self.device_info.setText(message)

    def _auto_report(self, status):
        state = self.auto_intake
        report = {'version': 1, 'status': status, 'saved': state['saved'], 'attempts': state['attempts'],
                  'polls': state['polls'], 'error': state['error'], 'events': state['events'][-100:],
                  'manual_power_reset_acknowledged': state.get('manual_reset', False)}
        if state.get('cleanup_version_checks'):report['cleanup_version_checks']=state['cleanup_version_checks']
        if state.get('last_lf_capture'):report['last_lf_capture']=state['last_lf_capture']
        temporary = state['root']/'auto-intake.tmp'
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        temporary.replace(state['root']/'auto-intake.json')

    def _auto_schedule(self):
        self._auto_job('waiting')
        self._auto_report('running')
        if self.auto_intake['events']:
            self.device_info.setText(self.auto_capture_status.text())
        else:
            self._auto_message(say('Приложите карту · HF + LF', 'Present a card · HF + LF'))
        self.auto_poll_timer.start(1000)

    def _auto_poll(self):
        if not self._auto_owned() or self.auto_intake['stopping']:
            return
        try:
            state = self.auto_intake
            if self.runner.busy:raise ValueError('Another client operation is active.')
            state['polls'] += 1
            band = state['band']
            self._auto_message(say('Приложите карту · поиск ', 'Present a card · scanning ') + band.upper())
            self._auto_start_search(band)
        except (OSError, ValueError) as exc:
            self._auto_fail(str(exc))

    def _auto_start_search(self, band, after_capture='search'):
        state,job=self.auto_intake,self.read_job
        if band=='lf' and state['profile']['transport']=='blueshark':
            # Broad native lf search can enter a 500000-sample COTAG raw USB
            # capture before the passive decoders. FPC/SPP instead captures a
            # finite frame below RDV4 BigBuf, then searches only that new buffer.
            job.update(stage='lf_search_capture',lf_search_after_capture=after_capture)
            self._start_step(Operation('lf_capture_search_buffer'),45000)
            self.current_log_name=('scan-' if after_capture=='search' else 'confirm-')+'lf-capture.log'
        else:
            job['stage']=after_capture
            self._start_step(band+' search',90000 if band=='hf' else 120000)
            self.current_log_name=('scan-' if after_capture=='search' else 'confirm-')+band+'.log'

    def _route_auto_reply(self, output, code):
        if not self._auto_owned():return False
        self.auto_intake['last_code'] = code
        if (self.read_job.get('job_kind') == 'auto_capture' or self.auto_intake['stopping']
                or transport_failed(output, code)):
            self._advance_auto_capture(output, code)
            return True
        return False

    def _advance_auto_capture(self, output, code):
        state, job = self.auto_intake, self.read_job
        stage = job['stage']
        if stage.startswith('auto_cleanup_'):
            if code:
                self._auto_uncertain();return
            if stage == 'auto_cleanup_break':
                job['stage'] = 'auto_cleanup_off';self._start_step(Operation('antenna_field_off'), 15000)
            elif stage == 'auto_cleanup_off':
                job['stage'] = 'auto_cleanup_version';job['cleanup_version_deadline']=time.monotonic()+20
                job['cleanup_version_probe_count']=1
                self._start_step('hw version', 20000)
            else:
                connection=version_probe_status(output,state['profile']['transport'],code,
                                                verified_transport=self.runner.connection_transport)
                record_cleanup_version_check(job,output,code,connection)
                state['cleanup_version_checks']=job['cleanup_version_checks']
                if (connection['read_only_recheck_allowed'] and schedule_cleanup_version_check(
                        self,job,stage,lambda message:self._auto_uncertain())):return
                if connection['connected']:self._auto_finish()
                else:self._auto_uncertain()
            return
        if state['stopping']:
            self._auto_cleanup();return
        if stage=='lf_search_capture':
            try:evidence=validate_lf_search_capture(output,code)
            except ValueError as exc:self._auto_fail(str(exc));return
            next_stage=job.pop('lf_search_after_capture')
            job['stage']=next_stage
            state['last_lf_capture']={**evidence,'for_stage':next_stage,
                                      'captured_at':datetime.now(timezone.utc).isoformat()}
            self._start_step(Operation('lf_search_buffer'),30000)
            self.current_log_name=('scan-' if next_stage=='search' else 'confirm-')+'lf.log'
            return
        if transport_failed(output, code):
            self._auto_fail(say('Связь или операция прервана; автоприём остановлен.', 'Connection or operation interrupted; intake stopped.'));return
        if stage == 'connect':
            if not parse_connection_status(output, state['profile']['transport'], code,
                                           verified_transport=self.runner.connection_transport)['connected']:
                self._auto_fail(say('Плата не подтвердила подключение.', 'The device did not confirm a connection.'));return
            self._auto_schedule();return
        if stage == 'field_off':
            if code:self._auto_fail(say('Не удалось выключить поле.', 'Could not turn the field off.'))
            else:self._auto_schedule()
            return
        if stage == 'family_read':
            request = job['request']
            if code and not request.allow_failure:
                self._auto_read_failed(say(f'Чтение завершено с кодом {code}.', f'Reading ended with code {code}.'));return
            self._auto_family_next((output, code));return
        if stage in {'search', 'confirm_detection'}:
            if code not in {0, -10}:
                self._auto_fail(say(f'Поиск завершён с кодом {code}.', f'Search ended with code {code}.'));return
            band = state['candidate'].band if stage == 'confirm_detection' else state['band']
            try:card = scan_card(band, output)
            except (MultipleCards, UnclearScan) as exc:
                state['gate'].unclear(band)
                if stage == 'confirm_detection':
                    self._auto_resume(say('Карта не подтверждена; запись пропущена.', 'Card not confirmed; record skipped.'));return
                state['unclear'] += 1
                if isinstance(exc, MultipleCards):
                    state['unclear'] = 0
                    self._auto_message(say('Оставьте одну карту на антенне.', 'Leave one card on the antenna.'))
                elif state['unclear'] >= 3:
                    self._auto_fail(say('Три ответа без результата поиска. Проверьте журнал.', 'Three search replies without a usable result. Check the log.'));return
                state['band'] = 'lf' if band == 'hf' else 'hf';self._auto_schedule();return
            state['unclear'] = 0
            if stage == 'confirm_detection':
                if card is None or card.token != state['candidate'].token:
                    self._auto_resume(say('Карта изменилась или снята; запись пропущена.', 'Card changed or was removed; record skipped.'));return
                doc = {'protocol': card.protocol, 'uid': card.uid, 'rf_band': card.band,
                       'read_scope': 'detection-only-not-memory-dump', 'search_output': output[-65536:],
                       'read_error': state.pop('read_error', '')}
                dump = structured_dump(doc, card.protocol, card.uid, 'detection-record')
                dump.metadata['rf_band'] = card.band
                self._auto_save_dump(dump);return
            state['band'] = 'lf' if band == 'hf' else 'hf'
            if not state['gate'].observe(band, card):
                self._auto_schedule();return
            self._auto_begin_card(card, output)

    def _auto_begin_card(self, card, output):
        state = self.auto_intake
        state.update(candidate=card, attempts=state['attempts']+1)
        directory = state['root']/'cards'/f"{state['attempts']:05d}"
        directory.mkdir(parents=True)
        (directory/'discovery.log').write_text(output, encoding='utf-8')
        self.current_session = directory
        self.read_job = {'auto_capture': True, 'session': directory, 'stage': 'info',
                         'device_profile': state['profile'], 'keys_only': False, 'band': card.band,
                         'recover': state['recover'], 'detection': card.legacy_detection()}
        self.auto_card_timer.start(1800000)
        self._auto_message(say('Читаем ', 'Reading ') + card.protocol + (' · '+card.uid if card.uid else ''))
        self.detected = card.legacy_detection()
        self.detection_label.setText(card.band.upper()+' · '+card.protocol+(' · '+card.uid if card.uid else ''))
        self.workspace_bands.setCurrentIndex(1 if card.band == 'lf' else 0)
        if card.kind == 'mfc':
            self._show_detection(card.legacy_detection())
            self._start_step(Operation('hf14a_select'))
        elif card.kind == 'mfu':
            self._show_detection(card.legacy_detection());self.read_job['stage'] = 'dump'
            self._start_step(Operation('mfu_dump', output_prefix=directory/'card', auth_key=self.mfu_auth.text().strip()), 120000)
        elif card.kind == 'em410x':
            self.read_job['stage'] = 'lf_identifier';self._start_step(Operation('lf_em410x_read'), 45000)
        elif card.kind in {'lf_profile', 'hf_profile'}:
            self.read_job.update(job_kind='auto_capture', stage='family_read',
                                 flow=iter((read_lf_profile(card.protocol) if card.band == 'lf' else read_hf_profile(card.protocol))(directory)))
            self._auto_family_next()
        else:self._auto_confirm_detection()

    def _auto_family_next(self, response=None):
        job = self.read_job
        try:
            request = next(job['flow']) if response is None else job['flow'].send(response)
        except StopIteration as done:
            result = done.value
            candidate = self.auto_intake['candidate']
            if candidate.uid and str(result.get('uid', '')).upper() != candidate.uid.upper():
                self._auto_resume(say('Идентификатор изменился; результат не сохранён.', 'Identifier changed; result not saved.'));return
            if not candidate.uid and result.get('uid'):
                self.auto_intake['candidate'] = replace(candidate, uid=result['uid'])
            self._auto_save_dump(result['dump']);return
        job['request'] = request
        self._start_step(request.operation(), request.timeout)

    def _auto_confirm_detection(self):
        job = self.read_job
        flow = job.pop('flow', None)
        if flow is not None:flow.close()
        job.update(job_kind='auto_capture', stage='confirm_detection')
        band = self.auto_intake['candidate'].band
        self._auto_start_search(band,'confirm_detection')

    def _auto_read_failed(self, error):
        if self.auto_intake['stopping']:
            self.auto_intake['error'] = str(error)
            self._auto_uncertain();return
        if (isinstance(error, sqlite3.Error) or isinstance(error, OSError) and not isinstance(error, FileNotFoundError)
                or self.read_job['stage'] == 'confirm_detection'):
            self._auto_fail(str(error));return
        self.auto_intake['read_error'] = str(error)
        self._auto_confirm_detection()

    def _auto_save_dump(self, dump, detected=None):
        state = self.auto_intake
        card = state['candidate']
        dump = deepcopy(dump)
        dump.metadata.update(rf_band=card.band, auto_intake={'uid': card.uid, 'protocol': card.protocol,
                             'captured_at': datetime.now(timezone.utc).isoformat(), 'session': str(self.current_session)})
        export_dump(dump, self.current_session/'saved.json')
        protocol = card.protocol + (' · '+card.classic_size.upper() if card.classic_size else '')
        matches = [row for row in self.library.list_cards() if card.uid and row['uid'] == card.uid and row['protocol'] == protocol and row['rf_band'] == card.band]
        detection_only = dump.metadata.get('snapshot_kind') == 'detection-record'
        matches = [row for row in matches if
                   (self.library.get_dump(row['id']).metadata.get('snapshot_kind') == 'detection-record') == detection_only]
        if len(matches) == 1:
            card_id = matches[0]['id'];self.library.save_revision(card_id, dump, say('Автоматическое чтение', 'Automatic read'))
        else:
            card_id = self.library.create_card(protocol+' · '+(card.uid or say('обнаружение', 'detection')), dump,
                uid=card.uid, protocol=protocol, notes=say('Автоприём · ', 'Automatic intake · ')+str(self.current_session))
        scope = 'partial' if dump.metadata.get('partial_dump') else 'detection' if dump.metadata.get('snapshot_kind') == 'detection-record' else 'identifier' if card.band == 'lf' else 'dump'
        state['saved'] += 1
        state['events'].append({'card_id': card_id, 'uid': card.uid, 'protocol': protocol, 'band': card.band,
                                'scope': scope, 'session': str(self.current_session)})
        state['events'] = state['events'][-100:]
        self.refresh_library()
        detail = {'partial': say('Сохранён неполный дамп', 'Partial dump saved'), 'identifier': say('Сохранён идентификатор', 'Identifier saved'),
                  'detection': say('Сохранено обнаружение; память не прочитана', 'Detection saved; memory was not read'),
                  'dump': say('Снимок сохранён', 'Snapshot saved')}[scope]
        message = detail+' · '+protocol+(' · '+card.uid if card.uid else '')
        self.statusBar().showMessage(message, 15000)
        self._auto_resume(message)

    def _auto_resume(self, message=''):
        if message:self._auto_message(message)
        flow = self.read_job.pop('flow', None)
        if flow is not None:flow.close()
        self.auto_card_timer.stop()
        self.auto_intake['candidate'] = None
        self._auto_job('field_off')
        self._start_step(Operation('antenna_field_off'), 15000)

    def _auto_fail(self, error):
        if not self._auto_owned():return
        self.auto_intake['error'] = str(error)
        self._stop_auto_capture()

    def _stop_auto_capture(self):
        if not self._auto_owned():return
        state = self.auto_intake
        if state['stopping'] and self.read_job['stage'] != 'uncertain':return
        state['stopping'] = True
        self.auto_poll_timer.stop();self.auto_card_timer.stop()
        self._auto_message(state['error'] or say('Останавливаем автоприём…', 'Stopping intake…'))
        self.auto_capture_button.setEnabled(False)
        if self.read_job['stage'].startswith('auto_cleanup_'):return
        if self.runner.busy:self.runner.cancel()
        else:self._auto_cleanup()

    def _auto_cleanup(self):
        job = self.read_job
        stop_cleanup_version_checks(job)
        try:
            flow = job.pop('flow', None)
            if flow is not None:flow.close()
            reader = job.get('partial_reader')
            if reader is not None and not reader.done:reader.stop('Automatic intake stopped; checkpoints retained.')
            self._auto_job('auto_cleanup_break')
            self.auto_intake['cleanup_version_attempt']=self.auto_intake.get('cleanup_version_attempt',0)+1
            self.read_job.update(cleanup_version_attempt=self.auto_intake['cleanup_version_attempt'],
                                 cleanup_version_recheck_used=False,
                                 cleanup_version_checks=self.auto_intake.get('cleanup_version_checks',[]))
            self._start_step(Operation('emulation_break'), 15000)
        except (OSError, ValueError) as exc:
            self.auto_intake['error'] = str(exc);self._auto_uncertain()

    def _auto_uncertain(self):
        stop_cleanup_version_checks(self.read_job)
        self.read_job['stage'] = 'uncertain'
        self.auto_reset_button.show()
        self._auto_message(say('Остановка не подтверждена. Повторите Stop или выполните полный сброс питания.',
                               'Stop was not confirmed. Retry Stop or fully power-cycle the device.'))
        try:self._auto_report('stop-unconfirmed')
        except OSError:pass
        self._busy_changed(False)

    def acknowledge_auto_reset(self):
        if not self._auto_owned() or self.read_job['stage'] != 'uncertain' or self.runner.busy:return
        if QMessageBox.question(self, say('Сброс питания', 'Power reset'),
                say('USB отключён, Battery и BTpower были выключены? Снять блокировку?',
                    'Was USB disconnected and were Battery and BTpower switched off? Release the lock?'),
                QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:return
        self.runner.shutdown();self.auto_intake['manual_reset'] = True;self._auto_finish()

    def _auto_finish(self):
        state = self.auto_intake
        stop_cleanup_version_checks(self.read_job)
        try:self._auto_report('failed' if state['error'] else 'stopped')
        except OSError as exc:state['error'] = str(exc)
        self.auto_poll_timer.stop();self.auto_card_timer.stop()
        self.auto_intake = None;self.read_job = None
        self.runner.set_rf_lease(False)
        if not self.keep_connection.isChecked() and self.runner._session.has_process:self.runner.disconnect()
        self.auto_reset_button.hide()
        self.auto_capture_button.blockSignals(True);self.auto_capture_button.setChecked(False);self.auto_capture_button.blockSignals(False)
        self.auto_capture_button.setText(say('Автоприём HF + LF', 'Automatic intake HF + LF'))
        self.auto_capture_status.setText(say('Автоприём остановлен · сохранено: ', 'Intake stopped · saved: ')+str(state['saved'])+(' · '+state['error'] if state['error'] else ''))
        self.device_info.setText(self.auto_capture_status.text())
        self._busy_changed(False)
        if state.get('close_requested'):QTimer.singleShot(0, self.close)
