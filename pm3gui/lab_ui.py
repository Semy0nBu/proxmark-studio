"""Shared asynchronous lifecycle for library emulation and LF transactions."""
from .i18n import tr
from copy import deepcopy
import re
import time
from PySide6.QtCore import QTimer
from .lab_dialogs import EmulationDialog,LFWriteDialog,ResumeDialog
from .memory_map import MemoryMapDialog
from .emulation import EmulationPlan
from .lf_writing import LFWritePlan
from .operations import Operation,Detection,parse_detection
from .connection_checks import (version_probe_status, record_cleanup_version_check,
                                schedule_cleanup_version_check, stop_cleanup_version_checks)


class LabControllerMixin:
    def _lab_available(self):
        if self.runner.busy or self.read_job is not None or self.antenna_job is not None or self.firmware_busy:
            raise ValueError(tr('Завершите текущую операцию устройства.'))

    def open_memory_map(self):
        try:self.memory_dialog=self.workspace_bridge.create_memory_map();self.memory_dialog.show()
        except (ValueError,OSError) as exc:self.error(exc)

    def open_emulation(self):
        if self.read_job and self.read_job.get('job_kind')=='emulation':self.read_job['dialog'].show();self.read_job['dialog'].raise_();return
        try:self._lab_available();self.emulation_dialog=EmulationDialog(self);self.emulation_dialog.show()
        except (ValueError,OSError) as exc:self.error(exc)

    def open_lf_write(self):
        if self.read_job and self.read_job.get('job_kind')=='lf_write':self.read_job['dialog'].show();self.read_job['dialog'].raise_();return
        try:self._lab_available();self.lf_write_dialog=LFWriteDialog(self);self.lf_write_dialog.show()
        except (ValueError,OSError) as exc:self.error(exc)

    def open_resume_read(self):
        try:self._lab_available();self.resume_dialog=ResumeDialog(self);self.resume_dialog.show()
        except (ValueError,OSError) as exc:self.error(exc)

    def _new_lab_job(self,kind,dialog,plan):
        self.keep_connection.setChecked(True)
        self.current_session=plan.directory
        self.read_job={'job_kind':kind,'stage':'prepare','session':plan.directory,'dialog':dialog,'plan':plan,
                       'device_profile':dict(self._device_profile())}
        dialog.set_directory(plan.directory)
        dialog.active(True)
        for name in ('start','prepare','write','clock','uid','duration','partial'):
            if getattr(dialog,name,None) is not None:getattr(dialog,name).setEnabled(False)
        self._busy_changed(False)

    def prepare_lf_write(self,dialog):
        try:
            self._lab_available();plan=LFWritePlan(dialog.identifier(),dialog.clock.currentData(),self._new_session())
            self._new_lab_job('lf_write',dialog,plan);self._next_lf_write()
        except (ValueError,OSError) as exc:
            if self.read_job and self.read_job.get('dialog') is dialog:self._lab_failed(exc)
            else:dialog.status.setText(str(exc))

    def approve_lf_write(self,dialog):
        job=self.read_job
        if not job or job.get('dialog') is not dialog or self.runner.busy:return
        try:job['plan'].approve();dialog.write.setEnabled(False);dialog.active(True);self._next_lf_write()
        except (ValueError,OSError) as exc:self._lab_failed(exc)

    def _next_lf_write(self):
        job=self.read_job;plan=job['plan'];dialog=job['dialog']
        if plan.done:
            job['result']=tr('Запись LF проверена: ID ')+plan.uid+tr(' · 8 блоков page 0 совпали; заводская область подтверждена.')
            self._cleanup_lab();return
        operation=plan.next_operation()
        if operation is None:
            job['stage']='review';dialog.review(plan);self._busy_changed(False);self.operation_progress.hide();return
        job['stage']='lf_step';self._start_step(operation,45000);job['has_sent']=True
        dialog.status.setText((tr('Проверка и резервная копия T5577…') if not plan.approved else tr('Запись и чтение обратно…'))+' · '+plan.pending[0])

    def start_emulation(self,dialog):
        try:
            self._lab_available()
            if dialog.picker.dump is None:raise ValueError(tr('Выберите источник.'))
            plan=EmulationPlan(deepcopy(dialog.picker.dump),dialog.picker.record,self._new_session(),dialog.uid.text().strip(),dialog.clock.currentData(),dialog.partial.isChecked())
            self._new_lab_job('emulation',dialog,plan);self.read_job['duration']=dialog.duration.value()*60000
            self.runner.set_rf_lease(True)
            self.read_job['stage']='emu_prepare_break';self._start_step(Operation('emulation_break'),15000);self.read_job['has_sent']=True
            dialog.status.setText(tr('Готовим эмулятор и проверяем связь с платой…'))
        except (ValueError,OSError) as exc:
            if self.read_job and self.read_job.get('dialog') is dialog:self._lab_failed(exc)
            else:dialog.status.setText(str(exc))

    def _start_simulation(self):
        job=self.read_job;job['stage']='emu_start';job['plan'].report('starting')
        self._start_step(job['plan'].start_operation(),job['duration']+20000)
        job['has_sent']=True;job['rf_dispatched']=True
        job['dialog'].status.setText(tr('Запускаем эмуляцию…'))

    def _emulation_active(self):
        job=self.read_job
        if not job or job.get('active_notified'):return
        job['active_notified']=True;job['plan'].report('running')
        job['dialog'].status.setText(tr('Эмуляция · ')+job['plan'].uid+tr(' · остановка кнопкой или таймером'))
        job['dialog'].progress.hide();self.device_info.setText(tr('Эмуляция ')+job['plan'].uid+tr(' · библиотека и физические карты не изменяются.'))
        self.emulation_timer=QTimer(self);self.emulation_timer.setSingleShot(True);self.emulation_timer.timeout.connect(self.cancel_operation);self.emulation_timer.start(job['duration'])

    def _lab_output(self,text):
        job=self.read_job
        if job and job.get('job_kind')=='emulation' and job['stage']=='emu_start' and job['plan'].family=='em410x':
            if re.search(r'Press.*abort simulation',text,re.I):
                try:self._emulation_active()
                except (ValueError,OSError) as exc:self._lab_failed(exc)

    def _advance_lab_job(self,output,code):
        job=self.read_job;stage=job['stage'];plan=job['plan']
        if stage.startswith('emu_prepare_'):
            if code or stage=='emu_prepare_confirm' and not re.search(r'\bOS\.{2,}\s*\S+',output):raise ValueError(tr('Не удалось подготовить эмулятор; RF-запуск не выполнялся.'))
            if stage=='emu_prepare_break':job['stage']='emu_prepare_off';self._start_step(Operation('antenna_field_off'),15000)
            elif stage=='emu_prepare_off':job['stage']='emu_prepare_confirm';self._start_step('hw version',20000)
            elif plan.family=='mfc':job['stage']='emu_load';self._start_step(plan.load_operation(),45000);job['dialog'].status.setText(tr('Загружаем снимок в память эмулятора…'))
            else:self._start_simulation()
            return
        if stage.startswith('cleanup_'):
            if code:
                self._lab_uncertain(tr('Остановка не подтверждена. Проверьте соединение и повторите «Остановить».'));return
            if stage=='cleanup_break':job['stage']='cleanup_off';self._start_step(Operation('antenna_field_off'),15000)
            elif stage=='cleanup_off':
                job['stage']='cleanup_confirm';job['cleanup_version_deadline']=time.monotonic()+20
                job['cleanup_version_probe_count']=1;self._start_step('hw version',20000)
            else:
                connection=version_probe_status(output,job['device_profile']['transport'],code,
                                                verified_transport=self.runner.connection_transport)
                record_cleanup_version_check(job,output,code,connection)
                if connection['read_only_recheck_allowed'] and schedule_cleanup_version_check(self,job,stage,self._lab_uncertain):return
                if not connection['connected']:
                    self._lab_uncertain(tr('Остановка не подтверждена. Проверьте соединение и повторите «Остановить».'));return
                self._finish_lab()
            return
        if job.get('cancelled'):self._cleanup_lab();return
        if job['job_kind']=='lf_write':
            plan.consume(output,code);self._next_lf_write();return
        if stage=='emu_load':
            if code or re.search(r'\b(?:error|failed|only loaded)\b',output,re.I):raise ValueError(tr('Не удалось загрузить память эмулятора.'))
            job['stage']='emu_verify';self._start_step(plan.verify_operation(),45000);job['dialog'].status.setText(tr('Сверяем память эмулятора со снимком…'))
        elif stage=='emu_verify':
            if code:raise ValueError(tr('Не удалось прочитать память эмулятора.'))
            plan.verify_memory();self._start_simulation()
        elif stage=='emu_start':
            if plan.family=='em410x':
                if code or not job.get('active_notified'):raise ValueError(tr('Клиент LF завершил эмуляцию без подтверждённого запуска.'))
                job['result']=tr('Эмуляция завершена кнопкой устройства.');self._cleanup_lab()
            else:
                if code or not re.search(r'MIFARE',output,re.I) or re.search(r'\b(?:error|failed)\b',output,re.I):raise ValueError(tr('Команда запуска эмуляции не подтверждена.'))
                job['stage']='emulating';self._emulation_active();self._busy_changed(False)

    def _cancel_lab_job(self):
        job=self.read_job
        if job['stage'].startswith('cleanup_') or job.get('cancelled') and self.runner.busy:return
        job['cancelled']=True
        if job['job_kind']=='lf_write':
            try:job['plan'].fail(tr('Остановлено пользователем; отправленная запись могла изменить заготовку.'))
            except OSError:job['log_warning']=tr('Не удалось обновить отчёт.')
        job['result']=tr('Операция остановлена. Отчёт и резервные данные доступны через «Файлы…».')
        if self.runner.busy:self.runner.cancel()
        else:self._cleanup_lab()

    def _cleanup_lab(self):
        job=self.read_job
        if hasattr(self,'emulation_timer'):self.emulation_timer.stop()
        stop_cleanup_version_checks(job)
        job['cleanup_version_attempt']=job.get('cleanup_version_attempt',0)+1
        job['cleanup_version_recheck_used']=False;job.pop('cleanup_version_deadline',None)
        job['stage']='cleanup_break';job['dialog'].status.setText(tr('Останавливаем RF и проверяем отклик платы…'))
        try:job['session'].mkdir(parents=True,exist_ok=True);self._start_step(Operation('emulation_break'),15000)
        except (ValueError,OSError) as exc:self._lab_uncertain(str(exc))

    def _lab_uncertain(self,message):
        stop_cleanup_version_checks(self.read_job)
        if hasattr(self,'emulation_timer'):self.emulation_timer.stop()
        if self.read_job['job_kind']=='emulation' and not self.read_job.get('rf_dispatched'):
            self.read_job['result']=tr('Эмуляция не запускалась: ')+self.read_job.get('failure',message)
            self.read_job['failure']=self.read_job.get('failure',message);self._finish_lab();return
        job=self.read_job;job['stage']='uncertain';job['dialog'].status.setText(message);job['dialog'].progress.hide();job['dialog'].stop.setEnabled(True)
        if job['job_kind']=='emulation':
            try:job['plan'].report('stop_unconfirmed',message)
            except OSError:job['log_warning']=tr('Не удалось обновить отчёт.')
        self.device_info.setText(message);self._busy_changed(False)
        job['dialog'].reset_button.show()

    def acknowledge_lab_reset(self,dialog):
        job=self.read_job
        if not job or job.get('dialog') is not dialog or job['stage']!='uncertain' or self.runner.busy:return
        self.runner.shutdown();job['manual_reset']=True;job['result']=tr('Операция снята после ручного сброса питания. Аппаратная остановка не подтверждена.');self._finish_lab()

    def _lab_failed(self,error):
        job=self.read_job;job['failure']=str(error);job['result']=tr('Операция не завершена: ')+str(error)
        try:
            if job['job_kind']=='lf_write':job['plan'].fail(error)
            else:job['plan'].report('error',error)
        except OSError:job['log_warning']=tr('Не удалось обновить отчёт.')
        if not job.get('has_sent'):self._finish_lab();return
        if job['stage'].startswith('cleanup_'):self._lab_uncertain(str(error))
        elif self.runner.busy:job['cancelled']=True;self.runner.cancel()
        else:self._cleanup_lab()

    def _finish_lab(self):
        job=self.read_job;dialog=job['dialog'];self.read_job=None;self.runner.set_rf_lease(False)
        stop_cleanup_version_checks(job)
        if job['job_kind']=='emulation':
            try:job['plan'].report('manual_reset' if job.get('manual_reset') else 'failed' if job.get('failure') else 'stopped',job.get('failure',''))
            except OSError:job['log_warning']=tr('Не удалось обновить отчёт.')
        dialog.reset_button.hide()
        dialog.active(False)
        for name in ('start','prepare','clock','uid','duration','partial'):
            if getattr(dialog,name,None) is not None:getattr(dialog,name).setEnabled(True)
        if hasattr(dialog,'write'):dialog.write.setEnabled(False)
        message=job.get('result',tr('Эмуляция остановлена. RF выключено, плата отвечает.'))
        if job.get('log_warning'):message+=' '+job['log_warning']
        dialog.status.setText(message);self.device_info.setText(message);self._busy_changed(False)

    def start_resume_read(self,dialog):
        try:
            self._lab_available()
            if dialog.dump is None:raise ValueError(tr('Выберите неполный Classic.'))
            from .partial import validate_partial_metadata
            info=validate_partial_metadata(dialog.dump.metadata['partial_dump'],dialog.dump.data)
            if info.get('classic_size') not in ('mini','1k','2k','4k'):raise ValueError(tr('Нет проверенной геометрии Classic.'))
            detection=Detection('mfc',info['uid'],'MIFARE Classic',info['classic_size'],True)
            self.current_session=self._new_session()
            self.read_job={'stage':'resume_select','session':self.current_session,'detection':detection,'keys_only':False,
                           'recover':False,'resume_dump':deepcopy(dialog.dump),'resume_path':dialog.path}
            dialog.accept();self._start_step(Operation('hf14a_select'),20000)
        except (ValueError,OSError) as exc:self._card_job_failed(exc)
