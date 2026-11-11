"""Native review screens for simulation, LF writing and read recovery."""
from .i18n import tr
from pathlib import Path
from PySide6.QtCore import Qt,QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox,QComboBox,QDialog,QFileDialog,QFormLayout,QHBoxLayout,QLabel,QLineEdit,
    QProgressBar,QPushButton,QSpinBox,QTableWidget,QTableWidgetItem,QVBoxLayout,QScrollArea,QWidget)
from .source_picker import SourcePicker
from .emulation import simulation_profile
from .core import parse_dump
from .theme import mono_font
from .presentation import configure_table
from .transfer_ui import TransferPanel
from .transfer import Check
from .interface_mode import Guidance



from .ui import SoftButton

def caption(text,kind='muted'):
    result=QLabel(text);result.setWordWrap(True);result.setTextFormat(Qt.TextFormat.PlainText);result.setObjectName(kind);return result


class OperationDialog(QDialog):
    def __init__(self,owner,title):
        super().__init__(owner);self.owner=owner;self._close_requested=False;self.setModal(True);self.setWindowTitle(title)
        screen=owner.screen().availableGeometry();self.resize(min(820,screen.width()-40),min(650,screen.height()-70))
        self.outer=QVBoxLayout(self);scroll=QScrollArea();scroll.setWidgetResizable(True);content=QWidget();self.layout=QVBoxLayout(content)
        self.layout.setSpacing(14);self.layout.addWidget(caption(title,'title'));scroll.setWidget(content);self.outer.addWidget(scroll,1)
        self.status=caption(tr('Выберите источник.'));self.progress=QProgressBar();self.progress.setTextVisible(False);self.progress.hide()
        self.stop=SoftButton(tr('Остановить'));self.stop.setEnabled(False);self.stop.clicked.connect(owner.cancel_operation)
        self.close_button=SoftButton(tr('Закрыть'));self.close_button.clicked.connect(self.reject)
        self.directory=None;self.files_button=SoftButton(tr('Файлы…'));self.files_button.setEnabled(False)
        self.files_button.clicked.connect(lambda:QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.directory))) if self.directory else None)
        self.reset_button=SoftButton(tr('Плата обесточена'));self.reset_button.hide()
        self.reset_button.setToolTip(tr('После отключения USB и аккумулятора Blue Shark. Снять блокировку, сохранив отсутствие аппаратного подтверждения остановки.'))
        self.reset_button.clicked.connect(lambda:owner.acknowledge_lab_reset(self))

    def footer(self,buttons):
        self.layout.addStretch();self.outer.addWidget(self.progress);self.outer.addWidget(self.status)
        row=QHBoxLayout()
        for button in buttons:row.addWidget(button)
        row.addStretch();row.addWidget(self.files_button);row.addWidget(self.stop);row.addWidget(self.close_button);self.outer.addLayout(row);self.outer.addWidget(self.reset_button)

    def set_directory(self,directory):
        self.directory=directory;self.files_button.setEnabled(True);self.files_button.setToolTip(str(directory))

    def active(self,active,review=False):
        self.picker.setEnabled(not active);self.stop.setEnabled(active);self.progress.setVisible(active and not review)
        self.progress.setRange(0,0);self.close_button.setEnabled(not active or review)

    def reject(self):
        if self.owner.read_job and self.owner.read_job.get('dialog') is self:
            self._close_requested=True
            self.owner.cancel_operation()
            if self.owner.read_job:return
        super().reject()

    def closeEvent(self,event):
        if self.owner.read_job and self.owner.read_job.get('dialog') is self:
            self.reject()
            if self.owner.read_job:event.ignore();return
        super().closeEvent(event)


