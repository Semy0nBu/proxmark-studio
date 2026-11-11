"""Presentation preference only; neither mode changes device operations."""
import json
from pathlib import Path
from .i18n import tr
from .ui import Disclosure

MODES=('guided','compact')


def bind_mode(owner, widget):
    """Apply the profile preference to a new view and future live changes."""
    if hasattr(owner,'interface_mode_changed'):
        owner.interface_mode_changed.connect(widget.set_interface_mode)
    widget.set_interface_mode(getattr(owner,'interface_mode','guided'))


class Guidance(Disclosure):
    """Optional explanation: inline in guided mode, available on demand in compact."""
    def __init__(self, owner, widget, title=None):
        super().__init__(title or tr('Пояснение'))
        self.body.addWidget(widget)
        bind_mode(owner,self)

    def set_interface_mode(self, mode):
        guided=mode!='compact'
        self.header.setVisible(not guided)
        self.setExpanded(guided)


def save_interface_mode(path, mode):
    if mode not in MODES:raise ValueError(tr('Этот режим интерфейса не поддерживается.'))
    path=Path(path)
    current=json.loads(path.read_text(encoding='utf8')) if path.is_file() else {}
    if not isinstance(current,dict):raise ValueError(tr('Настройки должны содержать JSON-объект.'))
    current['interface_mode']=mode
    temporary=path.with_suffix('.interface.tmp')
    temporary.write_text(json.dumps(current,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    temporary.replace(path)


class InterfaceModeMixin:
    def choose_interface_mode(self,*_):
        mode=self.interface_mode_selector.currentData()
        if mode==self.interface_mode:return
        try:save_interface_mode(self.settings_path,mode)
        except (OSError,ValueError) as exc:
            blocked=self.interface_mode_selector.blockSignals(True)
            self.interface_mode_selector.setCurrentIndex(self.interface_mode_selector.findData(self.interface_mode))
            self.interface_mode_selector.blockSignals(blocked)
            self.interface_mode_notice.setText(tr('Не удалось сохранить режим: {0}',str(exc)))
            self.interface_mode_notice.show();return
        self.interface_mode_notice.hide()
        self.settings['interface_mode']=self.interface_mode=mode
        self.interface_mode_changed.emit(mode)
