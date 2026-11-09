"""Incremental incomplete-read integration; uses the common runner and journal."""
from .i18n import tr
from PySide6.QtWidgets import QMessageBox
from .reading import ClassicPartialRead
from .partial import partial_prompt


class PartialReadControllerMixin:
    def _begin_partial_read(self):
        job=self.read_job
        job['partial_reader']=(ClassicPartialRead.resume(job['resume_dump'],job['found_keys'],job['session'],job.get('resume_path',''))
                               if job.get('resume_dump') else ClassicPartialRead(job['detection'],job['found_keys'],job['session']))
        job['stage']='partial_read';self._next_partial_read()

    def _advance_partial_read(self,output,code):
        job=self.read_job;reader=job['partial_reader']
        if job.get('partial_cancelled'):reader.stop(tr('Остановлено пользователем; неподтверждённые блоки отброшены.'))
        else:reader.consume(output,code)
        self._next_partial_read()

    def _next_partial_read(self):
        job=self.read_job;reader=job['partial_reader'];operation=reader.next_operation()
        if operation is not None:
            self._start_step(operation,15000)
            self.operation_progress.setRange(0,max(1,len(reader.sectors)));self.operation_progress.setValue(reader.position)
            self.device_info.setText(tr('Чтение доступных блоков · {0} сохранено · ', f'{len(reader.confirmed)}')+
                                     (tr('сектор {0}', f'{reader.sectors[reader.position]}') if reader.sectors else tr('проверка UID')))
            return
        dump=reader.dump() # Refuses empty or unguarded data.
        if not dump.metadata.get('partial_dump'):
            self._save_device_dump(dump,job['detection'])
            return
        reason=('\n\n'+reader.reason) if reader.reason else ''
        answer=(QMessageBox.StandardButton.Save if self._auto_owned() else
                QMessageBox.question(self,tr('Неполный дамп карты'),partial_prompt(dump.metadata['partial_dump'])+reason,
                                    QMessageBox.StandardButton.Save|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel))
        if answer!=QMessageBox.StandardButton.Save:
            self.read_job=None;self._busy_changed(False)
            self.device_info.setText(tr('Неполный дамп не добавлен в библиотеку. Файл для импорта: ')+str(reader.draft))
            return
        self._save_device_dump(dump,job['detection'])