class EmulationDialog(OperationDialog):
    def __init__(self,owner):
        super().__init__(owner,tr('Эмуляция из библиотеки'))
        self.layout.addWidget(caption(tr('1  Источник    ›    2  Проверка памяти    ›    3  Эмуляция'),'section'))
        self.picker=SourcePicker(owner);self.layout.addWidget(self.picker)
        form=QFormLayout();self.form=form;self.uid=QLineEdit();self.uid.setFont(mono_font());form.addRow('UID / ID',self.uid)
        self.clock=QComboBox();self.clock.addItem('RF/64',64);self.clock.addItem('RF/32',32);form.addRow('EM410x',self.clock)
        self.duration=QSpinBox();self.duration.setRange(1,20);self.duration.setValue(5);self.duration.setSuffix(tr(' мин'));form.addRow(tr('Автоостановка'),self.duration)
        self.layout.addLayout(form);self.profile=caption('');self.layout.addWidget(self.profile)
        self.partial=QCheckBox(tr('Использовать неполный образ с заполнителями'));self.layout.addWidget(self.partial)
        self.start=SoftButton(tr('Начать эмуляцию'));self.start.setObjectName('primary');self.start.clicked.connect(lambda:owner.start_emulation(self))
        self.footer([self.start]);self.picker.changed.connect(self.source_changed)
        self.partial.toggled.connect(lambda checked:self.start.setEnabled(self.profile_valid and (not self.picker.dump.metadata.get('partial_dump') or checked)));self.source_changed()

    def source_changed(self,*_):
        self.profile_valid=False
        try:
            if self.picker.dump is None:raise ValueError(tr('Выберите снимок Classic или EM410x ID.'))
            family,size,uid=simulation_profile(self.picker.dump,self.picker.record)
            self.profile_valid=True
            self.uid.setText(uid);self.clock.setEnabled(family=='em410x')
            self.form.setRowVisible(self.clock,family=='em410x');self.form.labelForField(self.uid).setText('UID' if family=='mfc' else 'ID EM410x')
            self.profile.setText(('HF · MIFARE Classic '+size.upper()+tr(' · загрузка и проверка памяти') if family=='mfc' else tr('LF · EM410x · эмуляция идентификатора')))
            incomplete=bool(self.picker.dump.metadata.get('partial_dump'));self.partial.setVisible(incomplete)
            self.start.setEnabled(not incomplete or self.partial.isChecked());self.status.setText(tr('Источник готов. Запуск выполняется кнопкой; карта не перезаписывается.'))
        except ValueError as exc:self.start.setEnabled(False);self.profile.setText('Classic Mini/1K/2K/4K · EM410x');self.status.setText(str(exc))


class LFWriteDialog(OperationDialog):
    def __init__(self,owner):
        super().__init__(owner,tr('Мастер записи LF'))
        self.picker=SourcePicker(owner,'lf');self.layout.addWidget(self.picker)
        self.transfer=TransferPanel(owner=owner);self.layout.addWidget(self.transfer)
        self.profile=caption(tr('EM410x → T5577 без пароля · резервная копия page 0 и контроль заводской области'));self.layout.addWidget(Guidance(owner,self.profile))
        row=QHBoxLayout();self.clock=QComboBox();self.clock.addItem('RF/64',64);self.clock.addItem('RF/32',32);row.addWidget(caption(tr('Скорость')));row.addWidget(self.clock);row.addStretch();self.layout.addLayout(row)
        self.table=QTableWidget(0,3);self.table.setHorizontalHeaderLabels([tr('Блок'),tr('Было'),tr('Будет')]);configure_table(self.table,[80,250,250]);self.table.setMinimumHeight(150);self.layout.addWidget(self.table,1)
        self.prepare=SoftButton(tr('Проверить заготовку'));self.prepare.clicked.connect(lambda:owner.prepare_lf_write(self))
        self.write=SoftButton(tr('Записать и проверить'));self.write.setObjectName('primary');self.write.setEnabled(False);self.write.clicked.connect(lambda:owner.approve_lf_write(self))
        self.footer([self.prepare,self.write]);self.picker.changed.connect(self.source_changed);self.source_changed()

    def identifier(self):
        if self.picker.dump is None:raise ValueError(tr('Выберите EM410x ID.'))
        family,size,identifier=simulation_profile(self.picker.dump,self.picker.record)
        if family!='em410x':raise ValueError(tr('Этот мастер записывает EM410x ID на T5577. Другие профили доступны в инструментах LF.'))
        return identifier

    def source_changed(self,*_):
        self.write.setEnabled(False);self.table.setRowCount(0)
        self.transfer.set_source(self.picker.dump,self.picker.record)
        self.transfer.reset_target()
        try:self.status.setText('ID '+self.identifier()+tr(' · положите одну T5577 на LF-антенну.'));self.prepare.setEnabled(True)
        except ValueError as exc:self.status.setText(str(exc));self.prepare.setEnabled(False)

    def review(self,plan):
        self.show_preflight(plan)
        self.set_directory(plan.directory)
        self.table.setRowCount(3)
        for row,b in enumerate(range(3)):
            for col,value in enumerate((str(b),f'{plan.backup[b]:08X}',f'{plan.expected[b]:08X}')):
                item=QTableWidgetItem(value);item.setFont(mono_font());self.table.setItem(row,col,item)
        self.active(True,review=True);self.prepare.setEnabled(False);self.clock.setEnabled(False);self.write.setEnabled(True)
        self.status.setText(tr('T5577 подтверждена · копия сохранена. Будут записаны блоки 0–2; 3–7 проверяются на неизменность.'))

    def show_preflight(self,plan):
        if plan.config is not None:self.transfer.set_target('T5577 / T55x7',details=tr('Микросхема определена. Полное резервное чтение и заводская область проверяются отдельно.'))
        self.transfer.show_checks([
            Check(tr('Источник'),'ok',tr('Переносится EM410x ID, а не полный образ исходного чипа.')),
            Check(tr('Заготовка'),'ok' if plan.ready else 'pending',tr('Нужна T5577/T55x7 без пароля. Обычная карта EM410x не является подтверждённой заготовкой.')),
            Check(tr('Резервная копия'),'ok' if plan.ready else 'pending',tr('Сохраняются все 8 блоков page 0 и проверяется заводская область page 1.')),
            Check(tr('Проверка результата'),'limit',tr('Записывается ID и проверяются блоки T5577. UID исходного HF-чипа и его память этим способом не переносятся.'))
        ],ready=plan.ready and not plan.error)
        if plan.approved:self.transfer.set_stage(4 if plan.pending and plan.pending[0].startswith(('verify','final','identifier','redetect')) else 3)


