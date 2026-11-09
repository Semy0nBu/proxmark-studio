"""Native prepared-write wizard for family-specific card operations."""
from copy import deepcopy
from pathlib import Path
import json
import sqlite3
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QComboBox, QLineEdit,
    QPlainTextEdit, QSpinBox, QCheckBox, QLabel, QPushButton, QTableWidget, QTableWidgetItem,
    QScrollArea, QWidget, QFileDialog)
from .studio_data import say, decode_ndef, structured_dump, import_profile_dump
from .core import parse_hex
from .source_picker import SourcePicker
from .hf_tools_ui import ParameterForm
from .hf_forms import form_arguments
from .operations import CLASSIC_GEOMETRIES
from .presentation import configure_table, install_table_copy
from .theme import mono_font
from .ui import Disclosure
from .wizard_plans import data_block_write, desfire_file_write, magic_user_restore, desfire_source_files
from .studio_flows import restore_hf_profile, HF_DUMP_PROFILES, HF_RESTORE_PROFILES, LF_READERS, clone_lf_profile, magic_uid, ndef_write


OPERATIONS = [
 ('restore:ISO15693 / NFC-V','ISO15693 · восстановить память','ISO15693 · restore memory'),
 ('restore:iCLASS / PicoPass','iCLASS · восстановить диапазон','iCLASS · restore block range'),
 ('restore:LEGIC','LEGIC · восстановить данные','LEGIC · restore data'),
 ('block:MIFARE Plus','MIFARE Plus · пользовательский блок','MIFARE Plus · user-data block'),
 ('desfire','DESFire · изменить файл данных','DESFire · edit a data file'),
 ('block:FeliCa','FeliCa · блок открытого сервиса','FeliCa · unencrypted service block'),
 ('block:Topaz','Topaz · пользовательский блок','Topaz · user-data block'),
 ('magic:Gen1a','Magic Gen1a · пользовательские блоки','Magic Gen1a · user-data blocks'),
 ('magic:Gen4 GTU','Magic Gen4 GTU · пользовательские блоки','Magic Gen4 GTU · user-data blocks'),
 ('uid','Magic Gen1a · UID / ATQA / SAK','Magic Gen1a · UID / ATQA / SAK'),
 ('ndef','Ultralight / NTAG · NDEF','Ultralight / NTAG · NDEF'),
] + [('lf:'+name, name+' → T5577',name+' → T5577') for name in LF_READERS]


