"""Native HF actions and parameters, with the full client catalogue available."""
from .i18n import tr
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox,QComboBox,QDialog,QFileDialog,QFormLayout,QHBoxLayout,QLabel,QLineEdit,
    QListWidget,QListWidgetItem,QMessageBox,QPlainTextEdit,QPushButton,QScrollArea,QSpinBox,QSplitter,QVBoxLayout,QWidget)
from .hf_catalog import COMMANDS
from .hf_forms import FAMILIES,action_title,family_title,parameters,form_arguments
from .operations import Operation
from .theme import mono_font
from .ui import Disclosure,line_icon
from .presentation import CardList

READ_ACTIONS={'hf search','hf 14a reader','hf 14a info','hf mf rdbl','hf mf rdsc','hf mfu rdbl','hf mfu info',
              'hf iclass rdbl','hf 15 read','hf 15 rdbl','hf 15 info','hf mfdes info','hf mfdes read'}


class ParameterForm(QWidget):
    def __init__(self,command,changed,parent=None):
        super().__init__(parent)
        self.command,self.fields,self.descriptors=command,{},parameters(command)
        self.file_buttons={}
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0)
        self.form=QFormLayout();self.form.setSpacing(10)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        layout.addLayout(self.form)
        self.extra=Disclosure(tr('Дополнительные параметры'))
        extra=QFormLayout();extra.setSpacing(10)
        extra.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.extra.body.addLayout(extra)
        common={'blk','block','tblk','sec','key','file','filename','uid','id','fc','cn','pwd','data','aid','fid','keyno','algo','cmode','type','offset','kfn'}
        self.key_type=None
        self.target_type=None
        self.target_fields={p.name for p in self.descriptors if p.kind=='bool' and p.name in {'q5','em','hts','htu'}} if command.startswith('lf ') and command.endswith(' clone') else set()
        paired=command in {'hf mf rdbl','hf mf wrbl'} and {'a','b'}<={p.name for p in self.descriptors}
        for field in self.descriptors:
            if field.name in self.target_fields:
                if self.target_type is None:
                    self.target_type=QComboBox();self.target_type.addItem('T55x7 / T5577',None)
                    for name,title in [('q5','Q5 / T5555'),('em','EM4305 / EM4469'),('hts','Hitag S'),('htu','Hitag µ')]:
                        if name in self.target_fields:self.target_type.addItem(title,name)
                    self.target_type.currentIndexChanged.connect(changed);self.form.addRow(tr('Заготовка'),self.target_type)
                continue
            if paired and field.name in {'a','b'}:
                if self.key_type is None:
                    self.key_type=QComboBox();self.key_type.addItem(tr('Ключ A'),'a');self.key_type.addItem(tr('Ключ B'),'b')
                    self.key_type.currentIndexChanged.connect(changed)
                    self.form.addRow(tr('Аутентификация'),self.key_type)
                continue
            primary=field.required or field.name in common or field.name.startswith('positional')
            form=self.form if primary else extra
            if field.kind=='bool':
                control=QCheckBox(field.label);control.toggled.connect(changed);form.addRow('',control)
            elif field.kind=='choice':
                control=QComboBox();control.addItem(tr('По умолчанию'),None)
                for choice in field.choices:control.addItem(choice,choice)
                control.currentIndexChanged.connect(changed)
                form.addRow(field.label+(' *' if field.required else ''),control)
            else:
                control=QPlainTextEdit() if field.repeated else QLineEdit()
                if field.repeated:
                    control.setMaximumHeight(78);control.setPlaceholderText(tr('По одному значению в строке'))
                else:control.setPlaceholderText(tr('Обязательно') if field.required else tr('Необязательно'))
                if field.kind=='hex':
                    control.setFont(mono_font())
                    if not field.repeated:control.setPlaceholderText('HEX · '+(tr('обязательно') if field.required else tr('необязательно')))
                control.textChanged.connect(changed)
                title=QLabel(field.label+(' *' if field.required else ''));title.setWordWrap(True);title.setMaximumWidth(190)
                title.setMinimumWidth(min(190,title.fontMetrics().horizontalAdvance(title.text())+4))
                if field.kind=='file':
                    row=QHBoxLayout();row.addWidget(control,1)
                    browse=QPushButton(tr('Выбрать…'));browse.clicked.connect(lambda _,f=field,w=control:self.browse(f,w))
                    self.file_buttons[field.name]=browse
                    row.addWidget(browse);form.addRow(title,row)
                else:form.addRow(title,control)
            control.setToolTip(field.description);control.setAccessibleName(field.label)
            self.fields[field.name]=control
        if extra.rowCount():layout.addWidget(self.extra)
        else:self.extra.hide()
        if not self.descriptors:
            empty=QLabel(tr('Дополнительных полей нет. Параметры старых команд доступны в расширенном редакторе.'))
            empty.setWordWrap(True);empty.setObjectName('muted');layout.addWidget(empty)
        layout.addStretch(1)

    def browse(self,field,control):
        output=self.command.split()[-1] in {'dump','save','esave'}
        chooser=QFileDialog.getSaveFileName if output else QFileDialog.getOpenFileName
        filename,_=chooser(self,tr('Файл для действия'),control.text() or '', tr('Все файлы (*)'))
        if filename:control.setText(filename)

    def values(self):
        values={}
        for name,widget in self.fields.items():
            if isinstance(widget,QCheckBox):values[name]=widget.isChecked()
            elif isinstance(widget,QComboBox):values[name]=widget.currentData()
            elif isinstance(widget,QPlainTextEdit):values[name]=widget.toPlainText().splitlines()
            else:values[name]=widget.text()
        if self.key_type is not None:values.update(a=self.key_type.currentData()=='a',b=self.key_type.currentData()=='b')
        if self.target_type is not None:values.update({name:self.target_type.currentData()==name for name in self.target_fields})
        return values

    def arguments(self):return form_arguments(self.command,self.values())


