"""Native workbench for card data, captures, board files and expert tools."""
from __future__ import annotations
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
import csv
import json
import math
import re
from PySide6.QtCore import Qt, QUrl, QRectF, QRect, QSize
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPen, QPainterPath
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QTabWidget, QPushButton,
    QLabel, QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox, QCheckBox, QTableWidget,
    QTableWidgetItem, QFileDialog, QMessageBox, QSplitter, QScrollArea, QListWidget, QStackedWidget, QLayout, QFrame, QHeaderView)
from .core import DumpData, parse_dump, parse_hex, export_dump
from .operations import CLASSIC_GEOMETRIES, sector_first_block, sector_trailer, _path_token
from .writing import access_conditions
from .source_picker import SourcePicker
from .interface_mode import Guidance
from .hf_tools_ui import ParameterForm
from .hf_forms import form_arguments
from .client_catalog import COMMANDS, split_command, uses_rf, changes_data
from .studio_data import (say, block_selection, encode_access, access_report, key_entries, merge_keys,
    parse_binary_keys, binary_keys, decode_value_block, compare_dumps, encode_ndef, decode_ndef,
    parse_trace, load_samples, fft_spectrum, structured_dump, TraceFrame)
from .studio_flows import (Request, one_command, classic_selected, magic_uid, emulator_read, emulator_write,
    traces, LF_READERS, HF_DUMP_PROFILES, read_lf_profile, read_hf_profile, ndef_read, ndef_write,
    capture_signal, download_spiffs, spiffs_file)
from .theme import mono_font
from .ui import Disclosure, SmoothStack, line_icon
from .motion_tabs import SmoothTabs
from .classic_ui import ClassicSplit, ClassicHexDelegate, BYTE_COLORS, key_slots
from .i18n import display_key_source



from .ui import SoftButton

def label(ru, en):
    w = QLabel(say(ru, en));w.setWordWrap(True);w.setTextFormat(Qt.TextFormat.PlainText)
    return w


def panel():
    w = QWidget();layout = QVBoxLayout(w);layout.setContentsMargins(12, 12, 12, 12);layout.setSpacing(10)
    return w, layout


def row(layout):
    r = FlowLayout();layout.addLayout(r);return r


class FlowLayout(QLayout):
    """Let native action rows wrap on narrow windows and high DPI."""
    def __init__(self):
        super().__init__();self.items=[];self.setContentsMargins(0,0,0,0);self.setSpacing(8)
    def addItem(self,item):self.items.append(item)
    def addWidget(self,widget,stretch=0):super().addWidget(widget)
    def count(self):return len(self.items)
    def itemAt(self,index):return self.items[index] if 0<=index<len(self.items) else None
    def takeAt(self,index):return self.items.pop(index) if 0<=index<len(self.items) else None
    def expandingDirections(self):return Qt.Orientation(0)
    def hasHeightForWidth(self):return True
    def heightForWidth(self,width):return self.arrange(QRect(0,0,width,0),True)
    def sizeHint(self):return self.minimumSize()
    def minimumSize(self):
        size=QSize(0,0)
        for item in self.items:size=size.expandedTo(item.minimumSize())
        return QSize(min(size.width(),320),size.height())
    def setGeometry(self,rect):super().setGeometry(rect);self.arrange(rect,False)
    def arrange(self,rect,test):
        x,y,height=rect.x(),rect.y(),0;space=self.spacing();width=max(1,rect.width())
        for item in self.items:
            if item.isEmpty():continue
            hint=item.sizeHint();w=min(hint.width(),width)
            h=item.heightForWidth(w) if item.hasHeightForWidth() else hint.height();h=max(h,hint.height())
            if x>rect.x() and x+w>rect.right()+1:x=rect.x();y+=height+space;height=0
            if not test:item.setGeometry(QRect(x,y,w,h))
            x+=w+space;height=max(height,h)
        return y+height-rect.y()


def table(headers, widths=None):
    from .presentation import configure_table, install_table_copy
    w = QTableWidget(0, len(headers));w.setHorizontalHeaderLabels(headers)
    configure_table(w, widths or [130] * len(headers));install_table_copy(w)
    return w


def fill_table(widget, rows):
    widget.setSortingEnabled(False);widget.setRowCount(len(rows))
    for r, values in enumerate(rows):
        for c, value in enumerate(values):
            widget.setItem(r, c, QTableWidgetItem(str(value)))


