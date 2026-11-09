"""Colour-coded, read-only Classic sector inspector."""
from .i18n import tr
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor,QBrush,QTextCursor
from PySide6.QtWidgets import (QCheckBox,QDialog,QGridLayout,QHBoxLayout,QLabel,QPushButton,QScrollArea,
    QSplitter,QTableWidget,QTableWidgetItem,QVBoxLayout,QWidget)
from .operations import CLASSIC_GEOMETRIES,sector_first_block,sector_trailer
from .writing import access_conditions
from .theme import mono_font
from .presentation import configure_table,install_table_copy
import re

COLOURS={'complete':('#183e35','#92dcc2'),'partial':('#49391d','#f1c671'),
         'missing':('#30333a','#a9afb9'),'invalid':('#48272e','#f1a2ae')}
DATA_ACCESS={0:'Чтение A/B · запись A/B',1:'Чтение A/B · запись запрещена',2:'Чтение A/B · запись запрещена',
             3:'Чтение B · запись B',4:'Чтение A/B · запись B',5:'Чтение B · запись запрещена',
             6:'Чтение A/B · запись B',7:'Доступ запрещён'}


def sector_info(dump,sector):
    count=next((s for s,size in CLASSIC_GEOMETRIES.values() if size==len(dump.data)),None)
    if dump.block_size!=16 or count is None or not 0<=sector<count:raise ValueError(tr('Нужен образ MIFARE Classic Mini/1K/2K/4K.'))
    first,last=sector_first_block(sector),sector_trailer(sector)
    known=set(dump.metadata.get('partial_dump',{}).get('known_bytes',range(len(dump.data))))
    present=sum(all(i in known for i in range(b*16,b*16+16)) for b in range(first,last+1))
    total_known=sum(i in known for i in range(first*16,(last+1)*16))
    acl_known=all(i in known for i in range(last*16+6,last*16+9))
    conditions=None
    if acl_known:
        try:conditions=access_conditions(dump.data[last*16:(last+1)*16])
        except ValueError:pass
    state=('missing' if total_known==0 else 'invalid' if acl_known and conditions is None else
           'complete' if present==last-first+1 else 'partial')
    keys={t:all(i in known for i in range(last*16+off,last*16+off+6)) for t,off in [('A',0),('B',10)]}
    return dict(sector=sector,first=first,last=last,present=present,count=last-first+1,state=state,
                known=known,keys=keys,conditions=conditions,total_known=total_known)


