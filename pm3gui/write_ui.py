"""HF write review using the existing asynchronous runner and cancellation."""
from .i18n import tr
from .writing import HFWritePlan
from .card_band import card_band
from .write_dialog import WriteDialog
from .operation_recovery import Termination, Link, State
from .studio_data import say
from PySide6.QtCore import QTimer


class HFWriteControllerMixin:
    def open_write_dialog(self):
        if self.runner.busy or self.read_job is not None or self.antenna_job is not None or self.firmware_busy:
            self.error(tr('Завершите текущую операцию.'))
            return
        try:
            dump=self._working_dump() if self.loaded_dump is not None else None
            if dump is not None and card_band({'protocol':self.protocol.text()},dump.metadata)=='lf':dump=None
            self.write_dialog=WriteDialog(self,dump)
            self.write_dialog.show()
        except (ValueError,OSError) as exc:
            self.error(exc)

    def prepare_card_write(self,dialog):
        if self.runner.busy or self.read_job is not None or self.antenna_job is not None or self.firmware_busy:
            return
        try:
            from .transfer import hf_checks
            checks=hf_checks(dialog.transfer.facts,options=dialog.options())
            if any(item.state=='blocked' for item in checks):
                raise ValueError('\n'.join(item.detail for item in checks if item.state=='blocked'))
            directory=self._new_session()
            self.current_session=directory
            candidates=getattr(dialog,'recovery_keys',())
            keys=lambda uid:self.key_library.list_card_keys(uid)+list(candidates)
            plan=HFWritePlan(dialog.dump,dialog.options(),directory,keys)
            self.workspace_bridge.invalidate('new_write_preflight')
            dialog.recovery_panel.hide()
            dialog.plan=plan
            self.read_job={'job_kind':'write','stage':'write_transaction','session':directory,'write_plan':plan,'write_dialog':dialog,
                           'source_token':dialog.source_token()}
            dialog.set_active(True)
            dialog.write_button.setEnabled(False)
            self._next_write_step()
        except (ValueError,OSError) as exc:
            self._card_job_failed(exc)

    def approve_card_write(self,dialog):
        job=self.read_job
        if self.runner.busy or not job or job.get('write_dialog') is not dialog:
            return
        try:
            if not self.workspace_bridge.can_approve(job.get('workspace_epoch')):
                raise ValueError(say('План изменён или уже завершён. Подготовьте новый план.', 'The plan changed or has ended. Prepare a new plan.'))
            if not dialog.configuration_unchanged(job['write_plan'],job['source_token']):
                raise ValueError(tr('Источник или параметры изменились. Проверьте заготовку и подготовьте новый план.'))
            dialog.plan.approve()
            dialog.set_active(True)
            self._next_write_step()
        except (ValueError,OSError) as exc:
            self._card_job_failed(exc)

    def _advance_write(self,output,code):
        job=self.read_job
        if job.get('write_cancelled'):
            self._end_write(False,tr('Запись остановлена. Уже отправленные записи не отменяются; смотрите отчёт и резервную копию.'))
            return
        plan=job['write_plan']
        plan.consume(output,code)
        self._persist_write_keys(job)
        self._next_write_step()

    def _persist_write_keys(self,job):
        plan=job['write_plan'];count=job.get('persisted_write_keys',0)
        if len(plan.expected_keys)>count:
            self.key_library.record_verified_keys(plan.target.uid,plan.expected_keys[count:],source=tr('Proxmark3: автозапись A/B и проверка на карте'))
            job['persisted_write_keys']=len(plan.expected_keys);self.refresh_keys()

    def _next_write_step(self):
        job=self.read_job
        plan,dialog=job['write_plan'],job['write_dialog']
        dialog.show_preflight(plan)
        if plan.done:
            self._persist_write_keys(job)
            message=(tr('Запись проверена чтением: {0} блоков/страниц. UID {1}.', f'{len(plan.verified)}', f'{plan.target.uid}') if plan.entries else
                tr('Данные выбранной области уже совпадают. Запись не потребовалась.'))
            self._end_write(True,message)
            return
        operation=plan.next_operation()
        if operation is None:
            dialog.show_review(plan)
            dialog.status.setText(tr('Проверьте изменения и нажмите «Начать автозапись».'))
            dialog.set_active(True,review=True)
            job['workspace_epoch'] = self.workspace_bridge.bind_review()
            self._busy_changed(False)
            self.operation_progress.hide()
            self.device_info.setText(tr('План записи подготовлен. Ожидается подтверждение в диалоге.'))
            self.operation_badge.setText(tr('Ожидание подтверждения'))
            return
        self._start_step(operation,30000 if not isinstance(operation,str) else 90000)
        self._busy_changed(self.runner.busy)
        description=(tr('Проверка и резервная копия…') if not plan.approved else
                     tr('Запись и проверка: {0} / {1}', f'{len(plan.verified)}', f'{len(plan.entries)}'))
        dialog.status.setText(description)
        self.device_info.setText(description)
        if plan.approved:
            dialog.update_progress(plan)
            self.operation_progress.setRange(0,max(1,len(plan.entries)))
            self.operation_progress.setValue(len(plan.verified))

    def _end_write(self,success,message):
        job=self.read_job
        plan=job['write_plan']
        termination=Termination.COMPLETED if success else Termination.CANCELLED if job.get('write_cancelled') else Termination.FAILED
        captured=self.workspace_bridge.capture_finished(job.get('workspace_epoch'),plan,
            termination=termination,link=Link.READY if success else Link.UNCONFIRMED)
        self.read_job=None
        self._busy_changed(self.runner.busy)
        dialog=job['write_dialog']
        dialog.set_active(False)
        dialog.write_button.setEnabled(False)
        dialog.show_review(job['write_plan'])
        dialog.steps.setText(tr('Автозапись завершена') if success else tr('Автозапись остановлена'))
        dialog.steps.hide()
        dialog.status.setText(message)
        dialog.transfer.finish(success,message)
        if captured:
            dialog.recovery_panel.show()
            self.workspace_bridge.result_changed.emit()
        self.device_info.setText(message)
        if getattr(dialog,'_close_requested',False):QTimer.singleShot(0,dialog.accept)

    def check_interrupted_write(self):
        bridge=self.workspace_bridge
        try:
            self._lab_available()
            if bridge._capture is None or bridge._snapshot is None:
                raise ValueError(say('Контекст записи недоступен.', 'Write context is unavailable.'))
            from .write_evidence import snapshot_from_live_plan
            if snapshot_from_live_plan(bridge._plan,bridge._capture,termination=bridge._snapshot.termination,
                    link=Link.UNCONFIRMED).binding != bridge._snapshot.binding:
                raise ValueError(say('План был изменён.', 'The plan has changed.'))
            from .write_readback import readback_flow
            epoch,binding=bridge.epoch,bridge._snapshot.binding
            def finished(result):
                if result['status']=='completed' and not result.get('manual_power_reset'):
                    bridge.accept_postcheck(epoch,binding,result['result'])
                bridge.result_changed.emit()
            self.start_studio(readback_flow(bridge._plan,binding,self._device_profile()['transport'],
                verified_transport=lambda:self.runner.connection_transport),
                say('Перечитывание результата записи', 'Write result readback'),rf=True,on_finished=finished)
            bridge.result_changed.emit()
        except (ValueError,OSError) as exc:self.error(exc)

    def prepare_recovery_write(self):
        from copy import deepcopy
        bridge=self.workspace_bridge
        try:
            self._lab_available()
            result=bridge.decision()
            if not result or result.state not in {State.NOT_WRITTEN,State.CANCELLED_BEFORE_WRITE,State.BACKUP_FAILED,State.DIFFERENCES_CONFIRMED}:
                raise ValueError(say('Сначала перечитайте карту и сравните с планом.', 'Read and compare the card first.'))
            source,options,target=deepcopy(bridge._source),deepcopy(bridge._options),bridge._target
            old=bridge._dialog
            if old is not None:old.close()
            self.write_dialog=dialog=WriteDialog(self,source)
            dialog.family.setCurrentIndex(dialog.family.findData(options.family));dialog._family_changed()
            dialog.key_a.setText(options.key_a);dialog.key_b.setText(options.key_b);dialog.mfu_key.setText(options.mfu_key)
            dialog.first.setValue(options.first);dialog.last.setValue(options.last)
            dialog.selected_blocks.setText(', '.join(map(str,options.blocks)))
            dialog.target_uid.setText(target.uid)
            dialog.include_keys.setChecked(options.include_keys);dialog.include_unknown.setChecked(options.include_unknown)
            dialog.secure.setChecked(options.secure_channel)
            # These are only candidates. The new preflight authenticates them
            # afresh; no key-library entry is invented from a write ACK.
            from .writing import block_sector
            dialog.recovery_keys=[]
            for entry in bridge._entries:
                if entry['trailer']:
                    dialog.recovery_keys.extend({'sector':block_sector(entry['block']),'type':kind,'key_hex':entry[field][a:b]}
                        for field in ('new','old') for kind,a,b in [('A',0,12),('B',20,32)])
            dialog.show()
        except (ValueError,OSError) as exc:self.error(exc)

    def _write_failed(self,message):
        job=self.read_job
        try:
            job['write_plan'].fail(message)
        except OSError:
            pass
        path=job['write_plan'].directory/'write-report.json'
        suffix=(tr('Доступный отчёт и резервные данные: ')+str(path)) if path.is_file() else tr('Не удалось сохранить отчёт; запись остановлена.')
        self._end_write(False,tr('Запись не завершена: ')+str(message)+tr('\nАвтоматического повтора нет. ')+suffix)
