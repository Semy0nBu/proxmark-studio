"""Native source/version picker; choosing a source never touches hardware."""
from .i18n import tr, display_revision_note
from copy import deepcopy
from pathlib import Path
from PySide6.QtCore import Signal,Qt
from PySide6.QtWidgets import QWidget,QComboBox,QPushButton,QHBoxLayout,QVBoxLayout,QFileDialog,QLabel
from .core import parse_dump,DumpData
from .keys import format_key_timestamp
from .card_band import card_band



from .ui import SoftButton

class SourcePicker(QWidget):
    changed=Signal()
    def __init__(self,owner,band=None,parser=parse_dump):
        super().__init__();self.owner=owner;self.dump=None;self.record={};self.files={};self.loading=False;self.parser=parser
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0)
        row=QHBoxLayout();self.cards=QComboBox();self.cards.setMinimumContentsLength(14)
        self.cards.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.cards.addItem(tr('Выберите источник'),None)
        if owner.loaded_dump is not None and (not band or card_band({'protocol':owner.protocol.text()},owner.loaded_dump.metadata)==band):
            self.current=deepcopy(owner._working_dump());self.current_record={'name':owner.card_name.text(),'uid':owner.uid.text(),'protocol':owner.protocol.text()}
            self.cards.addItem(tr('Текущий снимок редактора'),('editor',''))
        for card in owner.library.list_cards():
            if band and card['rf_band']!=band:continue
            self.cards.addItem(card['name'],('card',card['id']))
        row.addWidget(self.cards,1);button=SoftButton(tr('Файл…'));button.clicked.connect(self.open_file);row.addWidget(button);layout.addLayout(row)
        self.versions=QComboBox();self.versions.setMinimumContentsLength(14)
        self.versions.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon);layout.addWidget(self.versions)
        self.info=QLabel(tr('Выберите карту и версию либо откройте файл.'));self.info.setWordWrap(True);self.info.setTextFormat(Qt.TextFormat.PlainText);self.info.setObjectName('muted');layout.addWidget(self.info)
        self.cards.currentIndexChanged.connect(self.selected);self.versions.currentIndexChanged.connect(self.version)
        if self.cards.count()>1:self.cards.setCurrentIndex(1)

    def selected(self,*_):
        self.loading=True;self.versions.clear()
        try:
            item=self.cards.currentData();self.dump=None;self.record={}
            if item and item[0]=='editor':self.dump=deepcopy(self.current);self.record=dict(self.current_record);self.versions.addItem(tr('Текущие байты'))
            elif item and item[0]=='file':self.dump=deepcopy(self.files[item[1]]);self.record={'name':Path(item[1]).name};self.versions.addItem(tr('Из файла'))
            elif item:
                self.record=self.owner.library.get_card(item[1])
                for rev in self.owner.library.list_revisions(item[1]):
                    self.versions.addItem(format_key_timestamp(rev['created_at'])+' · '+display_revision_note(rev['note']),rev['id'])
                    self.versions.setItemData(self.versions.count()-1,self.versions.itemText(self.versions.count()-1),Qt.ItemDataRole.ToolTipRole)
                self.dump=self.owner.library.get_dump(item[1],self.versions.currentData())
            self.info.setText((self.record.get('name','')+tr(' · {0} Б', f'{len(self.dump.data)}')+(tr(' · НЕПОЛНЫЙ') if self.dump.metadata.get('partial_dump') else '')) if self.dump else tr('Источник не выбран.'))
        except (ValueError,OSError,KeyError) as exc:self.dump=None;self.info.setText(str(exc))
        finally:self.loading=False
        self.changed.emit()

    def version(self,*_):
        if self.loading:return
        item=self.cards.currentData()
        if item and item[0]=='card':
            try:self.dump=self.owner.library.get_dump(item[1],self.versions.currentData());self.changed.emit()
            except (ValueError,OSError) as exc:self.dump=None;self.info.setText(str(exc));self.changed.emit()

    def open_file(self):
        name,_=QFileDialog.getOpenFileName(self,tr('Источник'),'',tr('Дампы (*.json *.bin *.mfd *.eml)'))
        if not name:return
        try:
            dump=self.parser(Path(name))
            if len(dump.data)==5:dump=DumpData(dump.data,dump.format,{**dump.metadata,'protocol':'EM410x','record_kind':'identifier'},5)
            self.files[name]=dump;self.cards.addItem(Path(name).name,('file',name));self.cards.setCurrentIndex(self.cards.count()-1)
        except (ValueError,OSError) as exc:self.info.setText(str(exc))