class ResumeDialog(QDialog):
    def __init__(self,owner):
        super().__init__(owner);self.owner=owner;self.dump=None;self.path='';self.setWindowTitle(tr('Продолжить чтение'));self.resize(760,460)
        layout=QVBoxLayout(self);layout.setSpacing(14);layout.addWidget(caption(tr('Продолжение чтения'),'title'))
        layout.addWidget(caption(tr('Выберите сохранённый неполный снимок Classic. UID и ключи проверяются заново; ранее считанные данные дополняются.')))
        self.sources=QComboBox();self.sources.addItem(tr('Выберите промежуточный результат'),None)
        self.current=owner._working_dump() if owner.loaded_dump and owner.loaded_dump.metadata.get('partial_dump',{}).get('kind')=='mfc' else None
        if self.current:self.sources.addItem(tr('Текущий неполный дамп'),('current',''))
        paths=sorted((owner.data_dir/'sessions').glob('*/partial-draft.json'),key=lambda p:p.stat().st_mtime,reverse=True) if (owner.data_dir/'sessions').is_dir() else []
        for path in paths:self.sources.addItem(path.parent.name,('file',str(path)))
        row=QHBoxLayout();row.addWidget(self.sources,1);browse=SoftButton(tr('Файл…'));browse.clicked.connect(self.browse);row.addWidget(browse);layout.addLayout(row)
        self.info=caption('');layout.addWidget(self.info);layout.addStretch()
        self.start=SoftButton(tr('Проверить карту и продолжить'));self.start.setObjectName('primary');self.start.setEnabled(False);self.start.clicked.connect(lambda:owner.start_resume_read(self));layout.addWidget(self.start)
        self.sources.currentIndexChanged.connect(self.selected)
        if self.sources.count()>1:self.sources.setCurrentIndex(1)

    def selected(self,*_):
        try:
            item=self.sources.currentData();self.dump=None;self.start.setEnabled(False)
            if not item:return
            self.path=item[1] if item[0]=='file' else tr('Библиотека · ')+str(self.owner.loaded_revision)
            self.dump=parse_dump(Path(item[1])) if item[0]=='file' else self.current
            from .partial import validate_partial_metadata
            info=validate_partial_metadata(self.dump.metadata.get('partial_dump'),self.dump.data)
            if info['kind']!='mfc':raise ValueError(tr('Продолжение доступно для Classic.'))
            self.info.setText(tr('UID {0}\nПрочитано {1} блоков · отсутствуют {2}\n{3}', f"{info['uid']}", f"{len(info['read_blocks'])}", f"{len(info['missing_blocks'])}", f'{self.path}'))
            self.start.setEnabled(True)
        except (ValueError,OSError) as exc:self.dump=None;self.info.setText(str(exc))

    def browse(self):
        path,_=QFileDialog.getOpenFileName(self,tr('Промежуточный дамп'),'','JSON (*.json)')
        if path:self.sources.addItem(Path(path).name,('file',path));self.sources.setCurrentIndex(self.sources.count()-1)
