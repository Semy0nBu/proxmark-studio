"""Shared source/blank presentation; no device I/O or independent write approval."""
from PySide6.QtCore import Qt,QSize
from PySide6.QtWidgets import QWidget,QFrame,QLabel,QVBoxLayout,QGridLayout,QHBoxLayout,QLayout
from .i18n import tr
from .transfer import Check,source_facts
from .ui import Disclosure
from .interface_mode import bind_mode


class TransferPanel(QWidget):
    def __init__(self,parent=None,*,owner=None):
        super().__init__(parent)
        self.compact=False;self._checks=();self._result=None;self._target=('', '', '')
        outer=QVBoxLayout(self);outer.setContentsMargins(0,0,0,0)
        outer.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.stage_strip=QWidget();stages=QHBoxLayout(self.stage_strip);stages.setContentsMargins(0,0,0,0)
        self.stages=[]
        for index,title in enumerate((tr('Источник'),tr('Заготовка'),tr('План'),tr('Запись'),tr('Проверка'))):
            item=QLabel(str(index+1)+'  '+title);item.setWordWrap(True);stages.addWidget(item,1);self.stages.append(item)
        outer.addWidget(self.stage_strip)
        self.cards=QGridLayout();outer.addLayout(self.cards)
        self.source_box,self.source=self._card(tr('Источник'))
        self.target_box,self.target=self._card(tr('Заготовка'))
        self.cards.addWidget(self.source_box,0,0);self.cards.addWidget(self.target_box,0,1)
        self.heading=QLabel();self.heading.setWordWrap(True)
        self.heading.setTextFormat(Qt.TextFormat.PlainText);outer.addWidget(self.heading)
        self.explanation=QLabel(self);self.explanation.setTextFormat(Qt.TextFormat.PlainText);self.explanation.hide()
        self.important=QWidget();self.important_layout=QGridLayout(self.important)
        self.important_layout.setContentsMargins(0,0,0,0);outer.addWidget(self.important)
        self.details=Disclosure(tr('Совместимость и ограничения'))
        self.checks_layout=QGridLayout();self.details.body.addLayout(self.checks_layout);outer.addWidget(self.details)
        for layout in (self.important_layout,self.checks_layout):
            layout.setColumnStretch(1,1);layout.setHorizontalSpacing(8);layout.setVerticalSpacing(4)
        self.status='pending';self.set_source(None);self.reset_target();self.set_stage(0)
        bind_mode(owner,self)

    def set_interface_mode(self,mode):
        self.compact=mode=='compact'
        self.stage_strip.setVisible(not self.compact)
        self.details.header.setVisible(self.compact)
        self.details.setExpanded(not self.compact)
        self._render_source();self._render_target();self._render_checks()

    def set_stage(self,index):
        self.stage=index
        for number,item in enumerate(self.stages):
            item.setStyleSheet('padding:5px;border-bottom:2px solid '+('#7db7fa;color:#c5dfff' if number==index else '#383838;color:#aaa'))
        self._render_heading()

    def _card(self,title):
        box=QFrame();box.setObjectName('transferCard')
        box.setStyleSheet('QFrame#transferCard {background:#25282d;border:1px solid #3b4653;border-radius:8px;} QFrame#transferCard QLabel {background:transparent;border:0;}')
        layout=QVBoxLayout(box);layout.setContentsMargins(12,10,12,10)
        heading=QLabel(title);heading.setObjectName('section');layout.addWidget(heading)
        body=QLabel();body.setWordWrap(True);body.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(body);layout.addStretch();return box,body

    def set_source(self,dump,record=None,name='',origin=''):
        self._source_description=None
        self.facts=source_facts(dump,record,name);self._has_source=dump is not None;self._origin=origin
        self._render_source()

    def set_source_description(self,value):
        self._source_description=value;self._render_source()

    def _render_source(self):
        if self._source_description is not None:
            self.source.setText(self._source_description);return
        if not self._has_source:
            self.source.setText(tr('Выберите сохранённую карту, версию или файл.'));return
        facts=self.facts;parts=[facts.name,facts.protocol or tr('Семейство не указано')]
        if facts.uid:parts.append('UID / ID: '+facts.uid)
        if facts.known is not None:parts.append(tr('Известно {0} из {1} байт',facts.known,facts.size))
        if not self.compact:parts.extend((facts.scope,self._origin))
        self.source.setText('\n'.join(part for part in parts if part))
        self.source.setToolTip(facts.scope)

    def reset_target(self):
        self._target=('', '', '');self.set_stage(0);self._render_target()
        self.show_checks([Check(tr('Проверка'),'pending',tr('Сначала выберите источник и проверьте заготовку.'))])

    def set_target(self,title,uid='',details=''):
        self._target=(title,uid,details);self.set_stage(1);self._render_target()

    def _render_target(self):
        title,uid,details=self._target
        if not title:
            value=tr('Не проверена') if self.compact else tr('Ещё не проверена. Снимок источника остаётся в программе; на антенне должна быть заготовка.')
        else:value='\n'.join(p for p in (title,'UID / ID: '+uid if uid else '',details if not self.compact else '') if p)
        self.target.setText(value);self.target.setToolTip(details)

    def show_checks(self,checks,ready=False):
        self._checks=tuple(checks);self._result=None
        self.status=('blocked' if any(c.state=='blocked' for c in self._checks) else
                     'ready' if ready and not any(c.state=='pending' for c in self._checks) else 'pending')
        if self.status=='ready':self.set_stage(2)
        self._render_checks()

    def _render_heading(self):
        captions={'blocked':tr('Запись недоступна: исправьте ограничения'),
                  'ready':tr('Заготовка проверена. Можно подтвердить план.'),
                  'pending':tr('Совместимость ещё проверяется'),
                  'verified':tr('Результат подтверждён чтением'),
                  'stopped':tr('Операция не завершена; перед новой записью нужна новая проверка')}
        if self.compact:
            captions.update(blocked=tr('Запись недоступна'),ready=tr('План готов'),pending=tr('Не проверено'),stopped=tr('Запись не завершена'))
        caption=captions[self.status]
        if self.status=='ready' and getattr(self,'stage',0) in (3,4):
            caption=tr('Запись…') if self.stage==3 else tr('Проверка чтением…')
        self.heading.setText(caption)
        self.heading.setStyleSheet('color:'+('#ef9b9b' if self.status=='blocked' else '#83d5b7' if self.status in ('ready','verified') else '#e5bc77'))

    @staticmethod
    def _fill_rows(layout,lines):
        if getattr(layout,'_transfer_lines',None)==lines:return
        layout._transfer_lines=lines
        while layout.count():
            item=layout.takeAt(0)
            if item.widget():item.widget().hide();item.widget().deleteLater()
        colors={'ok':'#83d5b7','limit':'#a6bdd8','pending':'#e5bc77','blocked':'#ef9b9b'}
        symbols={'ok':'+','limit':'•','pending':'…','blocked':'!'}
        for row,(state,text) in enumerate(lines):
            marker=QLabel(symbols[state]);marker.setStyleSheet('color:'+colors[state]+';font-weight:600')
            marker.setAlignment(Qt.AlignmentFlag.AlignTop)
            label=QLabel(text);label.setWordWrap(True);label.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(marker,row,0);layout.addWidget(label,row,1)

    def _render_checks(self):
        self._render_heading()
        important=[];details=[]
        for check in self._checks:
            full=(check.state,check.title+': '+check.detail)
            if self.compact and (check.critical or check.state=='blocked'):
                important.append((check.state,check.title+': '+(check.brief or check.detail)))
            else:details.append(full)
        if self._result is not None:
            success,detail=self._result
            important=[('ok' if success else 'blocked',detail)]
            if success:details=[]
        self.explanation.setText('\n'.join(text for _,text in important+details))
        self._fill_rows(self.important_layout,tuple(important));self.important.setVisible(bool(important))
        self._fill_rows(self.checks_layout,tuple(details));self.details.setVisible(bool(details))

    def finish(self,success,detail):
        self._result=(success,detail);self.status='verified' if success else 'stopped'
        if success:self.set_stage(4)
        self._render_checks()

    def resizeEvent(self,event):
        super().resizeEvent(event)
        narrow=self.width()<600
        self.cards.addWidget(self.target_box,1 if narrow else 0,0 if narrow else 1)

    def minimumSizeHint(self):
        return QSize(280,0)
