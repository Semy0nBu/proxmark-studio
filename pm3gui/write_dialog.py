"""Native one-card autowrite wizard and portable source snapshots."""
from .i18n import tr, display_revision_note
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from PySide6.QtCore import Qt,QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox,QComboBox,QDialog,QFileDialog,QFormLayout,QHBoxLayout,
    QLabel,QLineEdit,QPlainTextEdit,QProgressBar,QPushButton,QSpinBox,QTableWidget,QTableWidgetItem,QVBoxLayout,QScrollArea,QWidget)
from .core import DumpData,parse_dump,parse_hex
from .keys import format_key_timestamp
from .writing import WriteOptions,source_family
from .write_scenario import load_scenario,save_scenario
from .theme import mono_font
from .ui import Disclosure
from .presentation import configure_table,install_table_copy
from .transfer_ui import TransferPanel
from .transfer import hf_checks



from .ui import SoftButton

class WriteDialog(QDialog):
    def __init__(self,owner,dump=None):
        super().__init__(owner)
        self.owner,self.dump,self.plan=owner,None,None
        self._close_requested=False;self.report_directory=None
        self._editor=deepcopy(dump);self._files={};self._loading=True;self.source_record={}
        self._editor_record={'name':owner.card_name.text(),'uid':owner.uid.text(),'protocol':owner.protocol.text()}
        self.setWindowTitle(tr('Автозапись HF-карты'));self.setModal(True)
        screen=owner.screen().availableGeometry();self.resize(min(850,screen.width()-40),min(730,screen.height()-60))
        outer=QVBoxLayout(self);outer.setSpacing(10)
        content=QWidget();layout=QVBoxLayout(content);layout.setContentsMargins(0,0,0,0);layout.setSpacing(12)
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setWidget(content);outer.addWidget(scroll,1)
        self.steps=QLabel()
        self.steps.setWordWrap(True)
        self.steps.setObjectName('section');layout.addWidget(self.steps);self.steps.hide()
        row=QHBoxLayout();row.addWidget(QLabel(tr('Дамп')))
        self.sources=QComboBox();self.sources.setMinimumContentsLength(16)
        self.sources.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.sources.addItem(tr('Выберите карту из библиотеки'),None)
        if dump is not None:self.sources.addItem(tr('Текущий дамп из редактора'),('editor',''))
        for card in owner.library.list_cards():
            if card['rf_band']=='lf':continue
            self.sources.addItem(card['name'],('card',card['id']))
            self.sources.setItemData(self.sources.count()-1,card['name']+' · '+card['uid'],Qt.ItemDataRole.ToolTipRole)
        row.addWidget(self.sources,1)
        self.file_button=SoftButton(tr('Открыть файл…'));self.file_button.clicked.connect(self.choose_file);row.addWidget(self.file_button)
        layout.addLayout(row)
        row=QHBoxLayout();row.addWidget(QLabel(tr('Версия')))
        self.source_versions=QComboBox();self.source_versions.setMinimumContentsLength(12)
        self.source_versions.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        row.addWidget(self.source_versions,1)
        self.load_button=SoftButton(tr('Открыть сценарий…'));self.load_button.clicked.connect(self.choose_scenario);row.addWidget(self.load_button)
        self.save_scenario_button=SoftButton(tr('Сохранить сценарий…'));self.save_scenario_button.clicked.connect(self.export_scenario);row.addWidget(self.save_scenario_button)
        layout.addLayout(row)
        self.source_info=QLabel(tr('Выберите дамп. Затем положите карту назначения на HF-антенну.'))
        self.source_info.setWordWrap(True);self.source_info.setObjectName('muted');layout.addWidget(self.source_info);self.source_info.hide()
        self.transfer = TransferPanel(owner=owner);layout.addWidget(self.transfer)
        self.family_button=SoftButton(tr('Профильные мастера HF / LF…'))
        self.family_button.clicked.connect(self.open_family);layout.addWidget(self.family_button)
        self.parameters=Disclosure(tr('Параметры записи'))
        form=QFormLayout();self.form=form;form.setSpacing(8)
        self.family=QComboBox();self.family.addItem(tr('Определить по дампу'),'auto')
        self.family.addItem('MIFARE Classic · Mini / 1K / 2K / 4K','mfc')
        self.family.addItem(tr('Ultralight / NTAG · пользовательские страницы'),'mfu');form.addRow(tr('Профиль'),self.family)
        self.key_a=QLineEdit('FFFFFFFFFFFF');self.key_b=QLineEdit('FFFFFFFFFFFF')
        for title,widget in [(tr('Текущий ключ A'),self.key_a),(tr('Текущий ключ B'),self.key_b)]:
            widget.setFont(mono_font());widget.setToolTip(tr('Также используются ключи библиотеки для UID назначения.'));form.addRow(title,widget)
        self.mfu_key=QLineEdit(owner.mfu_auth.text());self.mfu_key.setFont(mono_font())
        self.mfu_key.setPlaceholderText(tr('PWD 8 HEX / ключ 32 HEX · необязательно'));form.addRow(tr('Пароль / ключ'),self.mfu_key)
        self.secure=QCheckBox(tr('Защищённый канал UL-AES'));form.addRow('',self.secure)
        row=QHBoxLayout();self.first,self.last=QSpinBox(),QSpinBox()
        for item in (self.first,self.last):item.setRange(0,255)
        row.addWidget(self.first);row.addWidget(QLabel('—'));row.addWidget(self.last);row.addStretch();form.addRow(tr('Адреса'),row)
        self.target_uid=QLineEdit();self.target_uid.setFont(mono_font());self.target_uid.setClearButtonEnabled(True)
        self.target_uid.setPlaceholderText(tr('Определится при проверке'))
        self.target_uid.setToolTip(tr('Заполните для привязки сценария к конкретной карте. Несовпадающий UID остановит подготовку.'))
        form.addRow(tr('UID назначения'),self.target_uid)
        from .studio_data import say
        self.selected_blocks=QLineEdit();self.selected_blocks.setPlaceholderText('1, 4, 8-11')
        form.addRow(say('Только выбранные блоки','Selected blocks only'),self.selected_blocks)
        self.parameters.body.addLayout(form)
        self.include_keys=QCheckBox(tr('Перенести ключи A/B из дампа'))
        self.include_keys.setToolTip(tr('Условия доступа сохраняются. Нужны известные текущие A/B; поддержаны изменяемые трейлеры 001/011.'))
        self.parameters.body.addWidget(self.include_keys)
        self.include_unknown=QCheckBox(tr('Включить заполнители непрочитанных блоков'))
        self.parameters.body.addWidget(self.include_unknown);layout.addWidget(self.parameters)
        self.review_heading=QLabel(tr('План появится после проверки карты'));self.review_heading.setObjectName('section')
        self.review_heading.setWordWrap(True);layout.addWidget(self.review_heading)
        self.plan_table=QTableWidget(0,4);self.plan_table.setHorizontalHeaderLabels([tr('Адрес'),tr('Было'),tr('Будет'),tr('Проверка')])
        configure_table(self.plan_table,[70,255,255,130]);install_table_copy(self.plan_table)
        self.plan_table.setMinimumHeight(140);layout.addWidget(self.plan_table,1)
        self.details=Disclosure(tr('Отчёт и подробности'))
        self.summary=QPlainTextEdit();self.summary.setReadOnly(True);self.summary.setFont(mono_font());self.summary.setMaximumHeight(150)
        self.details.body.addWidget(self.summary)
        self.files_button=SoftButton(tr('Файлы отчёта и резервной копии…'));self.files_button.setEnabled(False)
        self.files_button.clicked.connect(lambda:QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.report_directory))) if self.report_directory else None)
        self.details.body.addWidget(self.files_button);layout.addWidget(self.details)
        from .card_workspace import RecoveryPanel
        self.recovery_panel=RecoveryPanel(owner,owner.workspace_bridge)
        self.recovery_panel.hide();layout.addWidget(self.recovery_panel)
        self.progress=QProgressBar();self.progress.setTextVisible(False);self.progress.hide();outer.addWidget(self.progress)
        self.status=QLabel(tr('Выберите источник и нажмите «Проверить карту».'))
        self.status.setWordWrap(True);outer.addWidget(self.status)
        row=QHBoxLayout()
        self.prepare_button=SoftButton(tr('Проверить карту'));self.prepare_button.clicked.connect(lambda:owner.prepare_card_write(self))
        self.write_button=SoftButton(tr('Начать автозапись'));self.write_button.setObjectName('primary');self.write_button.setEnabled(False)
        self.write_button.clicked.connect(lambda:owner.approve_card_write(self))
        self.stop=SoftButton(tr('Остановить'));self.stop.clicked.connect(owner.cancel_operation);self.stop.setEnabled(False)
        self.close_button=SoftButton(tr('Закрыть'));self.close_button.clicked.connect(self.reject)
        for widget in (self.prepare_button,self.write_button,self.stop,self.close_button):row.addWidget(widget)
        row.addStretch();outer.addLayout(row)
        for widget in (self.steps,self.source_info,self.review_heading,self.status):widget.setTextFormat(Qt.TextFormat.PlainText)
        self.sources.currentIndexChanged.connect(self._source_selected)
        self.source_versions.currentIndexChanged.connect(self._version_selected)
        self.family.currentIndexChanged.connect(self._family_changed)
        for field in (self.key_a,self.key_b,self.mfu_key,self.target_uid,self.selected_blocks):field.textChanged.connect(self._options_changed)
        for field in (self.secure,self.include_keys,self.include_unknown):field.toggled.connect(self._options_changed)
        for field in (self.first,self.last):field.valueChanged.connect(self._options_changed)
        self._loading=False
        if dump is not None:self.sources.setCurrentIndex(1)
        elif self.sources.count()>1:self.sources.setCurrentIndex(1)
        else:self._set_source(None,'')

    def _source_selected(self,*_):
        if self._loading:return
        self._loading=True;self.source_versions.clear();entry=self.sources.currentData()
        try:self._load_source(entry)
        except (OSError,ValueError,KeyError,TypeError) as exc:
            self._set_source(None,'');self.status.setText(tr('Не удалось открыть дамп: ')+str(exc))
        finally:self._loading=False

    def _load_source(self,entry):
        self.source_record={}
        if not entry:source=None;name=''
        elif entry[0]=='editor':
            self.source_record=dict(self._editor_record)
            source=deepcopy(self._editor);name=self._editor_record['name'];self.source_versions.addItem(tr('Текущие байты редактора'),None)
        elif entry[0]=='file':
            source=deepcopy(self._files[entry[1]]);name=Path(entry[1]).name;self.source_versions.addItem(tr('Из файла'),None)
        else:
            card=self.owner.library.get_card(entry[1]);name=card['name']
            self.source_record=card
            for rev in self.owner.library.list_revisions(entry[1]):
                self.source_versions.addItem(tr('{0} · {1} Б · {2}', f"{format_key_timestamp(rev['created_at'])}", f"{rev['size']}", display_revision_note(rev['note'])),rev['id'])
                self.source_versions.setItemData(self.source_versions.count()-1,self.source_versions.itemText(self.source_versions.count()-1),Qt.ItemDataRole.ToolTipRole)
            source=self.owner.library.get_dump(entry[1],self.source_versions.currentData())
        self._set_source(source,name)

    def _version_selected(self,*_):
        if self._loading:return
        entry=self.sources.currentData()
        if entry and entry[0]=='card' and self.source_versions.currentData():
            try:self._set_source(self.owner.library.get_dump(entry[1],self.source_versions.currentData()),self.owner.library.get_card(entry[1])['name'])
            except (OSError,ValueError,KeyError,TypeError) as exc:
                self._set_source(None,'');self.status.setText(tr('Не удалось открыть версию: ')+str(exc))

    def _set_source(self,source,name):
        self.dump=deepcopy(source);self.source_name=name;self.plan=None
        self.report_directory=None;self.files_button.setEnabled(False)
        self.plan_table.setRowCount(0);self.write_button.setEnabled(False);self.target_uid.clear()
        self.selected_blocks.clear()
        self.family.setCurrentIndex(0)
        self.include_unknown.setChecked(False);self.include_keys.setChecked(False)
        self.include_unknown.setVisible(bool(source and source.metadata.get('partial_dump')))
        self.prepare_button.setEnabled(source is not None);self.save_scenario_button.setEnabled(source is not None)
        self.source_info.setText((tr('{0} · {1} байт', f'{name}', f'{len(source.data)}')+(tr(' · НЕПОЛНЫЙ ДАМП') if source.metadata.get('partial_dump') else '')+
                                 tr('\nUID, условия доступа и служебные страницы сохраняются.')) if source else tr('Выберите дамп из библиотеки или файла.'))
        self.review_heading.setText(tr('План появится после проверки карты'));self._family_changed()
        self.transfer.set_source(self.dump,self.source_record,name)
        self.transfer.reset_target();self._refresh_compatibility()

    def _options_changed(self,*_):
        if self._loading:return
        job=self.owner.read_job
        owned=bool(job and job.get('write_dialog') is self)
        if not owned:
            self.plan=None;self.write_button.setEnabled(False);self.plan_table.setRowCount(0)
            self.transfer.reset_target();self._refresh_compatibility()

    def open_family(self):
        if self.owner.read_job is not None:return
        protocol=self.transfer.facts.protocol
        profile=next((name for name in ('DESFire','MIFARE Plus','FeliCa','Topaz','ISO15693 / NFC-V','iCLASS / PicoPass','LEGIC') if name.lower().split(' / ')[0] in protocol.lower()),'')
        self.owner.open_family_wizard(profile,self.dump,source_record=self.source_record)

    def source_token(self):
        return self.sources.currentData(),self.source_versions.currentData()

    def configuration_unchanged(self,plan,token):
        return (self.plan is plan and self.dump is not None and self.source_token()==token
                and self.dump.data==plan.source.data and self.dump.metadata==plan.source.metadata
                and self.dump.block_size==plan.source.block_size and self.options()==plan.options)

    def _refresh_compatibility(self,plan=None):
        try:options=self.options() if self.dump is not None else None
        except ValueError:options=None
        checks=hf_checks(self.transfer.facts,plan,options)
        self.transfer.show_checks(checks,ready=bool(plan and plan.ready and not plan.error and not plan.done))
        if not self.owner.read_job:self.prepare_button.setEnabled(self.dump is not None and not any(c.state=='blocked' for c in checks))

    def show_preflight(self,plan):
        self.report_directory=plan.directory
        self.files_button.setEnabled(plan.directory.is_dir())
        target=plan.target
        if target and target.uid and not target.ambiguous_identity:
            self.transfer.set_target(getattr(plan,'model','') or target.protocol,target.uid,
                tr('Проверяется только выбранная область; пустота всей карты не устанавливается.'))
        self._refresh_compatibility(plan)
        if plan.approved:self.transfer.set_stage(4 if plan.phase in {'verify','verify_b','keys_identity','verify_identity','final_identity'} else 3)

    def choose_file(self):
        filename,_=QFileDialog.getOpenFileName(self,tr('Дамп для автозаписи'),'',tr('Дампы (*.json *.bin *.mfd *.eml);;Все файлы (*)'))
        if not filename:return
        try:
            source=parse_dump(Path(filename));self._files[filename]=source
            self.sources.addItem(Path(filename).name,('file',filename));self.sources.setCurrentIndex(self.sources.count()-1)
        except (OSError,ValueError) as exc:self.status.setText(str(exc))

    def choose_scenario(self):
        filename,_=QFileDialog.getOpenFileName(self,tr('Сценарий автозаписи'),'',tr('Сценарии (*.pm3write.json);;JSON (*.json)'))
        if not filename:return
        try:
            source,options,name=load_scenario(Path(filename))
            self._files[filename]=source;self.sources.addItem(name,('file',filename));self.sources.setCurrentIndex(self.sources.count()-1)
            self.family.setCurrentIndex(self.family.findData(options.family));self._family_changed()
            self.first.setValue(options.first);self.last.setValue(options.last);self.target_uid.setText(options.target_uid)
            self.selected_blocks.setText(', '.join(map(str,options.blocks)))
            self.include_keys.setChecked(options.include_keys);self.include_unknown.setChecked(options.include_unknown)
            self.secure.setChecked(options.secure_channel)
            self.status.setText(tr('Сценарий загружен. Проверьте карту назначения перед запуском.'))
        except (OSError,ValueError) as exc:self.status.setText(str(exc))

    def export_scenario(self):
        if self.dump is None:return
        filename,_=QFileDialog.getSaveFileName(self,tr('Сохранить сценарий'),self.source_name+'.pm3write.json',tr('Сценарий (*.pm3write.json)'))
        if not filename:return
        try:
            options=self.options()
            if self.plan and self.plan.ready and self.plan.target and not self.plan.error and not options.target_uid:
                options=replace(options,target_uid=self.plan.target.uid)
            save_scenario(Path(filename),self.dump,options,self.source_name)
            self.status.setText(tr('Сценарий сохранён со снимком дампа. Ключи подключения берутся из библиотеки или задаются при запуске.'))
        except (OSError,ValueError) as exc:self.status.setText(str(exc))

    def _family_changed(self,*_,reset_range=True):
        family=self.family.currentData()
        try:classic=(source_family(self.dump) if family=='auto' and self.dump is not None else family)=='mfc'
        except ValueError:classic=False
        for widget,visible in ((self.key_a,classic),(self.key_b,classic),(self.mfu_key,not classic),(self.secure,not classic)):
            self.form.setRowVisible(widget,visible)
        self.key_a.setEnabled(classic);self.key_b.setEnabled(classic);self.include_keys.setEnabled(classic)
        if not classic:self.include_keys.setChecked(False)
        self.mfu_key.setEnabled(not classic);self.secure.setEnabled(not classic)
        if reset_range and self.dump:self.first.setValue(0);self.last.setValue(min(255,len(self.dump.data)//(16 if classic else 4)-1))
        if not self._loading and reset_range:self._options_changed()

    def options(self):
        if self.dump is None:raise ValueError(tr('Выберите дамп для записи.'))
        family=source_family(self.dump) if self.family.currentData()=='auto' else self.family.currentData()
        a=parse_hex(self.key_a.text()).hex().upper() if family=='mfc' else 'FFFFFFFFFFFF'
        b=parse_hex(self.key_b.text()).hex().upper() if family=='mfc' else 'FFFFFFFFFFFF'
        mfu=parse_hex(self.mfu_key.text()).hex().upper() if family=='mfu' else ''
        from .studio_data import block_selection
        blocks=block_selection(self.selected_blocks.text(),min(256,len(self.dump.data)//(16 if family=='mfc' else 4))) if self.selected_blocks.text().strip() else ()
        return WriteOptions(family,a,b,mfu,
                            self.secure.isChecked() if family=='mfu' else False,self.include_keys.isChecked(),
                            self.include_unknown.isChecked(),self.first.value(),self.last.value(),self.target_uid.text().strip(),blocks)

    def set_active(self,active,review=False):
        for widget in (self.sources,self.source_versions,self.file_button,self.load_button,self.family,self.key_a,self.key_b,
                       self.mfu_key,self.secure,self.include_keys,self.include_unknown,self.first,self.last,self.target_uid,self.selected_blocks,self.prepare_button):
            widget.setEnabled(not active)
        self.family_button.setEnabled(not active)
        self.save_scenario_button.setEnabled(self.dump is not None and (not active or review))
        if not active:self._family_changed(reset_range=False)
        self.prepare_button.setEnabled(not active and self.dump is not None)
        self.write_button.setEnabled(review);self.stop.setEnabled(active);self.close_button.setEnabled(not active or review)
        self.progress.setVisible(active and not review)
        if active and not review:self.progress.setRange(0,0)

    def update_progress(self,plan):
        self.steps.setVisible(self.owner.interface_mode!='compact')
        self.steps.setText(tr('Запись и проверка: {0} / {1}',len(plan.verified),len(plan.entries)))
        self.progress.setRange(0,max(1,len(plan.entries)));self.progress.setValue(len(plan.verified))
        for row,entry in enumerate(plan.entries):
            item=self.plan_table.item(row,3)
            if item and entry['block'] in plan.verified:item.setText(tr('Проверен'))

    def show_review(self,plan):
        self.show_preflight(plan)
        self.review_heading.setText(tr('Карта {0} · изменится {1} блоков/страниц', f'{plan.target.uid}', f'{len(plan.entries)}')
            if plan.target else tr('План появится после проверки карты'))
        self.summary.setPlainText(plan.review());self.plan_table.setRowCount(len(plan.entries))
        for row,entry in enumerate(plan.entries):
            state=tr('Проверен') if entry['block'] in plan.verified else tr('Не подтверждён') if entry['block'] in plan.may_have_written else tr('Ожидает')
            for col,value in enumerate((str(entry['block']),entry['old'],entry['new'],state)):
                item=QTableWidgetItem(value);item.setToolTip(value)
                if col in (1,2):item.setFont(mono_font())
                self.plan_table.setItem(row,col,item)

    def reject(self):
        job=self.owner.read_job
        if job and job.get('write_dialog') is self:
            self._close_requested=True
            self.owner.cancel_operation()
            if self.owner.read_job is not None:return
        super().reject()

    def closeEvent(self,event):
        job=self.owner.read_job
        if job and job.get('write_dialog') is self:
            self.reject()
            if self.owner.read_job is not None:event.ignore();return
        super().closeEvent(event)