class SignalPlot(QWidget):
    """Pan/zoom a bounded view of samples; draw at most two points per pixel."""
    def __init__(self):
        super().__init__();self.points=[];self.first=0;self.length=0;self.drag=None
        self.setMinimumHeight(180);self.setMouseTracking(True)

    def set_points(self, points):
        self.points=list(points);self.first=0;self.length=len(self.points);self.update()

    def wheelEvent(self, event):
        if not self.points:return
        fraction=max(0, min(1, event.position().x()/max(1,self.width())))
        length=max(16, min(len(self.points), int(self.length*(.75 if event.angleDelta().y()>0 else 1.33))))
        anchor=self.first+int(self.length*fraction)
        self.first=max(0,min(len(self.points)-length,anchor-int(length*fraction)));self.length=length;self.update()

    def mousePressEvent(self,event):self.drag=(event.position().x(),self.first)
    def mouseReleaseEvent(self,event):self.drag=None
    def mouseMoveEvent(self,event):
        if self.drag:
            self.first=max(0,min(len(self.points)-self.length,self.drag[1]+int((self.drag[0]-event.position().x())*self.length/max(1,self.width()))));self.update()

    def paintEvent(self,event):
        painter=QPainter(self);painter.fillRect(self.rect(),QColor('#131b25'))
        points=self.points[self.first:self.first+self.length]
        if not points:return
        low,high=min(points),max(points);span=max(high-low,1e-9);rect=self.rect().adjusted(12,18,-12,-22)
        painter.setPen(QColor('#a7b4c6'));painter.drawText(12,self.height()-5,f'{self.first} — {self.first+len(points)-1} · {low:.3g} / {high:.3g}')
        painter.setPen(QPen(QColor('#6fb5ff'),1));path=QPainterPath();started=False
        stride=max(1,len(points)//max(1,rect.width()))
        for i in range(0,len(points),stride):
            batch=points[i:i+stride]
            for value in (min(batch),max(batch)):
                x=rect.left()+i/max(1,len(points)-1)*rect.width();y=rect.bottom()-(value-low)/span*rect.height()
                if started:path.lineTo(x,y)
                else:path.moveTo(x,y);started=True
        painter.drawPath(path)


class StudioPage(QWidget):
    def __init__(self,owner):
        super().__init__();self.owner=owner;self.buttons=[];self.last_output='';self.last_session=None
        if not hasattr(owner,'client_catalog_snapshot'):
            from .catalog_snapshot import load_catalog,CatalogError
            try:owner.client_catalog_snapshot=load_catalog(owner.data_dir/'client-catalog.json')
            except CatalogError:owner.client_catalog_snapshot=None
        self.dump=None;self.record={};self.trace_frames=[];self.samples=[];self.sample_rate=None;self.ndef=b''
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0)
        toolbar=row(layout)
        self.status=label('Выберите инструмент. Просмотр работает без подключения.','Choose a tool. Browsing works offline.');toolbar.addWidget(self.status,1)
        self.reset_button=SoftButton(say('После сброса питания','After power reset'));self.reset_button.clicked.connect(owner.acknowledge_studio_reset);self.reset_button.hide();toolbar.addWidget(self.reset_button)
        self.button(toolbar,'Файлы сеанса…','Session files…',self.open_session,hardware=False)
        split=QSplitter(Qt.Orientation.Horizontal);layout.addWidget(split,1)
        self.sections=QListWidget();self.sections.setMinimumWidth(125);self.sections.setMaximumWidth(170)
        self.sections.setWordWrap(True);self.sections.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff);split.addWidget(self.sections)
        self.pages=SmoothStack();split.addWidget(self.pages);split.setSizes([160,920])
        for ru,en,builder in [('Classic','Classic',self.classic_page),('NDEF','NDEF',self.ndef_page),
                ('Семейства карт','Card families',self.profiles_page),('Трассы','Traces',self.traces_page),
                ('Сигнал LF','LF signal',self.signal_page),('Память платы','Board memory',self.files_page),
                ('Диагностика','Diagnostics',self.diagnostics_page),('Консоль','Console',self.console_page)]:
            self.sections.addItem(say(ru,en));scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setWidget(builder());self.pages.addWidget(scroll)
        self.sections.currentRowChanged.connect(self.pages.setCurrentIndex);self.sections.setCurrentRow(0)
        for i in range(self.sections.count()):self.sections.item(i).setSizeHint(QSize(0,44))
        self.output=QPlainTextEdit();self.output.setReadOnly(True);self.output.setFont(mono_font());self.output.setMaximumHeight(130)
        self.output.document().setMaximumBlockCount(2500);self.output.setPlaceholderText(say('Результаты лаборатории','Laboratory results'))
        self.output_disclosure=Disclosure(say('Технический вывод','Technical output'));self.output_disclosure.body.addWidget(self.output);layout.addWidget(self.output_disclosure)

    def button(self,layout,ru,en,callback,hardware=True):
        button=SoftButton(say(ru,en));button.clicked.connect(lambda checked=False:self.guard(callback));layout.addWidget(button)
        if hardware:self.buttons.append(button)
        return button

    def guard(self,callback):
        try:return callback()
        except (ValueError,OSError,KeyError,TypeError,UnicodeError,json.JSONDecodeError) as exc:self.status.setText(str(exc))

    def set_busy(self,active):
        for button in self.buttons:button.setEnabled(not active)

    def review(self,title,details):
        return QMessageBox.question(self,title,details,QMessageBox.StandardButton.Ok|QMessageBox.StandardButton.Cancel,
                                    QMessageBox.StandardButton.Cancel)==QMessageBox.StandardButton.Ok

    def run(self,flow,title,rf=False,on_done=None,hold=False,offline=False):
        self.owner.start_studio(flow,title,rf=rf,on_done=on_done,hold=hold,offline=offline)

    def open_session(self):
        path=self.last_session or self.owner.current_session or self.owner.data_dir/'sessions'
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def classic_page(self):
        w,layout=panel();toolbar=row(layout)
        badge=label('MIFARE CLASSIC','MIFARE CLASSIC');badge.setObjectName('classicBadge');toolbar.addWidget(badge)
        self.button(toolbar,'Из редактора','From editor',self.from_editor,False)
        self.button(toolbar,'Выбрать снимок…','Choose snapshot…',self.choose_snapshot,False)
        self.button(toolbar,'Определить карту','Identify card',lambda:self.run(one_command('hf 14a info'),say('Информация Classic','Classic information'),True))
        self.classic_info=label('Выберите снимок Classic.','Choose a Classic snapshot.');layout.addWidget(self.classic_info)
        self.classic_tabs=tabs=SmoothTabs();tabs.setObjectName('classicTabs');layout.addWidget(tabs,1)
        for ru,en,builder in [('Блоки','Blocks',self.blocks_page),('Права доступа','Access rights',self.access_page),
                ('Ключи','Keys',self.key_page),('Magic','Magic',self.magic_page),('RAM эмулятора','Emulator RAM',self.emulator_page),('Сравнение','Compare',self.diff_page)]:
            tabs.addTab(builder(),say(ru,en))
        return w

    def choose_snapshot(self):
        from PySide6.QtWidgets import QDialog,QDialogButtonBox
        dialog=QDialog(self);dialog.setWindowTitle(say('Снимок карты','Card snapshot'));body=QVBoxLayout(dialog)
        picker=SourcePicker(self.owner,'hf');body.addWidget(picker)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel);buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject);body.addWidget(buttons)
        if dialog.exec()==QDialog.DialogCode.Accepted and picker.dump is not None:self.use_dump(picker.dump,picker.record)

    def from_editor(self):
        self.use_dump(self.owner._working_dump(),{'uid':self.owner.uid.text(),'name':self.owner.card_name.text(),'protocol':self.owner.protocol.text(),'id':self.owner.card_id})

    def use_dump(self,dump,record):
        from .writing import source_family
        if source_family(dump) != 'mfc':raise ValueError('Choose a Classic snapshot for this editor.')
        size=next((s for s,(_,n) in CLASSIC_GEOMETRIES.items() if len(dump.data)==n),None)
        if size is None or dump.block_size!=16 or dump.metadata.get('structured_document'):
            raise ValueError(say('Нужен снимок Classic Mini/1K/2K/4K.','Select a Classic Mini/1K/2K/4K snapshot.'))
        self.dump,self.record,self.classic_size=deepcopy(dump),dict(record),size
        self.classic_info.setText(f"{record.get('name','')} · {size.upper()} · UID {record.get('uid','')} · {len(dump.data)//16} "+say('блоков','blocks'))
        self.acl_sector.setRange(0,CLASSIC_GEOMETRIES[size][0]-1)
        self.ram_size.setCurrentIndex(self.ram_size.findData(size))
        self.block_table.setRowCount(len(dump.data)//16)
        known=set(dump.metadata.get('partial_dump',{}).get('known_bytes',range(len(dump.data))))
        self.block_table.blockSignals(True)
        for block in range(len(dump.data)//16):
            choose=QTableWidgetItem(str(block));choose.setFlags(choose.flags()|Qt.ItemFlag.ItemIsUserCheckable);choose.setCheckState(Qt.CheckState.Unchecked)
            self.block_table.setItem(block,0,choose)
            sector=block//4 if block<128 else 32+(block-128)//16
            self.block_table.setItem(block,1,QTableWidgetItem(str(sector)))
            item=QTableWidgetItem(' '.join(f'{b:02X}' if block*16+i in known else '??' for i,b in enumerate(dump.data[block*16:(block+1)*16])))
            role='trailer' if block==sector_trailer(sector) else 'uid' if block==0 else 'data'
            item.setData(Qt.ItemDataRole.UserRole,role);item.setData(Qt.ItemDataRole.UserRole+1,len(record.get('uid','').replace(' ',''))//2 or 4)
            item.setFont(mono_font());item.setToolTip(item.text());self.block_table.setItem(block,2,item)
            kind=QTableWidgetItem(say('Трейлер','Trailer') if role=='trailer' else 'UID' if block==0 else say('Данные','Data'))
            kind.setForeground(QColor(BYTE_COLORS['acl' if role=='trailer' else role]));self.block_table.setItem(block,3,kind)
        self.block_table.blockSignals(False);self.update_block_count()
        self.load_acl();self.show_keys();self.show_key_overview()

    def require_dump(self):
        if self.dump is None:raise ValueError(say('Сначала выберите снимок.','Choose a snapshot first.'))
        return self.dump

    def blocks_page(self):
        w,layout=panel();toolbar=row(layout)
        for ru,en,mode in [('Все','All','all'),('Только данные','Data only','data'),('Трейлеры','Trailers','trailers'),('Снять','Clear','none')]:
            self.button(toolbar,ru,en,lambda mode=mode:self.select_blocks(mode),False)
        self.block_count=label('Выбрано: 0','Selected: 0');self.block_count.setObjectName('muted');toolbar.addWidget(self.block_count)
        self.classic_split=ClassicSplit(Qt.Orientation.Horizontal);layout.addWidget(self.classic_split,1)
        data_frame,data_layout=self.classic_pane('Память карты','Card memory')
        self.block_table=table([say('Блок','Block'),say('Сектор','Sector'),'HEX',say('Тип','Type')],[80,60,410,80])
        self.block_table.setObjectName('classicMemory');self.block_table.verticalHeader().setDefaultSectionSize(27)
        self.block_table.setItemDelegateForColumn(2,ClassicHexDelegate(self.block_table));self.block_table.setMinimumHeight(245)
        self.block_table.itemChanged.connect(self.update_block_count);data_layout.addWidget(self.block_table,1)
        legend=row(data_layout)
        for title,kind in [('UID','uid'),(say('Данные','Data'),'data'),('Key A','a'),('ACL','acl'),('Key B','b'),('??','unknown')]:
            tag=QLabel('● '+title);tag.setStyleSheet('color: '+BYTE_COLORS[kind]+'; background: transparent;');legend.addWidget(tag)
        toolbar=row(data_layout);self.button(toolbar,'Считать выбранные','Read selected',self.read_blocks)
        self.button(toolbar,'Записать выбранные…','Write selected…',self.write_blocks)
        self.classic_split.addWidget(data_frame)
        keys_frame,keys_layout=self.classic_pane('Ключи секторов','Sector keys')
        self.key_overview=table([say('Сектор','Sector'),'Key A','Key B'],[60,140,140]);self.key_overview.setObjectName('classicKeys')
        self.key_overview.verticalHeader().setDefaultSectionSize(27);self.key_overview.setMinimumHeight(210)
        self.key_overview.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.ResizeToContents)
        keys_layout.addWidget(self.key_overview,1)
        self.key_overview_note=label('Зелёный — подтверждён; цвет A/B — кандидат.','Green: verified; A/B color: candidate.');self.key_overview_note.setObjectName('muted');keys_layout.addWidget(self.key_overview_note)
        toolbar=row(keys_layout);self.attack_buttons={}
        for method in ('Nested','Hardnested','Darkside'):
            button=self.button(toolbar,method,method,lambda method=method:self.owner.open_classic_attack(method,True),False)
            button.setProperty('classicAction','attack');self.attack_buttons[method]=button
        toolbar=row(keys_layout)
        self.button(toolbar,'Автопоиск ключей','Automatic recovery',self.owner.check_card_keys)
        self.button(toolbar,'Библиотека ключей','Key library',lambda:self.owner.navigate(2),False)
        self.classic_split.addWidget(keys_frame);self.classic_split.setSizes([610,360])
        return w

    @staticmethod
    def classic_pane(ru,en):
        frame=QFrame();frame.setObjectName('classicPane');layout=QVBoxLayout(frame);layout.setContentsMargins(12,10,12,10);layout.setSpacing(8)
        heading=label(ru,en);heading.setObjectName('section');layout.addWidget(heading)
        return frame,layout

    def update_block_count(self,*_):
        count=sum(self.block_table.item(r,0) is not None and self.block_table.item(r,0).checkState()==Qt.CheckState.Checked for r in range(self.block_table.rowCount()))
        self.block_count.setText(say('Выбрано: ','Selected: ')+str(count))

    def show_key_overview(self):
        from .keys import format_key_timestamp
        uid=self.record.get('uid','').replace(' ','').upper()
        try:entries=self.owner.key_library.list_card_keys(uid) if uid else []
        except ValueError:entries=[]
        slots=key_slots(self.require_dump(),entries);self.key_overview.setRowCount(CLASSIC_GEOMETRIES[self.classic_size][0])
        for sector in range(self.key_overview.rowCount()):
            self.key_overview.setItem(sector,0,QTableWidgetItem(str(sector)))
            for col,kind in enumerate(('A','B'),1):
                values=slots.get((sector,kind),{});known=list(values.values())
                value=known[0]['key_hex'] if len(known)==1 else say('Нет ключа','Unknown') if not known else say('Варианты: ','Candidates: ')+str(len(known))
                item=QTableWidgetItem(value);item.setFont(mono_font())
                color='#8ad4a0' if len(known)==1 and known[0].get('verified') else BYTE_COLORS[kind.lower()] if len(known)==1 else '#e6b877' if known else BYTE_COLORS['unknown']
                item.setForeground(QColor(color))
                if len(known)==1 and known[0].get('verified'):item.setIcon(line_icon('check',color))
                item.setToolTip('\n'.join(e['key_hex']+' · '+(say('Подтверждён','Verified') if e.get('verified') else say('Кандидат','Candidate'))+' · '+display_key_source(e.get('source',''))+' · '+format_key_timestamp(e.get('verified_at') or e.get('created_at')) for e in known) or say('Не прочитан и не известен.','Not read and not known.'))
                self.key_overview.setItem(sector,col,item)

    def select_blocks(self,mode):
        self.block_table.blockSignals(True)
        for b in range(self.block_table.rowCount()):
            trailer=self.block_table.item(b,3).text()==say('Трейлер','Trailer')
            checked=mode=='all' or mode=='data' and b!=0 and not trailer or mode=='trailers' and trailer
            self.block_table.item(b,0).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.block_table.blockSignals(False);self.update_block_count()

    def selected_blocks(self):
        self.require_dump()
        blocks=tuple(b for b in range(self.block_table.rowCount()) if self.block_table.item(b,0).checkState()==Qt.CheckState.Checked)
        if not blocks:raise ValueError(say('Отметьте блоки.','Check some blocks.'))
        return blocks

    def read_blocks(self):
        blocks=self.selected_blocks();uid=self.record.get('uid','').replace(' ','').upper()
        if not re.fullmatch(r'(?:[0-9A-F]{8}|[0-9A-F]{14}|[0-9A-F]{20})',uid):raise ValueError('The snapshot needs a confirmed UID.')
        keys=self.owner.key_library.list_card_keys(uid)
        self.run(classic_selected(self.classic_size,uid,blocks,keys),say('Чтение выбранных блоков','Reading selected blocks'),True,self.save_result)

    def write_blocks(self):
        blocks=self.selected_blocks();self.owner.open_write_dialog();dialog=self.owner.write_dialog
        dialog._set_source(self.require_dump(),self.record.get('name',say('Выбранный снимок','Selected snapshot')))
        dialog.selected_blocks.setText(', '.join(map(str,blocks)))
        dialog.parameters.setExpanded(True)

    def access_page(self):
        w,layout=panel();form=QFormLayout();layout.addLayout(form)
        self.acl_sector=QSpinBox();self.acl_sector.setRange(0,39);form.addRow(say('Сектор','Sector'),self.acl_sector)
        self.conditions=[]
        for i in range(4):
            box=QSpinBox();box.setRange(0,7);box.valueChanged.connect(lambda *_:self.update_acl());self.conditions.append(box)
            form.addRow((say('Трейлер','Trailer') if i==3 else say('Группа','Group')+f' {i}')+' · C1 C2 C3',box)
        self.gpb=QLineEdit('69');self.gpb.setMaxLength(2);form.addRow('GPB HEX',self.gpb)
        self.acl_hex=QLineEdit();self.acl_hex.setReadOnly(True);form.addRow('HEX',self.acl_hex)
        self.acl_rights=table([say('Группа','Group'),say('Чтение','Read'),say('Запись','Write'),say('Увеличение','Increment'),say('Уменьшение / перенос','Decrement / transfer')]);layout.addWidget(self.acl_rights)
        self.acl_detail=label('','');layout.addWidget(self.acl_detail)
        toolbar=row(layout);self.button(toolbar,'Из сектора','From sector',self.load_acl,False)
        self.button(toolbar,'Применить к снимку…','Apply to snapshot…',self.apply_acl,False)
        self.value_block=QSpinBox();self.value_block.setRange(0,255);toolbar.addWidget(self.value_block)
        self.button(toolbar,'Проверить value-блок','Decode value block',self.value_info,False)
        self.gpb.textChanged.connect(lambda *_:self.guard(self.update_acl));self.update_acl()
        return w

    def update_acl(self):
        values=[b.value() for b in self.conditions];raw=encode_access(values,int(self.gpb.text() or '69',16));report=access_report(values)
        self.acl_hex.setText(raw.hex(' ').upper());fill_table(self.acl_rights,[(i,*rights) for i,rights in enumerate(report['data'])])
        rights=report['trailer'];self.acl_detail.setText(say('Трейлер: запись A / чтение ACL / запись ACL / чтение B / запись B: ','Trailer: write A / read ACL / write ACL / read B / write B: ')+ ' / '.join(rights)+'\n'+
                (say('Key B используется для аутентификации.','Key B can authenticate.') if report['key_b_authenticates'] else say('Key B читается как данные и не используется для аутентификации.','Key B is readable data and cannot authenticate.')))

    def load_acl(self):
        raw=self.require_dump().data[sector_trailer(self.acl_sector.value())*16:][:16]
        known=set(self.dump.metadata.get('partial_dump',{}).get('known_bytes',range(len(self.dump.data))))
        start=sector_trailer(self.acl_sector.value())*16
        if not all(i in known for i in range(start+6,start+10)):raise ValueError('The access bytes were not read.')
        values=access_conditions(raw)
        for box,value in zip(self.conditions,values):box.blockSignals(True);box.setValue(value);box.blockSignals(False)
        self.gpb.setText(f'{raw[9]:02X}');self.update_acl()

    def save_snapshot(self,dump,note):
        # A new immutable revision/card: no silent modification of the source/editor.
        uid=self.record.get('uid','');cid=self.record.get('id')
        if cid and self.owner.library.get_card(cid):self.owner.library.save_revision(cid,dump,note=note)
        else:cid=self.owner.library.create_card(self.record.get('name',say('Снимок Classic','Classic snapshot'))+' · '+note,dump,uid=uid,protocol='MIFARE Classic')
        self.owner.refresh_library();self.use_dump(dump,{**self.record,'id':cid})
        self.status.setText(say('Сохранена новая версия. Исходный снимок сохранён.','Saved a new revision. The source snapshot is preserved.'))

    def apply_acl(self):
        dump=deepcopy(self.require_dump());raw=bytearray(dump.data);start=sector_trailer(self.acl_sector.value())*16+6
        value=encode_access([b.value() for b in self.conditions],int(self.gpb.text(),16))
        if self.review(say('Новая версия прав доступа','New access-rights revision'),f'{self.acl_sector.value()}: {raw[start:start+4].hex().upper()} → {value.hex().upper()}'):
            raw[start:start+4]=value;dump.data=bytes(raw);self.save_snapshot(dump,say('Права доступа изменены вручную','Access rights edited manually'))

    def value_info(self):
        b=self.value_block.value();raw=self.require_dump().data[b*16:(b+1)*16]
        value,address=decode_value_block(raw);self.status.setText(f'Value: {value} · Address: {address}')

    def key_page(self):
        w,layout=panel();self.key_table=table([say('Сектор','Sector'),'A/B','HEX',say('Происхождение','Source')]);layout.addWidget(self.key_table)
        self.zeros=QCheckBox(say('Включать нулевые значения как кандидаты','Include zero values as candidates'));layout.addWidget(self.zeros)
        toolbar=row(layout);self.button(toolbar,'Ключи из дампа → библиотека','Dump keys → library',self.dump_keys_to_library,False)
        self.button(toolbar,'Ключи библиотеки → новая версия','Library keys → new revision',self.library_keys_to_dump,False)
        toolbar=row(layout);self.button(toolbar,'Импорт key.bin…','Import key.bin…',self.import_keybin,False)
        self.button(toolbar,'Экспорт key.bin…','Export key.bin…',self.export_keybin,False)
        layout.addWidget(Guidance(self.owner,label('Импорт и заполнение не подтверждают ключи на физической карте. Неизвестные позиции экспорта отмечаются в сопутствующем JSON.','Importing or filling does not verify a key on a physical card. Unknown exported slots are recorded in a sidecar JSON.')))
        return w

    def show_keys(self):
        fill_table(self.key_table,[(e['sector'],e['type'],e['key_hex'],say('Поле дампа','Dump field')) for e in key_entries(self.require_dump(),self.zeros.isChecked())])

    def dump_keys_to_library(self):
        entries=key_entries(self.require_dump(),self.zeros.isChecked())
        self.owner.key_library.record_candidate_keys(self.record.get('uid',''),entries,source='Studio: dump fields')
        self.owner.refresh_keys();self.status.setText(say('Кандидаты добавлены.','Candidates added.'))

    def library_keys_to_dump(self):
        rows=self.owner.key_library.list_card_keys(self.record.get('uid',''))
        geometry=CLASSIC_GEOMETRIES[self.classic_size][0]
        binary_keys(rows,geometry)  # Reject conflicting candidates instead of guessing.
        if self.review(say('Перенос ключей','Copy keys'),say('Создать новую версию с ключами библиотеки?','Create a new revision using the library keys?')):
            self.save_snapshot(merge_keys(self.require_dump(),rows),say('Ключи перенесены вручную','Keys copied manually'))

    def import_keybin(self):
        self.require_dump();name,_=QFileDialog.getOpenFileName(self,'key.bin','','BIN (*.bin *.dump)')
        if not name:return
        entries=parse_binary_keys(Path(name).read_bytes(),CLASSIC_GEOMETRIES[self.classic_size][0],self.zeros.isChecked())
        self.owner.key_library.record_candidate_keys(self.record.get('uid',''),entries,source='Studio: '+Path(name).name)
        self.owner.refresh_keys();fill_table(self.key_table,[(e['sector'],e['type'],e['key_hex'],'key.bin') for e in entries])

    def export_keybin(self):
        raw,slots=binary_keys(key_entries(self.require_dump(),self.zeros.isChecked()),CLASSIC_GEOMETRIES[self.classic_size][0])
        name,_=QFileDialog.getSaveFileName(self,'key.bin','key.bin','BIN (*.bin)')
        if name:
            path=Path(name);path.write_bytes(raw);path.with_name(path.name+'.slots.json').write_text(json.dumps({'format':'ProxmarkStudio.KeySlots','known_slots':slots,'unknown_fill':'000000000000','verified_on_card':False},indent=2),encoding='utf-8')

    def magic_page(self):
        w,layout=panel();form=QFormLayout();layout.addLayout(form)
        self.magic_size=QComboBox()
        for s in CLASSIC_GEOMETRIES:self.magic_size.addItem(s.upper(),s)
        self.magic_size.setCurrentIndex(1);form.addRow(say('Геометрия','Geometry'),self.magic_size)
        self.magic_uid=QLineEdit();self.magic_atqa=QLineEdit('0004');self.magic_sak=QLineEdit('08')
        form.addRow('UID HEX',self.magic_uid);form.addRow('ATQA HEX',self.magic_atqa);form.addRow('SAK HEX',self.magic_sak)
        toolbar=row(layout)
        self.button(toolbar,'Определить поколение','Identify generation',lambda:self.run(one_command('hf 14a info'),say('Диагностика Magic','Magic diagnostics'),True))
        self.button(toolbar,'Gen1a: копия → UID → проверка','Gen1a: backup → UID → verify',self.write_magic_uid)
        layout.addWidget(Guidance(self.owner,label('Мастер Gen1a требует положительного определения поколения и полной резервной копии. Другие поколения используют свои команды ниже.','The Gen1a wizard requires positive generation detection and a complete backup. Other generations use their own actions below.')))
        self.magic_action=QComboBox()
        actions=[c for c in COMMANDS if c.startswith('hf mf ') and (re.search(r'\b(?:gen3|gdm|g4|gen4)',c) or c.split()[-1] in {'cgetblk','csetblk','csave','cload','csetuid'}) and not c.endswith(' help')]
        for c in actions:self.magic_action.addItem(c,c)
        layout.addWidget(self.magic_action);self.magic_form_host=QVBoxLayout();layout.addLayout(self.magic_form_host)
        self.magic_form=None;self.magic_action.currentIndexChanged.connect(self.magic_action_changed);self.magic_action_changed()
        self.button(layout,'Выполнить профильное действие…','Run generation-specific action…',lambda:self.run_form(self.magic_action.currentData(),self.magic_form,True))
        return w

    def magic_action_changed(self):
        if self.magic_form:self.magic_form_host.removeWidget(self.magic_form);self.magic_form.hide();self.magic_form.deleteLater()
        self.magic_form=ParameterForm(self.magic_action.currentData(),lambda:None);self.magic_form_host.addWidget(self.magic_form)

    def write_magic_uid(self):
        dialog=self.owner.open_family_wizard(operation='uid')
        dialog.new_uid.setText(self.magic_uid.text());dialog.atqa.setText(self.magic_atqa.text());dialog.sak.setText(self.magic_sak.text());dialog.geometry.setCurrentText(self.magic_size.currentData())

    def emulator_page(self):
        w,layout=panel();form=QFormLayout();layout.addLayout(form)
        self.ram_size=QComboBox()
        for s in CLASSIC_GEOMETRIES:self.ram_size.addItem(s.upper(),s)
        self.ram_size.setCurrentIndex(1);form.addRow(say('Размер RAM-образа','RAM image size'),self.ram_size)
        self.ram_blocks=QLineEdit('0-3');form.addRow(say('Блоки','Blocks'),self.ram_blocks)
        self.ram_table=table([say('Блок','Block'),'HEX']);layout.addWidget(self.ram_table)
        toolbar=row(layout);self.button(toolbar,'Прочитать RAM','Read RAM',self.read_ram)
        self.ram_block=QSpinBox();self.ram_block.setRange(0,255);toolbar.addWidget(self.ram_block)
        self.ram_data=QLineEdit();self.ram_data.setPlaceholderText('16 bytes HEX');layout.addWidget(self.ram_data)
        toolbar=row(layout);self.button(toolbar,'Записать блок и проверить…','Write and verify block…',self.write_ram)
        self.button(toolbar,'Очистить RAM…','Clear RAM…',lambda:self.run_reviewed('hf mf eclr'))
        self.button(toolbar,'Сохранить RAM в библиотеку','Save RAM to library',self.save_ram)
        return w

    def read_ram(self):
        size=self.ram_size.currentData();blocks=block_selection(self.ram_blocks.text(),CLASSIC_GEOMETRIES[size][1]//16)
        self.run(emulator_read(size,blocks),say('Чтение RAM эмулятора','Reading emulator RAM'),on_done=lambda result:fill_table(self.ram_table,sorted(result['blocks'].items())))

    def write_ram(self):
        plan=emulator_write(self.ram_block.value(),parse_hex(self.ram_data.text()).hex())
        if self.review(say('Память эмулятора','Emulator memory'),f'{self.ram_block.value()}: {self.ram_data.text()}'):
            self.run(plan,say('Запись RAM эмулятора','Writing emulator RAM'),on_done=lambda r:fill_table(self.ram_table,sorted(r['blocks'].items())))

    def save_ram(self):
        size=self.ram_size.currentData()
        def flow(directory):
            prefix=directory/'emulator'
            yield Request('hf mf esave',f'--{size} -f '+_path_token(prefix))
            dump=parse_dump(prefix.with_suffix('.json') if prefix.with_suffix('.json').is_file() else prefix.with_suffix('.bin'))
            if len(dump.data)!=CLASSIC_GEOMETRIES[size][1]:raise ValueError('Wrong emulator image size.')
            return {'dump':dump,'uid':dump.data[:4].hex().upper(),'protocol':'MIFARE Classic '+size.upper(),'verification':'emulator-snapshot-not-card-read'}
        self.run(flow,say('Сохранение RAM','Saving RAM'),on_done=self.save_result)

    def diff_page(self):
        w,layout=panel();toolbar=row(layout);self.button(toolbar,'Сравнить с файлом…','Compare with file…',self.compare_file,False)
        self.button(toolbar,'Сравнить с версией библиотеки…','Compare with library revision…',self.compare_library,False)
        self.diff_table=table([say('Адрес','Offset'),'A','B',say('Состояние A / B','State A / B')]);layout.addWidget(self.diff_table)
        layout.addWidget(Guidance(self.owner,label('Сравниваются байты и карта прочитанных данных. Заполнители не считаются прочитанными нулями.','Compares bytes and known-byte maps. Placeholders are not treated as confirmed zero bytes.')))
        self.value_decode=QLineEdit();self.value_decode.setPlaceholderText('Value block · 16 bytes HEX');layout.addWidget(self.value_decode)
        self.button(layout,'Декодировать value-блок','Decode value block',lambda:self.status.setText(str(decode_value_block(parse_hex(self.value_decode.text())))),False)
        return w

    def compare_file(self):
        self.require_dump();name,_=QFileDialog.getOpenFileName(self,say('Второй снимок','Second snapshot'),'','Dumps (*.bin *.mfd *.eml *.json)')
        if name:
            self.show_difference(parse_dump(Path(name)))

    def compare_library(self):
        from PySide6.QtWidgets import QDialog,QDialogButtonBox
        self.require_dump();dialog=QDialog(self);dialog.setWindowTitle(say('Сравнить снимки','Compare snapshots'));body=QVBoxLayout(dialog)
        picker=SourcePicker(self.owner);body.addWidget(picker)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel);buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject);body.addWidget(buttons)
        if dialog.exec()==QDialog.DialogCode.Accepted and picker.dump is not None:self.show_difference(picker.dump)

    def show_difference(self,other):
        differences=compare_dumps(self.require_dump(),other)
        fill_table(self.diff_table,[(f'{d["offset"]:04X}',f'{d["a"]:02X}' if d['a'] is not None else '—',f'{d["b"]:02X}' if d['b'] is not None else '—',f'{d["a_known"]} / {d["b_known"]}') for d in differences])
        for r,d in enumerate(differences):
            color=QColor('#f1c671') if not d['a_known'] or not d['b_known'] else QColor('#f1a2ae')
            for c in range(4):self.diff_table.item(r,c).setForeground(color)
        self.status.setText(say('Различий: ','Differences: ')+str(len(differences)))

    def ndef_page(self):
        w,layout=panel();toolbar=row(layout);self.ndef_profile=QComboBox();self.ndef_profile.addItems(['Ultralight / NTAG','Classic','Type 4A','Type 4B']);toolbar.addWidget(self.ndef_profile)
        self.ndef_key=QLineEdit();self.ndef_key.setPlaceholderText(say('Необязательный ключ HEX','Optional HEX key'));toolbar.addWidget(self.ndef_key)
        self.button(toolbar,'Считать NDEF','Read NDEF',lambda:self.run(ndef_read(self.ndef_profile.currentText(),self.ndef_key.text().strip()),'NDEF',True,self.ndef_result))
        self.ndef_records=QPlainTextEdit();self.ndef_records.setPlaceholderText(say('URL https://example.com\nTEXT ru Привет\nAAR com.example.app','URL https://example.com\nTEXT en Hello\nAAR com.example.app'));self.ndef_records.setMaximumHeight(150);layout.addWidget(self.ndef_records)
        toolbar=row(layout);self.button(toolbar,'Собрать записи','Build records',self.build_ndef,False)
        self.button(toolbar,'Открыть BIN…','Open BIN…',self.load_ndef,False);self.button(toolbar,'Сохранить BIN…','Save BIN…',self.save_ndef_file,False)
        self.ndef_hex=QPlainTextEdit();self.ndef_hex.setFont(mono_font());self.ndef_hex.setMaximumHeight(120);layout.addWidget(self.ndef_hex)
        self.ndef_tree=QPlainTextEdit();self.ndef_tree.setReadOnly(True);layout.addWidget(self.ndef_tree)
        toolbar=row(layout);self.button(toolbar,'Декодировать HEX','Decode HEX',self.decode_ndef_hex,False)
        self.button(toolbar,'В библиотеку','Add to library',self.save_ndef_library,False)
        self.button(toolbar,'NTAG: копия → запись → проверка…','NTAG: backup → write → verify…',self.write_ndef)
        layout.addWidget(Guidance(self.owner,label('Обычная запись — Ultralight/NTAG. Для других типов ниже доступны команды с собственными параметрами и просмотром плана.','The verified writer targets Ultralight/NTAG. Other tag types have their own parameterised actions in the expert tools.')))
        return w

    def build_ndef(self):
        records=[]
        for line in self.ndef_records.toPlainText().splitlines():
            if not line.strip():continue
            kind,_,value=line.strip().partition(' ')
            if kind.upper()=='URL':records.append({'kind':'uri','value':value})
            elif kind.upper()=='TEXT':
                lang,_,text=value.partition(' ');records.append({'kind':'text','language':lang,'value':text})
            elif kind.upper()=='AAR':records.append({'kind':'aar','value':value})
            else:raise ValueError('Use URL, TEXT <language>, or AAR records.')
        self.show_ndef(encode_ndef(records))

    def show_ndef(self,raw):
        decoded=decode_ndef(raw);self.ndef=bytes(raw);self.ndef_hex.setPlainText(raw.hex(' ').upper());self.ndef_tree.setPlainText(json.dumps(decoded,ensure_ascii=False,indent=2))

    def decode_ndef_hex(self):self.show_ndef(parse_hex(self.ndef_hex.toPlainText()))
    def ndef_result(self,result):self.show_ndef(result['ndef'])

    def load_ndef(self):
        name,_=QFileDialog.getOpenFileName(self,'NDEF','','BIN (*.bin)')
        if name:
            path=Path(name)
            if path.stat().st_size>1024*1024:raise ValueError('NDEF file is too large.')
            self.show_ndef(path.read_bytes())

    def save_ndef_file(self):
        self.decode_ndef_hex();name,_=QFileDialog.getSaveFileName(self,'NDEF','ndef.bin','BIN (*.bin)')
        if name:Path(name).write_bytes(self.ndef)

    def save_ndef_library(self):
        self.decode_ndef_hex();self.save_result({'dump':structured_dump({'records':decode_ndef(self.ndef),'ndef_hex':self.ndef.hex()},'NDEF',kind='ndef-message'),'protocol':'NDEF','uid':''})

    def write_ndef(self):
        self.decode_ndef_hex()
        dialog=self.owner.open_family_wizard(operation='ndef');dialog.payload.setPlainText(self.ndef.hex(' ').upper());dialog.ndef_key.setText(self.ndef_key.text())

    def profiles_page(self):
        w,layout=panel();toolbar=row(layout);self.profile=QComboBox()
        for name in HF_DUMP_PROFILES:self.profile.addItem('HF · '+name,('hf',name))
        for name in LF_READERS:self.profile.addItem('LF · '+name,('lf',name))
        toolbar.addWidget(self.profile,1);self.button(toolbar,'Считать и сохранить','Read and save',self.read_profile)
        self.button(toolbar,'Мастер записи…','Write wizard…',lambda:self.owner.open_family_wizard(self.profile.currentData()[1],self.family_dump),False)
        self.profile_form=None;self.profile_form_host=QVBoxLayout();layout.addLayout(self.profile_form_host)
        self.family_action=QComboBox();layout.addWidget(self.family_action)
        self.family_action_form=None;self.family_action_host=QVBoxLayout();layout.addLayout(self.family_action_host)
        self.family_action.currentIndexChanged.connect(self.family_action_changed)
        self.button(layout,'Проверить и выполнить действие…','Review and run family action…',self.run_family_action)
        self.profile_hint=label('','');layout.addWidget(Guidance(self.owner,self.profile_hint))
        self.profile_view=QPlainTextEdit();self.profile_view.setReadOnly(True);self.profile_view.setFont(mono_font());layout.addWidget(self.profile_view)
        toolbar=row(layout);self.button(toolbar,'Открыть снимок для просмотра…','Open snapshot for inspection…',self.inspect_profile_file,False)
        self.sim_profile=QComboBox();self.sim_profile.addItems(['Ultralight / NTAG','ISO15693 / NFC-V','FeliCa','LEGIC']);toolbar.addWidget(self.sim_profile)
        self.sim_type=QComboBox()
        for name,value in [('Ultralight',2),('EV1 / NTAG',7),('UL-C',13),('UL-AES',14)]:self.sim_type.addItem(name,value)
        self.sim_type.setCurrentIndex(1);toolbar.addWidget(self.sim_type)
        self.sim_duration=QSpinBox();self.sim_duration.setRange(1,20);self.sim_duration.setValue(5);self.sim_duration.setSuffix(say(' мин',' min'));toolbar.addWidget(self.sim_duration)
        self.button(toolbar,'Эмулировать снимок…','Simulate snapshot…',self.simulate_profile)
        toolbar=row(layout);self.button(toolbar,'LF: записать на T5577…','LF: write to T5577…',self.clone_lf)
        self.button(toolbar,'LF: эмулировать идентификатор…','LF: simulate identifier…',self.simulate_lf)
        self.button(toolbar,'Выбрать запись библиотеки…','Choose library record…',self.choose_family_record,False)
        self.button(toolbar,'Экспорт исходного JSON…','Export original JSON…',self.export_family_json,False)
        self.button(toolbar,'Сохранить снимок в библиотеку','Save snapshot to library',self.save_family_source,False)
        self.family_dump=None;self.profile.currentIndexChanged.connect(self.profile_changed);self.profile_changed()
        return w

    def profile_changed(self):
        if self.profile_form:self.profile_form_host.removeWidget(self.profile_form);self.profile_form.hide();self.profile_form.deleteLater();self.profile_form=None
        band,name=self.profile.currentData()
        command=HF_DUMP_PROFILES[name][1] if band=='hf' else LF_READERS[name]
        self.profile_form=ParameterForm(command,lambda:None);self.profile_form_host.addWidget(self.profile_form)
        for key in ('file','filename','f'):
            if key in self.profile_form.fields:
                self.profile_form.fields[key].setEnabled(False)
                self.profile_form.fields[key].setPlaceholderText(say('В файлах сеанса','In session files'))
                if key in self.profile_form.file_buttons:self.profile_form.file_buttons[key].setEnabled(False)
        prefix=' '.join(command.split()[:-1])+' '
        self.family_action.blockSignals(True);self.family_action.clear()
        for name in sorted(COMMANDS):
            if name.startswith(prefix) and not name.endswith(' help'):self.family_action.addItem(name,name)
        desired={'hf 15 dump':'hf 15 restore','hf iclass dump':'hf iclass restore','hf legic dump':'hf legic restore'}.get(command)
        if desired:self.family_action.setCurrentIndex(self.family_action.findData(desired))
        self.family_action.blockSignals(False)
        self.family_action_changed()
        self.profile_hint.setText(say('Файлы результата сохраняются в сеансе. Закрытые данные требуют подходящих ключей. LF-запись содержит идентификатор и его поля, а не полный образ чипа.',
                                     'Result files are kept in the session. Protected data needs appropriate keys. LF records contain identifiers and their fields, not a complete chip image.'))

    def read_profile(self):
        band,name=self.profile.currentData()
        if band=='lf':flow=read_lf_profile(name)
        else:
            values=self.profile_form.values()
            for k in ('file','filename','f'):values.pop(k,None)
            args=form_arguments(HF_DUMP_PROFILES[name][1],values)
            flow=read_hf_profile(name,args)
        self.run(flow,say('Чтение: ','Reading: ')+name,True,self.profile_result)

    def family_action_changed(self):
        from .studio_flows import HF_RESTORE_PROFILES
        if self.family_action_form:self.family_action_host.removeWidget(self.family_action_form);self.family_action_form.hide();self.family_action_form.deleteLater();self.family_action_form=None
        command=self.family_action.currentData()
        if not command:return
        self.family_action_form=ParameterForm(command,lambda:None);self.family_action_host.addWidget(self.family_action_form)
        if command in HF_RESTORE_PROFILES.values():
            for name in ('file','filename','f'):
                field=self.family_action_form.fields.get(name)
                if isinstance(field,QLineEdit):
                    field.setText(say('Из выбранного снимка','From the selected snapshot'));field.setReadOnly(True)
                    if name in self.family_action_form.file_buttons:self.family_action_form.file_buttons[name].setEnabled(False)
        for name,value in [('first','6'),('last','31')]:
            field=self.family_action_form.fields.get(name)
            if isinstance(field,QLineEdit):field.setText(value)

    def run_family_action(self):
        from .studio_flows import HF_RESTORE_PROFILES,restore_hf_profile
        command=self.family_action.currentData()
        if not command:raise ValueError('Choose a family action.')
        band,profile=self.profile.currentData()
        if command in HF_RESTORE_PROFILES.values():
            dialog=self.owner.open_family_wizard(profile,self.family_dump)
            for original,target in ((self.profile_form,dialog.read_form),(self.family_action_form,dialog.write_form)):
                for name,value in original.values().items():
                    field=target.fields.get(name)
                    if isinstance(field,QLineEdit) and not field.isReadOnly():field.setText(str(value))
                    elif isinstance(field,QCheckBox) and field.isEnabled():field.setChecked(bool(value))
                    elif isinstance(field,QComboBox):field.setCurrentText(str(value))
        elif command in {'hf mfp wrbl','hf mfdes write','hf felica wrbl','hf topaz wrbl'}:
            dialog=self.owner.open_family_wizard(profile,self.family_dump)
            values=self.family_action_form.values()
            for key,fieldname in [('blk','address'),('aid','aid'),('fid','fid'),('key','key'),('scl','service'),('data','payload')]:
                value=values.get(key);field=getattr(dialog,fieldname,None)
                if value in (None,'') or field is None:continue
                if isinstance(field,QLineEdit):field.setText(str(value))
                elif isinstance(field,QPlainTextEdit):field.setPlainText(str(value))
                elif isinstance(field,QSpinBox):field.setValue(int(value))
        else:self.run_reviewed(command,self.family_action_form.arguments())

    def export_family_json(self):
        if self.family_dump is None:raise ValueError('Choose a family snapshot first.')
        document=self.family_dump.metadata.get('structured_document') or self.family_dump.metadata.get('source_json')
        if not isinstance(document,dict):raise ValueError('This snapshot has no original JSON document.')
        name,_=QFileDialog.getSaveFileName(self,'PM3 JSON','card.json','JSON (*.json)')
        if name:Path(name).write_text(json.dumps(document,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')

    def save_family_source(self):
        if self.family_dump is None:raise ValueError('Choose a snapshot first.')
        meta=self.family_dump.metadata;doc=meta.get('structured_document') or meta.get('source_json') or {}
        uid=meta.get('uid') or doc.get('Card',{}).get('UID',doc.get('Card',{}).get('uid',''))
        self.save_result({'dump':deepcopy(self.family_dump),'uid':uid,'protocol':meta.get('protocol',''),'verification':'imported-snapshot-not-a-new-card-read'})

    def profile_result(self,result):
        self.family_dump=deepcopy(result['dump']);self.profile_view.setPlainText(json.dumps(result.get('fields') or result['dump'].metadata.get('structured_document') or result['dump'].metadata.get('source_json') or result['dump'].metadata,ensure_ascii=False,indent=2))
        self.save_result(result)

    def inspect_profile_file(self):
        from .studio_data import import_profile_dump
        name,_=QFileDialog.getOpenFileName(self,say('Снимок','Snapshot'),'','Dumps (*.json *.bin *.eml)')
        if name:
            self.family_dump=import_profile_dump(Path(name),self.profile.currentData()[1])
            self.profile_view.setPlainText(json.dumps(self.family_dump.metadata.get('structured_document') or self.family_dump.metadata.get('source_json') or self.family_dump.metadata,ensure_ascii=False,indent=2))

    def simulate_profile(self):
        from .studio_flows import simulate_family
        if self.family_dump is None:raise ValueError(say('Сначала прочитайте или откройте снимок.','Read or open a snapshot first.'))
        profile=self.sim_profile.currentText();flow=simulate_family(deepcopy(self.family_dump),profile,self.sim_type.currentData())
        if self.review(say('Эмуляция','Simulation'),profile+say('\nИспользовать этот снимок до нажатия «Остановить»?','\nUse this snapshot until Stop is pressed?')):
            self.owner.start_studio(flow,say('Эмуляция: ','Simulation: ')+profile,rf=True,hold=True,duration_ms=self.sim_duration.value()*60000)

    def choose_family_record(self):
        from PySide6.QtWidgets import QDialog,QDialogButtonBox
        dialog=QDialog(self);dialog.setWindowTitle(say('Запись библиотеки','Library record'));body=QVBoxLayout(dialog);picker=SourcePicker(self.owner);body.addWidget(picker)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel);buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject);body.addWidget(buttons)
        if dialog.exec()==QDialog.DialogCode.Accepted and picker.dump is not None:
            self.family_dump=deepcopy(picker.dump);self.profile_view.setPlainText(json.dumps(self.family_dump.metadata.get('structured_document') or self.family_dump.metadata,ensure_ascii=False,indent=2))
            fields=self.family_dump.metadata.get('structured_document',{})
            protocol=fields.get('protocol',self.family_dump.metadata.get('protocol',''))
            for i in range(self.profile.count()):
                band,name=self.profile.itemData(i)
                if name==protocol:self.profile.setCurrentIndex(i);break
            if protocol in LF_READERS:
                command=LF_READERS[protocol].rsplit(' ',1)[0]+' clone'
                index=self.family_action.findData(command)
                if index>=0:self.family_action.setCurrentIndex(index)
                values={'id':fields.get('identifier'),'raw':fields.get('identifier'),'fc':fields.get('facility_code'),'cn':fields.get('card_number'),
                        'fmt':fields.get('bit_length'),'vn':fields.get('version'),'country':fields.get('country'),'national':fields.get('national_code')}
                for name,value in values.items():
                    control=self.family_action_form.fields.get(name) if self.family_action_form else None
                    if isinstance(control,QLineEdit) and value is not None:control.setText(str(value))

    def lf_fields(self):
        if self.family_dump is None:raise ValueError('Choose an LF library record first.')
        meta=self.family_dump.metadata
        if meta.get('snapshot_kind')=='lf-identifier':return deepcopy(meta['structured_document'])
        if meta.get('protocol')=='EM410x' and len(self.family_dump.data)==5:return {'protocol':'EM410x','identifier':self.family_dump.data.hex().upper()}
        raise ValueError('Choose a supported LF identifier record.')

    def clone_lf(self):
        fields=self.lf_fields();self.owner.open_family_wizard(fields['protocol'],self.family_dump)

    def simulate_lf(self):
        from .studio_flows import lf_profile_arguments
        command,args=lf_profile_arguments(self.lf_fields(),'sim')
        if self.review(say('Эмуляция LF','LF simulation'),command+' '+args):
            self.owner.start_studio(one_command(command,args,1800000),say('Эмуляция LF','LF simulation'),rf=True,hold=True,duration_ms=self.sim_duration.value()*60000)

    def save_result(self,result):
        dump=result['dump'];uid=result.get('uid','');protocol=result.get('protocol',dump.metadata.get('protocol',''))
        name=protocol+' · '+(uid or say('снимок','snapshot'))
        cid=self.owner.library.create_card(name,dump,uid=uid,protocol=protocol,notes=result.get('verification',say('Снимок лаборатории','Laboratory snapshot')))
        self.owner.refresh_library();self.status.setText(say('Сохранено в библиотеку: ','Saved to library: ')+name)
        return cid

    def traces_page(self):
        w,layout=panel();toolbar=row(layout);self.trace_protocol=QComboBox();self.trace_protocol.addItems(['14a','14b','15','mf','iclass','legic','felica','hitag','iso7816']);toolbar.addWidget(self.trace_protocol)
        self.button(toolbar,'Записать обмен','Capture exchange',lambda:self.run(traces(self.trace_protocol.currentText(),capture=True),say('Запись обмена','Capturing exchange'),True,self.show_trace))
        self.button(toolbar,'Загрузить TRACE…','Load TRACE…',self.load_trace)
        self.button(toolbar,'Из буфера платы','From device buffer',lambda:self.run(traces(self.trace_protocol.currentText()),say('Чтение трассы','Reading trace'),on_done=self.show_trace))
        self.trace_filter=QLineEdit();self.trace_filter.setPlaceholderText(say('Фильтр: команда, HEX, направление','Filter: command, HEX, direction'));self.trace_filter.textChanged.connect(self.filter_trace);layout.addWidget(self.trace_filter)
        self.trace_table=table([say('Начало','Start'),say('Конец','End'),say('Направление','Direction'),'HEX','CRC',say('Команда','Command')],[90,90,90,260,70,220]);layout.addWidget(self.trace_table)
        toolbar=row(layout);self.button(toolbar,'Экспорт CSV…','Export CSV…',self.export_trace,False)
        layout.addWidget(Guidance(self.owner,label('«Остановить» завершает захват и затем сохраняет буфер. Таблица показывает разбор клиента, исходный TRACE остаётся в файлах сеанса.','Stop ends capture and then saves the buffer. The table uses the client decoder; the original TRACE is kept in the session files.')))
        return w

    def show_trace(self,result):
        self.trace_frames=parse_trace(result['output']);self.filter_trace()
        if not self.trace_frames:self.status.setText(say('Клиент не вернул разбираемых кадров. Исходный вывод сохранён.','No parseable frames were returned. The original output was saved.'))

    def filter_trace(self):
        query=self.trace_filter.text().casefold()
        fill_table(self.trace_table,[tuple(asdict(f).values()) for f in self.trace_frames if query in ' '.join(asdict(f).values()).casefold()])
        for r in range(self.trace_table.rowCount()):
            color=QColor('#92dcc2') if re.search(r'tag|card',self.trace_table.item(r,2).text(),re.I) else QColor('#93bff0')
            self.trace_table.item(r,2).setForeground(color)
            if re.search(r'fail|bad|!|err',self.trace_table.item(r,4).text(),re.I):self.trace_table.item(r,4).setForeground(QColor('#f29b9b'))

    def load_trace(self):
        name,_=QFileDialog.getOpenFileName(self,'TRACE','','Trace (*.trace *.trc)')
        if name:self.run(traces(self.trace_protocol.currentText(),Path(name)),say('Загрузка трассы','Loading trace'),on_done=self.show_trace,offline=True)

    def export_trace(self):
        name,_=QFileDialog.getSaveFileName(self,'CSV','trace.csv','CSV (*.csv)')
        if name:
            with Path(name).open('w',encoding='utf-8-sig',newline='') as f:
                writer=csv.writer(f);writer.writerow(TraceFrame.__dataclass_fields__)
                writer.writerows(tuple(asdict(frame).values()) for frame in self.trace_frames)

    def signal_page(self):
        w,layout=panel();form=QFormLayout();layout.addLayout(form)
        self.lf_divisor=QSpinBox();self.lf_divisor.setRange(19,255);self.lf_divisor.setValue(95)
        self.lf_bps=QSpinBox();self.lf_bps.setRange(1,8);self.lf_bps.setValue(8)
        self.lf_dec=QSpinBox();self.lf_dec.setRange(1,8);self.lf_dec.setValue(1)
        self.lf_avg=QCheckBox();self.lf_trigger=QSpinBox();self.lf_trigger.setRange(0,128)
        self.lf_skip=QSpinBox();self.lf_skip.setRange(0,1000000)
        for name,widget in [(say('Делитель LF','LF divisor'),self.lf_divisor),(say('Бит на отсчёт','Bits per sample'),self.lf_bps),('Decimation',self.lf_dec),('Averaging',self.lf_avg),(say('Порог','Trigger'),self.lf_trigger),(say('Пропустить отсчётов','Skip samples'),self.lf_skip)]:form.addRow(name,widget)
        toolbar=row(layout);self.button(toolbar,'Получить конфигурацию','Get configuration',lambda:self.run(one_command('lf config'),say('Параметры LF','LF settings'),on_done=self.show_lf_config))
        self.button(toolbar,'Применить конфигурацию…','Apply configuration…',self.apply_lf_config)
        self.button(toolbar,'Сбросить параметры…','Reset settings…',self.reset_lf_config)
        toolbar=row(layout);self.button(toolbar,'Получить сигнал','Capture signal',lambda:self.run(capture_signal(),say('Захват LF','LF capture'),True,self.show_signal))
        self.button(toolbar,'Открыть PM3 / WAV…','Open PM3 / WAV…',self.open_signal,False)
        self.rate=QDoubleSpinBox();self.rate.setRange(1,100000000);self.rate.setDecimals(2);self.rate.setValue(125000);toolbar.addWidget(label('Гц отсчётов','Sample rate Hz'));toolbar.addWidget(self.rate)
        self.signal_plot=SignalPlot();layout.addWidget(self.signal_plot)
        toolbar=row(layout);self.button(toolbar,'Спектр FFT','FFT spectrum',self.show_spectrum,False)
        self.button(toolbar,'Сигнал','Waveform',lambda:self.signal_plot.set_points(self.samples),False)
        self.button(toolbar,'Определить модуляцию','Detect modulation',self.detect_modulation)
        self.button(toolbar,'Сохранить PM3…','Save PM3…',self.export_samples,False)
        self.signal_info=label('Колесо — масштаб; перетаскивание — прокрутка. Частота отсчётов задаётся отдельно от частоты несущей.','Wheel to zoom; drag to pan. Sample rate is separate from the carrier frequency.');layout.addWidget(self.signal_info)
        return w

    def show_lf_config(self,result):
        text=result['output']
        for pattern,widget in [(r'divisor\D+(\d+)',self.lf_divisor),(r'bits per sample\D+(\d+)',self.lf_bps),(r'decimation\D+(\d+)',self.lf_dec),(r'trigger threshold\D+(\d+)',self.lf_trigger),(r'samples to skip\D+(\d+)',self.lf_skip)]:
            m=re.search(pattern,text,re.I)
            if m:widget.setValue(int(m[1]))
        m=re.search(r'averaging\D+(Yes|No|0|1)\b',text,re.I)
        if m:self.lf_avg.setChecked(m[1].lower() in {'yes','1'})

    def apply_lf_config(self):
        args=f'--divisor {self.lf_divisor.value()} --bps {self.lf_bps.value()} --dec {self.lf_dec.value()} --avg {int(self.lf_avg.isChecked())} --trig {self.lf_trigger.value()} --skip {self.lf_skip.value()}'
        self.run_reviewed('lf config',args,on_done=self.show_lf_config)

    def reset_lf_config(self):
        # Explicit documented default sampling configuration, not a guessed prior state.
        self.lf_divisor.setValue(95);self.lf_bps.setValue(8);self.lf_dec.setValue(1);self.lf_avg.setChecked(True);self.lf_trigger.setValue(0);self.lf_skip.setValue(0);self.apply_lf_config()

    def show_signal(self,result):
        self.samples=result['samples'];self.sample_rate=result.get('sample_rate');self.signal_path=result.get('path')
        if self.sample_rate:self.rate.setValue(self.sample_rate)
        self.signal_plot.set_points(self.samples);self.signal_info.setText(say('Отсчётов: ','Samples: ')+str(len(self.samples))+(' · '+str(self.sample_rate)+' Hz' if self.sample_rate else say(' · Укажите частоту отсчётов для FFT.',' · Set the sample rate for FFT.')))

    def open_signal(self):
        name,_=QFileDialog.getOpenFileName(self,say('Сигнал','Signal'),'','Signal (*.pm3 *.wav *.txt)')
        if name:
            values,rate=load_samples(Path(name));self.show_signal({'samples':values,'sample_rate':rate,'path':name})

    def show_spectrum(self):
        spectrum=fft_spectrum(self.samples,self.rate.value());self.signal_plot.set_points([db for hz,db in spectrum])
        hz,db=max(spectrum,key=lambda pair:pair[1]);self.signal_info.setText(f'FFT · peak {hz:.2f} Hz · {db:.2f} dB · bin {spectrum[1][0]:.2f} Hz')

    def detect_modulation(self):
        if not self.samples:raise ValueError('Capture or load samples first.')
        values=list(self.samples)
        def flow(directory):
            path=directory/'analysis.pm3';path.write_text('\n'.join(str(round(x)) for x in values)+'\n',encoding='ascii')
            yield Request('data load','-f '+_path_token(path,existing=True))
            output,_=yield Request('data autodemod',timeout=120000)
            return {'output':output,'verification':'signal-analysis-not-card-identity'}
        self.run(flow,say('Анализ модуляции','Modulation analysis'),offline=True)

    def export_samples(self):
        if not self.samples:raise ValueError('No samples.')
        name,_=QFileDialog.getSaveFileName(self,'PM3','signal.pm3','PM3 (*.pm3)')
        if name:Path(name).write_text('\n'.join(str(round(x)) for x in self.samples)+'\n',encoding='ascii')

    def files_page(self):
        w,layout=panel();toolbar=row(layout)
        self.button(toolbar,'Список SPIFFS','List SPIFFS',lambda:self.run(one_command('mem spiffs tree'),say('Файлы платы','Board files'),on_done=self.show_files))
        self.button(toolbar,'Информация о памяти','Memory information',lambda:self.run(one_command('mem info'),say('Внешняя память','External flash')))
        self.files_table=table([say('Имя','Name'),say('Размер / сведения','Size / details')]);self.files_table.itemSelectionChanged.connect(self.file_selected);layout.addWidget(self.files_table)
        form=QFormLayout();layout.addLayout(form);self.board_file=QLineEdit();self.board_dest=QLineEdit();form.addRow(say('Файл платы','Board filename'),self.board_file);form.addRow(say('Новое имя','New name'),self.board_dest)
        self.upload_new=QCheckBox(say('Загрузить как новый файл без перезаписи','Upload as a new file, without overwriting'));layout.addWidget(self.upload_new)
        toolbar=row(layout);self.button(toolbar,'Скачать','Download',lambda:self.run(download_spiffs(self.board_file.text()),say('Загрузка с платы','Downloading from board'),on_done=lambda r:self.status.setText(r['path'])))
        self.button(toolbar,'Загрузить на плату…','Upload to board…',self.upload_board_file)
        self.button(toolbar,'Переименовать…','Rename…',lambda:self.board_file_action('rename'))
        self.button(toolbar,'Копировать…','Copy…',lambda:self.board_file_action('copy'))
        self.button(toolbar,'Удалить…','Remove…',lambda:self.board_file_action('remove'))
        self.file_view=QPlainTextEdit();self.file_view.setReadOnly(True);layout.addWidget(self.file_view)
        self.button(layout,'Просмотреть файл','View file',lambda:self.run(one_command('mem spiffs view','-f '+spiffs_file(self.board_file.text())),say('Просмотр файла платы','Viewing board file'),on_done=lambda r:self.file_view.setPlainText(r['output'])))
        return w

    def show_files(self,result):
        from .studio_flows import spiffs_tree
        entries=spiffs_tree(result['output'])
        fill_table(self.files_table,sorted(entries.items()));self.file_view.setPlainText(result['output'])

    def file_selected(self):
        r=self.files_table.currentRow()
        if r>=0 and self.files_table.item(r,0):self.board_file.setText(self.files_table.item(r,0).text())

    def board_file_action(self,action):
        from .studio_flows import change_spiffs
        name=spiffs_file(self.board_file.text());args=('-f '+name if action=='remove' else '-s '+name+' -d '+spiffs_file(self.board_dest.text()))
        if self.review('SPIFFS',action+' '+args):
            self.run(change_spiffs(action,name,self.board_dest.text()),'SPIFFS · '+action,on_done=lambda r:self.file_view.setPlainText(json.dumps(r,indent=2)))

    def upload_board_file(self):
        from .studio_flows import upload_spiffs
        name=spiffs_file(self.board_file.text());filename,_=QFileDialog.getOpenFileName(self,say('Загрузить файл','Upload file'))
        if not filename:return
        source=Path(filename)
        if source.stat().st_size>2*1024*1024:raise ValueError('Upload exceeds 2 MiB.')
        payload=source.read_bytes()
        if not self.review(say('Загрузка в SPIFFS','Upload to SPIFFS'),f'{source.name} → {name}\n{len(payload)} bytes'):return
        self.run(upload_spiffs(name,payload,self.upload_new.isChecked()),say('Запись файла в SPIFFS','Uploading SPIFFS file'),on_done=lambda r:self.file_view.setPlainText(json.dumps(r,indent=2)))

    def diagnostics_page(self):
        w,layout=panel();toolbar=row(layout)
        self.button(toolbar,'Отчёт для диагностики…','Diagnostic report…',self.open_diagnostics,False)
        self.button(toolbar,'Статус платы','Device status',lambda:self.run(one_command('hw status'),say('Статус платы','Device status')))
        self.button(toolbar,'Внешнее поле HF','External HF field',lambda:self.run(one_command('hw detectreader','-H',120000),say('Внешнее HF-поле','External HF field'),True))
        self.button(toolbar,'Внешнее поле LF','External LF field',lambda:self.run(one_command('hw detectreader','-L',120000),say('Внешнее LF-поле','External LF field'),True))
        self.decay_form=ParameterForm('hw decay',lambda:None);layout.addWidget(self.decay_form)
        self.button(layout,'Измерить спад HF','Measure HF decay',lambda:self.run_reviewed('hw decay',self.decay_form.arguments(),True,self.show_decay))
        self.decay_plot=SignalPlot();layout.addWidget(self.decay_plot)
        self.threshold_form=ParameterForm('hw sethfthresh',lambda:None);layout.addWidget(self.threshold_form)
        self.button(layout,'Применить пороги…','Apply thresholds…',lambda:self.run_form('hw sethfthresh',self.threshold_form))
        self.button(layout,'Стандартные пороги…','Default thresholds…',lambda:self.run_reviewed('hw sethfthresh','-t 7 -i 20 -l 8'))
        layout.addWidget(Guidance(self.owner,label('В установленной GUI-BlueShark сборке автономный модуль — LF_SAMYRUN (HID26). Он управляется кнопкой платы: чтение, эмуляция, запись на заготовку. Другие standalone-модули требуют другой сборки.','The installed GUI-BlueShark image contains LF_SAMYRUN (HID26). The device button controls reading, simulation and writing a blank tag. Other standalone modules require a different build.')))
        self.button(layout,'Запустить установленный автономный режим…','Start installed standalone mode…',self.start_standalone)
        layout.addWidget(Guidance(self.owner,label('Диагностика антенн не измеряет заряд Blue Shark и не гарантирует устранение Auth2.','Antenna diagnostics do not measure Blue Shark battery charge or guarantee an Auth2 fix.')))
        return w

    def open_diagnostics(self):
        from .diagnostics_ui import DiagnosticBundleDialog
        self.diagnostics_dialog=DiagnosticBundleDialog(self.owner)
        self.diagnostics_dialog.show()

    def show_decay(self,result):
        # Raw numerical table is retained in the session in addition to the plot.
        values=[]
        for line in result['output'].splitlines():
            m=re.search(r'^\s*(?:\[[=+!]\]\s*)?(\d+)\s*\|\s*(\d+(?:\.\d+)?)\s*\|',line)
            if m:
                columns=line.split('|')
                if len(columns)>2:
                    value=re.search(r'\d+(?:\.\d+)?',columns[2])
                    if value:values.append(float(value[0]))
        if values:self.decay_plot.set_points(values)

    def start_standalone(self):
        if not self.review('Standalone',say('Запустить установленный модуль? Кнопка платы управляет чтением / эмуляцией / записью.','Start the installed module? Its button controls read / simulate / write.')):return
        def flow(directory):
            output,_=yield Request('hw version')
            if not re.search(r'^\s*OS\.{2,}\s+\S',output,re.M):raise ValueError('No fresh firmware response.')
            yield Request('hw status')
            output,_=yield Request('hw standalone')
            return {'output':output,'verification':'installed-mode-requested-not-external-reader-tested'}
        self.run(flow,'Standalone',True,hold=True)

    def console_page(self):
        w,layout=panel();self.command_search=QLineEdit();self.command_search.setPlaceholderText(say('Найти команду клиента','Find a client command'));layout.addWidget(self.command_search)
        self.command_choice=QComboBox();self.command_choice.setMaxVisibleItems(20)
        for cmd in sorted(COMMANDS):self.command_choice.addItem(cmd,cmd)
        layout.addWidget(self.command_choice)
        self.command_form_host=QVBoxLayout();layout.addLayout(self.command_form_host);self.command_form=None
        self.console_manual=QCheckBox(say('Ввести строку вручную','Enter command line manually'));layout.addWidget(self.console_manual)
        self.console_offline=QCheckBox(say('Офлайн-анализ без устройства','Offline analysis without a device'));layout.addWidget(self.console_offline)
        self.console_line=QLineEdit();self.console_line.setFont(mono_font());self.console_line.setPlaceholderText('hw status');layout.addWidget(self.console_line)
        self.console_timeout=QSpinBox();self.console_timeout.setRange(5,1800);self.console_timeout.setValue(120);self.console_timeout.setSuffix(say(' с',' s'));layout.addWidget(self.console_timeout)
        self.command_help=QPlainTextEdit();self.command_help.setReadOnly(True);self.command_help.setMaximumHeight(140);layout.addWidget(self.command_help)
        self.button(layout,'Выполнить с просмотром…','Review and run…',self.run_console)
        self.button(layout,'Избранные действия…','Quick actions…',self.open_quick_actions,False)
        self.button(layout,'Каталог выбранного клиента…','Selected client catalog…',self.open_client_catalog,False)
        self.history=QComboBox();self.history.setMinimumContentsLength(20);layout.addWidget(self.history)
        self.history.activated.connect(self.restore_history)
        self.button(layout,'Очистить историю','Clear history',self.clear_history,False)
        layout.addWidget(label('Сценарии Lua / Python','Lua / Python scripts'))
        self.script_choice=QComboBox();layout.addWidget(self.script_choice);toolbar=row(layout)
        self.button(toolbar,'Список сценариев','List scripts',self.scan_scripts,False)
        self.script_args=QLineEdit();self.script_args.setPlaceholderText(say('Аргументы сценария','Script arguments'));toolbar.addWidget(self.script_args)
        self.button(toolbar,'Запустить сценарий…','Run script…',self.run_script)
        self.wiegand=QLineEdit();self.wiegand.setPlaceholderText('HEX / binary');layout.addWidget(self.wiegand)
        self.button(layout,'Форматы Wiegand','Wiegand formats',lambda:self.choose_command('wiegand list'),False)
        self.button(layout,'Декодер MAD','MAD decoder',lambda:self.choose_command('mad decode'),False)
        self.button(layout,'Сравнение через клиент','Client binary comparison',lambda:self.choose_command('data diff'),False)
        self.command_choice.setCurrentIndex(self.command_choice.findData('hw status'))
        self.command_choice.currentIndexChanged.connect(self.command_changed);self.command_search.textChanged.connect(self.filter_commands);self.command_changed()
        self.load_history();return w

    def filter_commands(self):
        query=self.command_search.text().casefold();self.command_choice.blockSignals(True);self.command_choice.clear()
        for cmd in sorted(COMMANDS):
            if query in (cmd+' '+COMMANDS[cmd]['description']).casefold():self.command_choice.addItem(cmd,cmd)
        self.command_choice.blockSignals(False);self.command_changed()

    def choose_command(self,command):
        self.sections.setCurrentRow(7);self.command_search.clear();self.command_choice.setCurrentIndex(self.command_choice.findData(command))

    def command_changed(self):
        if self.command_form:self.command_form_host.removeWidget(self.command_form);self.command_form.hide();self.command_form.deleteLater();self.command_form=None
        command=self.command_choice.currentData()
        if command:
            self.command_form=ParameterForm(command,lambda:None);self.command_form_host.addWidget(self.command_form);self.command_help.setPlainText(COMMANDS[command]['help'])

    def run_form(self,command,form,rf=False):self.run_reviewed(command,form.arguments(),rf=rf)

    def run_reviewed(self,command,args='',rf=None,on_done=None,offline=False):
        self.confirm_expert(command,args)
        request=Request(command,args);line=request.line
        if self.review(say('Проверка команды','Review command'),line):
            self.remember_command(line);self.run(one_command(command,args,self.console_timeout.value()*1000 if hasattr(self,'console_timeout') else 120000),command,
                                               False if offline else uses_rf(command) if rf is None else rf,on_done,offline=offline)

    def run_console(self):
        if self.console_manual.isChecked():command,args=split_command(self.console_line.text())
        else:
            command=self.command_choice.currentData()
            if not command or self.command_form is None:raise ValueError('Choose a command.')
            args=self.command_form.arguments()
        self.run_reviewed(command,args,offline=self.console_offline.isChecked())

    def open_quick_actions(self):
        from .quick_actions import QuickAction
        from .quick_actions_ui import QuickActionsDialog
        previous=getattr(self,'quick_actions_dialog',None)
        if previous is not None and previous.isVisible():previous.raise_();return
        if previous is not None:previous.deleteLater()
        try:
            if self.console_manual.isChecked():command,args=split_command(self.console_line.text())
            else:
                command=self.command_choice.currentData();args=self.command_form.arguments() if self.command_form else ''
            initial=QuickAction('',(command+(' '+args if args else ''),),self.console_timeout.value(),self.console_offline.isChecked())
        except (ValueError,TypeError):initial=None
        self.quick_actions_dialog=QuickActionsDialog(self,initial)
        self.quick_actions_dialog.show()

    def run_quick_action(self, action):
        from .quick_actions import action_flow
        action=action.validated()
        self.owner._lab_available()
        for line in action.lines:self.confirm_expert(*split_command(line))
        details='\n'.join(f'{i+1}. {line}' for i,line in enumerate(action.lines))
        details+='\n\n'+say('Без устройства' if action.offline else 'Подключённое устройство',
                            'Without a device' if action.offline else 'Connected device')
        if not self.review(action.name,details):return False
        self.run(action_flow(action),action.name,rf=not action.offline and any(uses_rf(split_command(line)[0]) for line in action.lines),offline=action.offline)
        return True

    def confirm_expert(self,command,args=''):
        snapshot=getattr(self.owner,'client_catalog_snapshot',None)
        if snapshot is not None:
            from .client_catalog import confirmed_command
            confirmed_command(snapshot,self.owner.client_path.text(),command,args)
        elif Path(self.owner.client_path.text()).is_file():
            from .client_catalog import bundled_client_matches
            from .catalog_snapshot import CatalogError
            if not bundled_client_matches(self.owner.client_path.text()):
                raise CatalogError('Update the command catalog for the selected client before using these tools.')

    def open_client_catalog(self):
        from .catalog_ui import CatalogDialog
        previous=getattr(self,'catalog_dialog',None)
        if previous is not None and previous.isVisible():previous.raise_();return
        self.catalog_dialog=CatalogDialog(self.owner,snapshot=self.owner.client_catalog_snapshot,on_open=self.choose_command)
        self.catalog_dialog.snapshot_updated.connect(self.catalog_updated)
        self.catalog_dialog.show()

    def catalog_updated(self,snapshot):
        self.owner.client_catalog_snapshot=snapshot
        self.status.setText(say('Каталог выбранного клиента обновлён.','Selected client catalog updated.'))

    def load_history(self):
        path=self.owner.data_dir/'studio-history.json'
        if not path.is_file():return
        try:
            if path.stat().st_size>1024*1024:return
            data=json.loads(path.read_text(encoding='utf-8'))
        except (ValueError,OSError):return
        if isinstance(data,list):
            for line in data[-100:]:
                if isinstance(line,str):self.history.addItem(line,line)

    def restore_history(self,index):
        self.console_line.setText(self.history.itemData(index) or '')
        self.console_manual.setChecked(True)

    def remember_command(self,line):
        existing=[self.history.itemData(i) for i in range(self.history.count())]
        existing=[s for s in existing if s!=line]+[line]
        self.history.clear()
        for command in existing[-100:]:self.history.addItem(command,command)
        (self.owner.data_dir/'studio-history.json').write_text(json.dumps(existing[-100:],ensure_ascii=False,indent=2),encoding='utf-8')

    def clear_history(self):
        self.history.clear();(self.owner.data_dir/'studio-history.json').write_text('[]\n',encoding='utf-8')

    def scan_scripts(self):
        self.script_choice.clear();exe=Path(self.owner.client_path.text())
        for folder,extension in [('luascripts','*.lua'),('pyscripts','*.py')]:
            base=exe.parent/folder
            if base.is_dir():
                for path in sorted(base.glob(extension)):self.script_choice.addItem(path.name,str(path.resolve()))

    def run_script(self):
        if self.console_offline.isChecked():raise ValueError(say('Сценарии запускаются в режиме устройства. Для офлайн-анализа выберите отдельную команду.','Scripts use device mode. Select an individual command for offline analysis.'))
        name=self.script_choice.currentData()
        if not name:raise ValueError('Choose a script.')
        path=Path(name)
        if not path.is_file():raise ValueError('Script not found.')
        self.run_reviewed('script run',_path_token(path,existing=True)+(' '+self.script_args.text().strip() if self.script_args.text().strip() else ''),rf=True)