class FamilyWizard(QDialog):
    def __init__(self, owner, profile='', source=None):
        super().__init__(owner);self.owner=owner;self.gate=None;self.outcome=None;self.closing=False;self.last_session=None
        self.setWindowTitle(say('Мастер операций с картой','Card operation wizard'));self.resize(1000,820);self.setMinimumSize(720,540)
        outer=QVBoxLayout(self);self.steps=QLabel(say('Источник → копия назначения → план → запись → проверка','Source → target backup → plan → write → verify'));self.steps.setWordWrap(True);outer.addWidget(self.steps)
        scroll=self.scroll=QScrollArea();scroll.setWidgetResizable(True);body=QWidget();layout=QVBoxLayout(body);outer.addWidget(scroll,1);scroll.setWidget(body)
        self.configuration=QWidget();config=QVBoxLayout(self.configuration);config.setContentsMargins(0,0,0,0)
        self.config_disclosure=Disclosure(say('Параметры операции','Operation settings'),expanded=True)
        self.config_disclosure.body.addWidget(self.configuration);layout.addWidget(self.config_disclosure)
        self.operation=QComboBox()
        for key,ru,en in OPERATIONS:self.operation.addItem(say(ru,en),key)
        config.addWidget(self.operation)
        self.sources=SourcePicker(owner,parser=self.import_source);config.addWidget(self.sources)
        self.target=QLineEdit();self.target.setPlaceholderText(say('Необязательно: UID / IDm назначения','Optional: destination UID / IDm'));self.target.setFont(mono_font());config.addWidget(self.target)
        self.form_host=QVBoxLayout();config.addLayout(self.form_host);self.form_widget=None
        self.hint=QLabel();self.hint.setWordWrap(True);config.addWidget(self.hint)
        self.plan_heading=QLabel(say('Сначала проверим карту и сохраним резервную копию.','First, check the card and save its backup.'));self.plan_heading.setWordWrap(True);layout.addWidget(self.plan_heading)
        self.plan=QTableWidget(0,3);self.plan.setHorizontalHeaderLabels([say('Область','Region'),say('Было','Before'),say('Будет','After')])
        configure_table(self.plan,[150,330,330]);install_table_copy(self.plan);self.plan.setMinimumHeight(170);layout.addWidget(self.plan,1)
        self.details=Disclosure(say('Отчёт и файлы','Report and files'));self.report=QPlainTextEdit();self.report.setReadOnly(True);self.report.setFont(mono_font());self.report.setMaximumHeight(160);self.details.body.addWidget(self.report);layout.addWidget(self.details)
        self.save_result=QCheckBox(say('Сохранить проверенный результат в библиотеку','Save the verified result to the library'));self.save_result.setChecked(True);outer.addWidget(self.save_result)
        self.status=QLabel(say('Выберите операцию и нажмите «Подготовить».','Choose an operation and select Prepare.'));self.status.setWordWrap(True);outer.addWidget(self.status)
        buttons=QHBoxLayout();outer.addLayout(buttons)
        self.prepare=QPushButton(say('Подготовить','Prepare'));self.commit=QPushButton(say('Записать по плану','Write prepared plan'));self.commit.setObjectName('primary');self.commit.setEnabled(False)
        self.stop=QPushButton(say('Остановить','Stop'));self.stop.setEnabled(False)
        self.files=QPushButton(say('Файлы…','Files…'));self.close_button=QPushButton(say('Закрыть','Close'))
        self.reset=QPushButton(say('Питание сброшено…','Power was reset…'));self.reset.hide()
        for b in (self.prepare,self.commit,self.stop,self.files,self.close_button):buttons.addWidget(b)
        outer.addWidget(self.reset)
        self.prepare.clicked.connect(lambda:self.guard(self.prepare_operation));self.commit.clicked.connect(lambda:self.guard(self.approve))
        self.stop.clicked.connect(owner.cancel_operation);self.close_button.clicked.connect(self.close);self.reset.clicked.connect(owner.acknowledge_studio_reset)
        self.files.clicked.connect(self.open_files);self.operation.currentIndexChanged.connect(self.operation_changed)
        self.sources.changed.connect(self.source_changed);self.operation_changed()
        match=next((key for key,_,_ in OPERATIONS if key in {'restore:'+profile,'block:'+profile,'lf:'+profile}),None)
        if profile=='DESFire':match='desfire'
        if profile=='Magic Gen1a':match='uid'
        if match:self.operation.setCurrentIndex(self.operation.findData(match))
        if source is not None:
            token='selected-snapshot.json';self.sources.files[token]=deepcopy(source)
            self.sources.cards.addItem(say('Выбранный снимок','Selected snapshot'),('file',token));self.sources.cards.setCurrentIndex(self.sources.cards.count()-1)
        self.sync()

    def guard(self,fn):
        try:return fn()
        except (ValueError,OSError,KeyError,TypeError,sqlite3.Error) as exc:self.status.setText(str(exc))

    def owned(self):
        return bool(self.owner.read_job and self.owner.read_job.get('on_review')==self.show_review)

    def sync(self):
        owned=self.owned();job=self.owner.read_job if owned else None
        other=self.owner.runner.busy or self.owner.read_job is not None or self.owner.antenna_job is not None or self.owner.firmware_busy
        self.configuration.setEnabled(not owned);self.prepare.setEnabled(not other)
        self.commit.setEnabled(bool(owned and job['stage']=='review' and self.gate and not self.owner.runner.busy))
        self.stop.setEnabled(owned and not job['stage'].startswith('cleanup_'))
        self.save_result.setEnabled(not owned);self.reset.setVisible(bool(owned and job['stage']=='uncertain'))
        if owned and job['stage']=='uncertain':self.status.setText(self.owner.studio.status.text())
        elif owned and job['stage'].startswith('cleanup_'):self.status.setText(say('Выключаем поле и проверяем отклик платы…','Turning RF off and checking the device…'))

    def line(self,form,name,title,value=''):
        w=QLineEdit(value);w.setFont(mono_font());form.addRow(title,w);setattr(self,name,w);return w

    def spin(self,form,name,title,first,last,value=0):
        w=QSpinBox();w.setRange(first,last);w.setValue(value);form.addRow(title,w);setattr(self,name,w);return w

    def combo(self,form,name,title,values):
        w=QComboBox();w.addItems(values);form.addRow(title,w);setattr(self,name,w);return w

    def operation_changed(self):
        self.gate=None;self.commit.setEnabled(False);self.plan.setRowCount(0)
        self.config_disclosure.setExpanded(True)
        if self.form_widget:self.form_host.removeWidget(self.form_widget);self.form_widget.hide();self.form_widget.deleteLater()
        self.form_widget=QWidget();layout=QVBoxLayout(self.form_widget);layout.setContentsMargins(0,0,0,0);form=QFormLayout();layout.addLayout(form);self.form_host.addWidget(self.form_widget)
        op=self.operation.currentData();self.sources.setVisible(op.startswith(('restore:','magic:','lf:','block:')) or op=='desfire')
        self.target.setVisible(not op.startswith('lf:'))
        if op.startswith('restore:'):
            family=op.split(':',1)[1]
            self.read_form=ParameterForm(HF_DUMP_PROFILES[family][1],lambda:None);self.write_form=ParameterForm(HF_RESTORE_PROFILES[family],lambda:None)
            for title,widget in [(say('Параметры резервного чтения','Backup read settings'),self.read_form),(say('Параметры восстановления','Restore settings'),self.write_form)]:
                group=Disclosure(title);group.body.addWidget(widget);layout.addWidget(group)
            for widget in (self.read_form,self.write_form):
                for key in ('file','filename','f','uid'):
                    field=widget.fields.get(key)
                    if isinstance(field,QLineEdit):field.setText('');field.setReadOnly(True);field.setPlaceholderText(say('Управляется мастером','Managed by the wizard'))
                    if key in widget.file_buttons:widget.file_buttons[key].setEnabled(False)
            if family=='iCLASS / PicoPass':
                self.write_form.fields['first'].setText('6');self.write_form.fields['last'].setText('31')
            if 'ob' in self.write_form.fields:self.write_form.fields['ob'].setEnabled(False)
            if family=='LEGIC':
                for name in ('de','deobfuscate'):
                    if name in self.read_form.fields:self.read_form.fields[name].setEnabled(False)
            hint=say('Сравнивается выбранная память целиком. LEGIC сохраняет первые 7 служебных байтов назначения.','The selected memory is compared in full. LEGIC preserves the first 7 destination service bytes.')
        elif op.startswith('block:') or op=='desfire':
            family=op.split(':',1)[1] if ':' in op else 'DESFire'
            if op=='desfire':
                self.line(form,'aid','AID HEX','000001');self.line(form,'fid','File ID HEX','01');self.spin(form,'address',say('Смещение в файле','File offset'),0,0xffffff)
                self.source_files=QComboBox();self.source_files.currentIndexChanged.connect(lambda:self.guard(self.select_source_file));form.addRow(say('Файл из дампа','File from dump'),self.source_files)
                self.combo(form,'algo',say('Алгоритм','Algorithm'),['AES','DES','2TDEA','3TDEA'])
                self.spin(form,'keyno',say('Номер ключа','Key number'),0,13)
                self.combo(form,'channel',say('Канал','Channel'),['ev1','ev2','d40','lrp'])
                self.combo(form,'cmode',say('Обмен','Communication'),['plain','mac','encrypt'])
                self.no_auth=QCheckBox(say('Без аутентификации','No authentication'));form.addRow(self.no_auth)
            else:
                first,last=(2,12) if family=='Topaz' else (0,13) if family=='FeliCa' else (1,255)
                self.spin(form,'address',say('Блок назначения','Destination block'),first,last,2 if family=='Topaz' else 1)
            if family in {'MIFARE Plus','DESFire'}:
                self.line(form,'key',say('Текущий ключ HEX','Current key HEX'),'00'*16 if family=='DESFire' else 'FF'*16)
            if family=='MIFARE Plus':
                self.key_b=QCheckBox(say('Ключ B','Key B'));form.addRow(self.key_b)
            if family=='FeliCa':self.line(form,'service',say('Сервис: байты в порядке передачи','Service: on-wire byte order'),'0900')
            self.payload=QPlainTextEdit();self.payload.setFont(mono_font());self.payload.setMaximumHeight(110);self.payload.setPlaceholderText('HEX');layout.addWidget(self.payload)
            row=QHBoxLayout();layout.addLayout(row)
            self.source_unit=QSpinBox();self.source_unit.setRange(0,255);self.source_unit.setValue(2 if family=='Topaz' else 1)
            if op!='desfire':row.addWidget(QLabel(say('Блок источника','Source block')));row.addWidget(self.source_unit)
            copy=QPushButton(say('Данные из снимка','Data from snapshot'));copy.clicked.connect(lambda:self.guard(self.copy_source));row.addWidget(copy)
            load=QPushButton(say('Данные из BIN…','Data from BIN…'));load.clicked.connect(lambda:self.guard(self.load_payload));row.addWidget(load);row.addStretch()
            hints={
                'MIFARE Plus':say('Нужен текущий AES-ключ. UID, ключи и условия доступа назначения сохраняются.','The current AES key is required. Destination UID, keys and access conditions are preserved.'),
                'Topaz':say('Пользовательские блоки 2–12 по 8 байт. UID, CC, lock и OTP сохраняются.','User blocks 2–12, 8 bytes each. UID, CC, lock and OTP are preserved.'),
                'FeliCa':say('Открытый сервис, пользовательские блоки 0–13 по 16 байт. Проверяется IDm.','Unencrypted service, user blocks 0–13, 16 bytes each. IDm is verified.'),
                'DESFire':say('Standard/Backup Data до 64 КиБ, порциями по 512 байт. Копия и проверка всего файла. Записанные порции при отмене не откатываются. Нужен стабильный UID.','Standard/Backup Data up to 64 KiB in 512-byte chunks. Full-file backup/readback. Written chunks are not rolled back on cancellation. A stable UID is required.')}
            hint=hints[family]
        elif op.startswith('magic:'):
            self.line(form,'password',say('Пароль GTU HEX','GTU password HEX'),'00000000')
            self.password.setEnabled(op.endswith('Gen4 GTU'))
            hint=say('Переносятся только пользовательские блоки. UID/BCC, ключи и условия доступа назначения сохраняются.','Only user blocks are transferred. Destination UID/BCC, keys and access conditions are preserved.')
        elif op=='uid':
            self.combo(form,'geometry',say('Геометрия','Geometry'),list(CLASSIC_GEOMETRIES))
            self.geometry.setCurrentText('1k');self.line(form,'new_uid','UID HEX');self.line(form,'atqa','ATQA HEX','0004');self.line(form,'sak','SAK HEX','08')
            hint=say('Gen1a: положительное определение поколения, полный backup и проверка UID/ATQA/SAK. Длина UID сохраняется.','Gen1a: positive generation detection, full backup, UID/ATQA/SAK verification. UID length is preserved.')
        elif op=='ndef':
            self.line(form,'ndef_key',say('Пароль / ключ HEX','Password / key HEX'))
            self.payload=QPlainTextEdit();self.payload.setFont(mono_font());self.payload.setMaximumHeight(120);layout.addWidget(self.payload)
            raw=getattr(self.owner.studio,'ndef',b'')
            if raw:self.payload.setPlainText(raw.hex(' ').upper())
            load=QPushButton(say('Сообщение из BIN…','Message from BIN…'));load.clicked.connect(lambda:self.guard(self.load_payload));layout.addWidget(load)
            hint=say('Копия и проверка NDEF-сообщения, не всей памяти чипа. Защищённые страницы требуют подходящего ключа.','Backup/readback covers the NDEF message, not the entire chip. Protected pages require an appropriate key.')
        else:
            if op=='lf:EM410x':self.combo(form,'clock',say('Тактовый делитель','Clock divisor'),['64','32'])
            hint=say('T5577 без пароля: backup page 0 и заводской page 1; проверка ID, заводских и посторонних блоков. Параметры LF берутся из выбранной записи.','Unprotected T5577: page 0 and factory page 1 backup; ID, factory and unrelated-block verification. LF parameters come from the selected record.')
        self.hint.setText(hint)
        self.source_changed()

    def source_changed(self):
        if not self.owned():self.gate=None;self.commit.setEnabled(False)
        if self.operation.currentData()=='desfire':
            self.source_files.blockSignals(True);self.source_files.clear()
            self.source_files.addItem(say('Выберите файл из дампа','Choose a file from the dump'),None)
            try:
                if self.sources.dump is not None:
                    for file in desfire_source_files(self.sources.dump):
                        self.source_files.addItem(file['aid']+' / '+file['fid']+f" · {len(file['data'])} B",file)
            except (ValueError,KeyError):pass
            self.source_files.blockSignals(False)

    def select_source_file(self):
        file=self.source_files.currentData()
        if file is None:return
        self.aid.setText(file['aid']);self.fid.setText(file['fid']);self.address.setValue(0)
        self.payload.setPlainText(file['data'].hex(' ').upper())

    def source(self):
        if self.sources.dump is None:raise ValueError(say('Выберите снимок источника.','Choose a source snapshot.'))
        return deepcopy(self.sources.dump)

    def import_source(self,path):
        op=self.operation.currentData()
        profile=op.split(':',1)[1] if ':' in op else 'DESFire' if op=='desfire' else 'NDEF'
        return import_profile_dump(path,profile)

    def load_payload(self):
        name,_=QFileDialog.getOpenFileName(self,say('Данные','Data'),'','BIN (*.bin)')
        if name:
            path=Path(name)
            if path.stat().st_size>65536:raise ValueError('Payload exceeds 64 KiB.')
            self.payload.setPlainText(path.read_bytes().hex(' ').upper())

    def copy_source(self):
        dump=self.source();doc=dump.metadata.get('structured_document',{})
        if self.operation.currentData()=='desfire':
            if self.source_files.currentIndex()==0 and self.source_files.count()==2:self.source_files.setCurrentIndex(1)
            if self.source_files.currentData() is None:raise ValueError(say('Выберите читаемый файл из списка либо загрузите его BIN.','Choose a readable file from the list or load its BIN.'))
            self.select_source_file();return
        elif isinstance(doc.get('block_data'),str):raw=parse_hex(doc['block_data'])
        else:
            if doc:raise ValueError(say('Это структурированный документ. Выберите плоский дамп блока или BIN.','This is a structured document. Choose a flat block dump or BIN.'))
            unit=8 if self.operation.currentData()=='block:Topaz' else 16;offset=self.source_unit.value()*unit
            if offset+unit>len(dump.data):raise ValueError('Source block is outside the snapshot.')
            known=dump.metadata.get('partial_dump',{}).get('known_bytes')
            if known is not None and not all(i in set(known) for i in range(offset,offset+unit)):raise ValueError('Source block contains unread placeholders.')
            raw=dump.data[offset:offset+unit]
        self.payload.setPlainText(raw.hex(' ').upper())

    def build_flow(self):
        op=self.operation.currentData();target=self.target.text().replace(' ','').upper()
        if op.startswith('restore:'):
            profile=op.split(':',1)[1];read_values=self.read_form.values();write_values=self.write_form.values()
            for key in ('file','filename','f','uid'):
                read_values.pop(key,None);write_values.pop(key,None)
            for key in ('file','filename','f'):
                if key in self.write_form.fields:write_values[key]='__studio_source__.json'
            return restore_hf_profile(self.source(),profile,form_arguments(HF_DUMP_PROFILES[profile][1],read_values),
                form_arguments(HF_RESTORE_PROFILES[profile],write_values),review=True,target_uid=target)
        if op.startswith('block:'):
            family=op.split(':',1)[1]
            return data_block_write(family,self.address.value(),parse_hex(self.payload.toPlainText()),key=self.key.text().strip() if family=='MIFARE Plus' else '',
                key_b=self.key_b.isChecked() if family=='MIFARE Plus' else False,service=self.service.text().strip() if family=='FeliCa' else '0900',target_uid=target)
        if op=='desfire':return desfire_file_write(self.aid.text().strip(),self.fid.text().strip(),self.address.value(),parse_hex(self.payload.toPlainText()),
            key=self.key.text().strip(),algo=self.algo.currentText(),keyno=self.keyno.value(),channel=self.channel.currentText(),cmode=self.cmode.currentText(),no_auth=self.no_auth.isChecked(),target_uid=target)
        if op.startswith('magic:'):return magic_user_restore(self.source(),op.split(':',1)[1],self.password.text().strip(),target_uid=target)
        if op=='uid':return magic_uid(self.new_uid.text().strip(),self.atqa.text().strip(),self.sak.text().strip(),self.geometry.currentText(),review=True,target_uid=target)
        if op=='ndef':
            raw=parse_hex(self.payload.toPlainText());decode_ndef(raw)
            return ndef_write(raw,self.ndef_key.text().strip(),review=True,target_uid=target)
        dump=self.source();fields=deepcopy(dump.metadata.get('structured_document',{}));name=op.split(':',1)[1]
        if name=='EM410x' and len(dump.data)==5:fields={'protocol':name,'identifier':dump.data.hex().upper()}
        if fields.get('protocol')!=name:raise ValueError('The LF record does not match the selected profile.')
        if name=='EM410x':fields['clock']=int(self.clock.currentText())
        return clone_lf_profile(fields,review=True)

    def prepare_operation(self):
        self.owner._lab_available();flow=self.build_flow();self.gate=None;self.outcome=None;self.plan.setRowCount(0)
        self.status.setText(say('Проверяем карту и читаем резервную копию…','Checking the card and reading its backup…'))
        self.save_requested=self.save_result.isChecked()
        self.owner.start_studio(flow,self.operation.currentText(),rf=True,on_review=self.show_review,on_finished=self.operation_finished)
        if self.owned():self.last_session=self.owner.current_session
        else:self.status.setText(self.owner.studio.status.text())
        self.sync()

    def show_review(self,gate):
        self.gate=gate;self.plan.setRowCount(len(gate.rows));self.plan.setSortingEnabled(False)
        self.config_disclosure.setExpanded(False)
        for i,row in enumerate(gate.rows):
            for j,key in enumerate(('address','before','after')):
                item=QTableWidgetItem(str(row[key]));item.setToolTip(str(row[key]));item.setFont(mono_font())
                if j:item.setForeground(QColor('#e9bc68' if j==1 else '#7bd6ad'))
                self.plan.setItem(i,j,item)
        self.plan_heading.setText(gate.title+' · '+gate.uid+'\n'+gate.scope)
        self.report.setPlainText(say('Резервная копия: ','Backup: ')+gate.backup+'\nSHA256: '+gate.digest)
        self.status.setText(say('Копия сохранена. Проверьте изменения и нажмите «Записать по плану».','Backup saved. Review the changes and select Write prepared plan.'))
        self.scroll.verticalScrollBar().setValue(0)
        self.sync()

    def approve(self):
        if not self.gate or not self.owned():raise ValueError('Prepare the operation first.')
        self.status.setText(say('Проверяем назначение заново, затем записываем…','Rechecking the target, then writing…'))
        self.owner.approve_studio_review(self.gate.digest);self.sync()

    def operation_finished(self,outcome):
        self.outcome=outcome;self.last_session=outcome['session'];self.commit.setEnabled(False)
        message=(say('Готово: записанные данные проверены чтением обратно.','Done: written data was verified by readback.') if outcome['status']=='completed' else
                 say('Остановлено. Резервная копия и журнал сохранены.','Stopped. Backup and log are saved.') if outcome['status']=='cancelled' else str(outcome['error']))
        result=outcome['result']
        if outcome['status']=='completed' and 'ndef' in result and 'dump' not in result:
            result.update(protocol='NDEF',dump=structured_dump({'records':decode_ndef(result['ndef']),'ndef_hex':result['ndef'].hex()},
                'NDEF',result.get('uid',''),'ndef-message'))
        if outcome['status']=='completed' and not outcome['may_have_written']:message=say('Данные уже совпадают. Запись не понадобилась.','Data already matches. No write was needed.')
        if outcome['status']=='completed' and self.save_requested and result.get('dump') is not None:
            try:self.owner.studio.save_result(result)
            except (ValueError,OSError,sqlite3.Error) as exc:message+='\n'+say('Не удалось добавить результат в библиотеку: ','Could not add the result to the library: ')+str(exc)
        self.status.setText(message);self.report.appendPlainText(json.dumps({'status':outcome['status'],'verification':result.get('verification',''),
            'may_have_written':outcome['may_have_written'],'error':outcome['error']},ensure_ascii=False,indent=2));self.sync()
        if self.closing:QTimer.singleShot(0,self.accept)

    def open_files(self):
        path=self.last_session or self.owner.data_dir/'sessions';QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def closeEvent(self,event):
        if self.owned():self.closing=True;self.owner.cancel_operation();event.ignore();return
        event.accept()

    def reject(self):
        if self.owned():self.closing=True;self.owner.cancel_operation();return
        super().reject()