class MemoryMapDialog(QDialog):
    def __init__(self,owner,dump):
        super().__init__(owner);self.owner=owner;self.dump=dump;self.sector=0
        self.card_id=owner.card_id
        count=next((s for s,size in CLASSIC_GEOMETRIES.values() if size==len(dump.data)),None)
        if count is None or dump.block_size!=16:raise ValueError(tr('Карта секторов доступна для MIFARE Classic Mini/1K/2K/4K.'))
        self.setWindowTitle(tr('Карта памяти · MIFARE Classic'));screen=owner.screen().availableGeometry();self.resize(min(1080,screen.width()-40),min(860,screen.height()-70))
        outer=QVBoxLayout(self);outer.setSpacing(14)
        title=QLabel(owner.card_name.text());title.setWordWrap(True);title.setObjectName('title');title.setTextFormat(Qt.TextFormat.PlainText);outer.addWidget(title)
        states=[sector_info(dump,s)['state'] for s in range(count)]
        self.stats=QLabel(tr('{0} секторов · {1} блоков · ', f'{count}', f'{len(dump.data) // 16}')+
                         tr('полных {0} · частичных {1} · не прочитано {2} · ошибок {3}', f"{states.count('complete')}", f"{states.count('partial')}", f"{states.count('missing')}", f"{states.count('invalid')}"))
        self.stats.setWordWrap(True);outer.addWidget(self.stats)
        legend=QHBoxLayout()
        for key,label in [('complete',tr('Данные есть')),('partial',tr('Частично')),('missing',tr('Не прочитано')),('invalid',tr('Ошибка доступа'))]:
            chip=QLabel('  ● '+label+'  ');chip.setStyleSheet('color:'+COLOURS[key][1]);legend.addWidget(chip)
        legend.addStretch();outer.addLayout(legend)
        splitter=QSplitter(Qt.Orientation.Vertical);outer.addWidget(splitter,1)
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff);grid_widget=QWidget();grid=QGridLayout(grid_widget);grid.setSpacing(9);self.grid=grid
        self.tiles=[]
        for sector in range(count):
            info=sector_info(dump,sector);bg,fg=COLOURS[info['state']]
            a='●' if info['keys']['A'] else '○';b='●' if info['keys']['B'] else '○'
            tile=QPushButton(tr('СЕКТОР {0}\n{1}/{2} блоков\nA {3}    B {4}', f'{sector:02d}', f"{info['present']}", f"{info['count']}", f'{a}', f'{b}'))
            tile.setCheckable(True);tile.setMinimumSize(105,68)
            tile.setStyleSheet(f'QPushButton {{background:{bg};color:{fg};border:1px solid {bg};border-radius:10px;padding:9px;text-align:left;}} '
                              f'QPushButton:checked {{border:2px solid {fg};}} QPushButton:hover {{border:1px solid {fg};}}')
            tile.setToolTip(tr('Блоки {0}–{1} · известны {2} байт', f"{info['first']}", f"{info['last']}", f"{info['total_known']}"))
            tile.clicked.connect(lambda checked=False,s=sector:self.show_sector(s));grid.addWidget(tile,sector//8,sector%8);self.tiles.append(tile)
        scroll.setWidget(grid_widget);scroll.setMinimumHeight(90);splitter.addWidget(scroll)
        detail=QWidget();body=QVBoxLayout(detail);body.setContentsMargins(0,0,0,0)
        row=QHBoxLayout();self.heading=QLabel();self.heading.setObjectName('section');row.addWidget(self.heading,1)
        self.show_keys=QCheckBox(tr('Показать ключи'));self.show_keys.toggled.connect(lambda:self.show_sector(self.sector));row.addWidget(self.show_keys);body.addLayout(row)
        self.key_info=QLabel();self.key_info.setTextFormat(Qt.TextFormat.PlainText);self.key_info.setFont(mono_font());body.addWidget(self.key_info)
        self.table=QTableWidget(0,4);self.table.setHorizontalHeaderLabels([tr('Блок'),tr('Назначение'),tr('Доступ / состояние'),'HEX'])
        configure_table(self.table,[65,150,260,430]);install_table_copy(self.table);self.table.setMinimumHeight(90)
        self.table.cellDoubleClicked.connect(self.jump_to_block);body.addWidget(self.table,1);splitter.addWidget(detail);splitter.setSizes([430,220])
        hint=QLabel(tr('A/B: ● поле есть в дампе, ○ неизвестно. Синий маркер — трейлер. Двойной щелчок по блоку откроет его в редакторе.'))
        hint.setWordWrap(True);hint.setObjectName('muted');outer.addWidget(hint)
        self.show_sector(0)
        self.reflow()

    def reflow(self):
        columns=max(3,min(8,(self.width()-55)//120))
        for sector,tile in enumerate(self.tiles):self.grid.addWidget(tile,sector//columns,sector%columns)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'tiles'):self.reflow()

    def show_sector(self,sector):
        self.sector=sector;info=sector_info(self.dump,sector)
        for i,tile in enumerate(self.tiles):tile.setChecked(i==sector)
        self.heading.setText(tr('Сектор {0} · блоки {1}–{2}', f'{sector}', f"{info['first']}", f"{info['last']}"))
        trailer=self.dump.data[info['last']*16:(info['last']+1)*16];parts=[]
        for kind,off in [('A',0),('B',10)]:
            parts.append(kind+': '+(tr('неизвестен') if not info['keys'][kind] else trailer[off:off+6].hex().upper() if self.show_keys.isChecked() else '••••••••••••'))
        self.key_info.setText('     '.join(parts));self.table.setRowCount(info['count'])
        for row,block in enumerate(range(info['first'],info['last']+1)):
            raw=self.dump.data[block*16:(block+1)*16];known=sum(i in info['known'] for i in range(block*16,(block+1)*16))
            is_trailer=block==info['last'];group=3 if is_trailer else min(2,row//(5 if sector>=32 else 1))
            access=(tr('Некорректны биты доступа') if info['state']=='invalid' else tr('Неизвестно') if info['conditions'] is None else f'C={info["conditions"][group]:03b} · '+
                    (tr('трейлер A/B/ACL') if is_trailer else tr(DATA_ACCESS[info['conditions'][group]])))
            value=' '.join(f'{byte:02X}' if block*16+i in info['known'] else '??' for i,byte in enumerate(raw))
            if is_trailer and not self.show_keys.isChecked():value=' '.join('••' if i<6 or i>=10 else value.split()[i] for i in range(16))
            for col,text in enumerate((str(block),tr('Трейлер A/B') if is_trailer else tr('Блок UID') if block==0 else tr('Данные'),tr('{0}/16 Б · ', f'{known}')+access,value)):
                item=QTableWidgetItem(text);item.setToolTip(text)
                if col==3:item.setFont(mono_font())
                if is_trailer:item.setForeground(QBrush(QColor('#93bff0')))
                self.table.setItem(row,col,item)

    def jump_to_block(self,row,column=0):
        try:current=self.owner._working_dump()
        except ValueError:current=None
        if current is None or self.owner.card_id!=self.card_id or current.data!=self.dump.data:
            self.heading.setText(tr('Редактор изменился · откройте карту памяти заново'));return
        block=self.sector_info_block(row);text=self.owner.hex_edit.toPlainText();tokens=list(re.finditer(r'[0-9A-Fa-f]{2}',text))
        cursor=self.owner.hex_edit.textCursor();cursor.setPosition(tokens[block*16].start());cursor.setPosition(tokens[block*16+15].end(),QTextCursor.MoveMode.KeepAnchor)
        self.owner.hex_edit.setTextCursor(cursor);self.owner.navigate(1);self.hide();self.owner.hex_edit.setFocus()

    def sector_info_block(self,row):return sector_first_block(self.sector)+row