class HFToolsDialog(QDialog):
    def __init__(self,owner,band='hf'):
        super().__init__(owner)
        self.owner=owner;self.parameter_form=None;self._busy=False;self._changing=False;self.band=band
        if band=='lf':
            from .lf_catalog import COMMANDS as commands
        else:commands=COMMANDS
        self.catalog=commands
        self.setWindowTitle(tr('Инструменты ')+band.upper())
        screen=owner.screen().availableGeometry();self.resize(min(1080,screen.width()-40),min(760,screen.height()-60))
        layout=QVBoxLayout(self);layout.setSpacing(12)
        quick=QHBoxLayout();title=QLabel(tr('Действия с ')+band.upper()+tr('-картой'));title.setObjectName('section');quick.addWidget(title,1)
        self.auto_write_button=QPushButton(tr('Автозапись дампа…'));self.auto_write_button.setIcon(line_icon('cards'))
        self.auto_write_button.clicked.connect(self.open_autowrite);quick.addWidget(self.auto_write_button);layout.addLayout(quick)
        self.auto_write_button.setVisible(band=='hf')
        row=QHBoxLayout();self.group=QComboBox();self.group.addItem(tr('Все семейства'),'')
        for name in sorted({c.split()[1] for c in self.catalog}):self.group.addItem(FAMILIES.get(name,name.upper()),name)
        row.addWidget(self.group);self.search=QLineEdit();self.search.setPlaceholderText(tr('Найти действие, семейство или команду'))
        row.addWidget(self.search,1);self.writes_only=QCheckBox(tr('Запись и изменение'));row.addWidget(self.writes_only);layout.addLayout(row)
        splitter=QSplitter(Qt.Orientation.Horizontal);splitter.setChildrenCollapsible(False)
        self.commands=CardList();self.commands.setMinimumWidth(210);self.commands.setWordWrap(True);splitter.addWidget(self.commands)
        right=QWidget();body=QVBoxLayout(right);body.setContentsMargins(10,0,0,0)
        self.action_heading=QLabel();self.action_heading.setObjectName('section');self.action_heading.setWordWrap(True);body.addWidget(self.action_heading)
        self.action_context=QLabel();self.action_context.setObjectName('muted');body.addWidget(self.action_context)
        self.form_scroll=QScrollArea();self.form_scroll.setWidgetResizable(True);body.addWidget(self.form_scroll,1)
        self.advanced=Disclosure(tr('Команда и справка'))
        self.manual=QCheckBox(tr('Ввести параметры вручную'));self.manual.toggled.connect(self._manual_changed);self.advanced.body.addWidget(self.manual)
        self.args=QLineEdit();self.args.setFont(mono_font());self.args.setEnabled(False);self.args.setPlaceholderText(tr('Параметры командной строки'))
        self.advanced.body.addWidget(self.args)
        self.preview=QLineEdit();self.preview.setReadOnly(True);self.preview.setFont(mono_font());self.advanced.body.addWidget(self.preview)
        self.help=QPlainTextEdit();self.help.setReadOnly(True);self.help.setFont(mono_font());self.help.setMaximumHeight(180)
        self.advanced.body.addWidget(self.help);body.addWidget(self.advanced)
        self.status=QLabel(tr('Выберите действие. Поля и справка работают без подключения.'))
        self.status.setWordWrap(True);self.status.setObjectName('muted');body.addWidget(self.status)
        for widget in (self.action_heading,self.action_context,self.status):widget.setTextFormat(Qt.TextFormat.PlainText)
        row=QHBoxLayout();self.timeout=QSpinBox();self.timeout.setRange(5,1800);self.timeout.setValue(120);self.timeout.setSuffix(tr(' с'))
        self.timeout.setToolTip(tr('Предельное время выполнения'));row.addWidget(QLabel(tr('Тайм-аут')));row.addWidget(self.timeout);row.addStretch()
        self.run_button=QPushButton(tr('Выполнить'));self.run_button.setObjectName('primary');self.run_button.clicked.connect(self.run_command)
        self.stop_button=QPushButton(tr('Остановить'));self.stop_button.clicked.connect(owner.cancel_operation);self.stop_button.setEnabled(False)
        row.addWidget(self.run_button);row.addWidget(self.stop_button);body.addLayout(row)
        splitter.addWidget(right);splitter.setSizes([315,720]);layout.addWidget(splitter,1)
        self.group.currentIndexChanged.connect(self.filter_commands);self.search.textChanged.connect(self.filter_commands)
        self.writes_only.toggled.connect(self.filter_commands);self.commands.currentItemChanged.connect(self.selected)
        self.args.textChanged.connect(self.update_preview);self.filter_commands()

    def open_autowrite(self):self.hide();self.owner.open_write_dialog()

    def filter_commands(self):
        self.commands.clear();query=self.search.text().casefold();group=self.group.currentData()
        for name,row in self.catalog.items():
            title=action_title(name);family=family_title(name)
            if group and name.split()[1]!=group:continue
            if query and query not in (name+' '+title+' '+family+' '+row['description']).casefold():continue
            if self.writes_only.isChecked() and not any(w in (name+' '+row['description']).lower() for w in
                ('write','restore','clone','set','wipe','erase','format','change','create','delete','cload','eload','config','raw')):continue
            item=QListWidgetItem(title+'\n'+family);item.setData(Qt.ItemDataRole.UserRole,name)
            item.setToolTip(name+'\n'+row['description']);self.commands.addItem(item)
        if self.commands.count():self.commands.setCurrentRow(0)
        else:self.selected(None)
        self.commands.fit_items()

    def selected(self,item,*_):
        self._changing=True;command=item.data(Qt.ItemDataRole.UserRole) if item else None
        self.help.setPlainText(self.catalog[command]['help'] if command else '')
        self.action_heading.setText(action_title(command) if command else tr('По этому запросу действий нет'))
        title=action_title(command) if command else tr('Выполнить')
        self.run_button.setText(title if len(title)<=25 else tr('Выполнить'))
        self.action_context.setText(family_title(command) if command else '')
        old=self.form_scroll.takeWidget()
        if old:old.deleteLater()
        self.parameter_form=ParameterForm(command,self.update_preview) if command else None
        if self.parameter_form:self.form_scroll.setWidget(self.parameter_form)
        self.manual.setChecked(False);self.args.clear();self._changing=False;self.update_preview()

    def _manual_changed(self,manual):
        self.args.setEnabled(manual and not self._busy)
        if self.parameter_form:self.parameter_form.setEnabled(not manual and not self._busy)
        self.update_preview()

    def operation(self):
        item=self.commands.currentItem()
        if not item:raise ValueError(tr('Выберите действие ')+self.band.upper()+'.')
        args=self.args.text() if self.manual.isChecked() else self.parameter_form.arguments()
        command=item.data(Qt.ItemDataRole.UserRole)
        op=(Operation('lf_tool',lf_command=command,lf_args=args) if self.band=='lf' else Operation('hf_tool',hf_command=command,hf_args=args))
        op.render();return op

    def update_preview(self,*_):
        if self._changing:return
        try:
            op=self.operation();self.preview.setText(op.render());self.status.setText(tr('Параметры готовы к выполнению.'))
            self.run_button.setEnabled(not self._busy)
        except ValueError as exc:self.preview.setText('');self.status.setText(str(exc));self.run_button.setEnabled(False)

    def set_busy(self,busy):
        self._busy=busy
        for widget in (self.commands,self.group,self.search,self.writes_only,self.timeout,self.manual,self.auto_write_button):widget.setEnabled(not busy)
        self.args.setEnabled(not busy and self.manual.isChecked())
        if self.parameter_form:self.parameter_form.setEnabled(not busy and not self.manual.isChecked())
        self.stop_button.setEnabled(busy and not self.owner.firmware_busy);self.update_preview()

    def run_command(self):
        try:
            if self.owner.runner.busy or self.owner.read_job is not None or self.owner.antenna_job is not None or self.owner.firmware_busy:
                raise ValueError(tr('Завершите текущую операцию.'))
            op=self.operation();command=op.lf_command if self.band=='lf' else op.hf_command;name=action_title(command)
            values=self.parameter_form.values() if not self.manual.isChecked() else None
            if values is None:review=op.render()
            else:
                labels={p.name:p.label for p in self.parameter_form.descriptors}
                review='\n'.join(labels.get(k,k)+': '+(tr('Да') if v is True else ', '.join(v) if isinstance(v,list) else str(v))
                                 for k,v in values.items() if v is not None and v is not False and v!='' and v!=[])
                if self.parameter_form.target_type is not None:review+=tr('\nЗаготовка: ')+self.parameter_form.target_type.currentText()
            if self.manual.isChecked() or command not in READ_ACTIONS:
                answer=QMessageBox.question(self,name,name+' · '+family_title(command)+'\n\n'+review+
                    tr('\n\nВыполнить действие с этими параметрами?'),QMessageBox.StandardButton.Ok|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)
                if answer!=QMessageBox.StandardButton.Ok:return
            self.owner.current_session=self.owner._new_session();self.owner._start_step(op,self.timeout.value()*1000)
            self.owner.journal_toggle.setChecked(True);self.set_busy(True)
            self.status.setText(tr('Выполняется: ')+name+tr('. Результат появится в журнале.'))
        except (ValueError,OSError) as exc:self.status.setText(str(exc))


class HFToolsControllerMixin:
    def open_hf_tools(self):
        if getattr(self,'hf_tools_dialog',None) is None:self.hf_tools_dialog=HFToolsDialog(self)
        self.hf_tools_dialog.set_busy(self.runner.busy or self.read_job is not None or self.antenna_job is not None or self.firmware_busy)
        self.hf_tools_dialog.show();self.hf_tools_dialog.raise_()

    def open_lf_tools(self,write=False):
        if getattr(self,'lf_tools_dialog',None) is None:self.lf_tools_dialog=HFToolsDialog(self,'lf')
        dialog=self.lf_tools_dialog
        dialog.set_busy(self.runner.busy or self.read_job is not None or self.antenna_job is not None or self.firmware_busy)
        if write:
            dialog.writes_only.setChecked(True)
            if self.loaded_dump and self.loaded_dump.metadata.get('protocol')=='EM410x':
                dialog.group.setCurrentIndex(0)
                dialog.search.setText('lf em 410x clone')
                if dialog.parameter_form and 'id' in dialog.parameter_form.fields:dialog.parameter_form.fields['id'].setText(self.loaded_dump.data.hex().upper())
        dialog.show();dialog.raise_()
