"""Named actions editor. Opening/selecting/saving never starts a client."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QMessageBox, QPlainTextEdit, QSpinBox, QVBoxLayout)
from .quick_actions import QuickAction, actions_revision, load_actions, save_actions
from .studio_data import say
from .theme import mono_font
from .ui import SoftButton


class QuickActionsDialog(QDialog):
    def __init__(self, studio, initial=None):
        super().__init__(studio)
        self.studio = studio
        self.path = studio.owner.data_dir / 'quick-actions.json'
        self.setWindowTitle(say('Избранные действия', 'Quick actions'))
        self.resize(760, 540)
        layout = QVBoxLayout(self)
        hint = QLabel(say('Одна команда в строке. Перед запуском показывается весь сценарий; ошибка или Stop прекращают его.',
                         'One command per line. Review every step before running; an error or Stop ends the action.'))
        hint.setWordWrap(True); layout.addWidget(hint)
        body = QHBoxLayout(); layout.addLayout(body, 1)
        self.choices = QListWidget(); self.choices.setMaximumWidth(250); body.addWidget(self.choices)
        form = QFormLayout(); body.addLayout(form, 1)
        self.name = QLineEdit(); self.name.setMaxLength(80); form.addRow(say('Название', 'Name'), self.name)
        self.commands = QPlainTextEdit(); self.commands.setFont(mono_font())
        self.commands.setPlaceholderText('hw version\nhw status'); form.addRow(say('Команды', 'Commands'), self.commands)
        self.timeout = QSpinBox(); self.timeout.setRange(5, 1800); self.timeout.setValue(120)
        self.timeout.setSuffix(say(' с на команду', ' s per command')); form.addRow(say('Лимит', 'Timeout'), self.timeout)
        self.offline = QCheckBox(say('Без устройства', 'Without a device')); form.addRow('', self.offline)
        self.notice = QLabel(); self.notice.setTextFormat(Qt.TextFormat.PlainText); self.notice.setWordWrap(True); layout.addWidget(self.notice)
        buttons = QHBoxLayout(); layout.addLayout(buttons)
        for ru, en, method in [('Новое','New',self.new),('Сохранить','Save',self.save),
                                ('Удалить','Delete',self.remove),('Просмотреть и запустить…','Review and run…',self.run),
                                ('Закрыть','Close',self.close)]:
            button = SoftButton(say(ru,en)); button.clicked.connect(lambda checked=False, fn=method:self.guard(fn)); buttons.addWidget(button)
            if en == 'Review and run…':self.run_button = button
        self.choices.currentRowChanged.connect(self.select)
        self.actions = []
        self.load_error = False
        try:
            self.revision = actions_revision(self.path)
            self.actions = load_actions(self.path)
            if actions_revision(self.path) != self.revision:raise ValueError('Quick actions changed while being loaded.')
        except (OSError, ValueError, TypeError) as exc:
            self.load_error = True; self.notice.setText(str(exc))
        self.refresh()
        if initial is not None:self.populate(initial)

    def guard(self, method):
        try:method()
        except (OSError, ValueError, TypeError) as exc:self.notice.setText(str(exc))

    def refresh(self, name=None):
        self.choices.blockSignals(True); self.choices.clear()
        for action in self.actions:self.choices.addItem(action.name)
        self.choices.blockSignals(False)
        if name is not None:
            index = next(i for i, a in enumerate(self.actions) if a.name.casefold() == name.casefold())
            self.choices.setCurrentRow(index)

    def populate(self, action):
        self.name.setText(action.name); self.commands.setPlainText('\n'.join(action.lines))
        self.timeout.setValue(action.timeout_seconds); self.offline.setChecked(action.offline)

    def select(self, index):
        if 0 <= index < len(self.actions):self.populate(self.actions[index])

    def new(self):
        self.choices.setCurrentRow(-1); self.populate(QuickAction('', (), 120, False)); self.notice.clear()

    def edited(self):
        return QuickAction(self.name.text(), tuple(self.commands.toPlainText().splitlines()),
                           self.timeout.value(), self.offline.isChecked()).validated()

    def save(self):
        if self.load_error:raise ValueError(say('Исправьте повреждённый файл избранного перед сохранением.',
                                                'Repair the quick actions file before saving.'))
        action = self.edited()
        index = next((i for i,a in enumerate(self.actions) if a.name.casefold() == action.name.casefold()), None)
        if index is not None and self.actions[index] != action:
            if QMessageBox.question(self, say('Заменить действие?', 'Replace action?'), action.name,
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:return
        updated = list(self.actions)
        if index is None:updated.append(action)
        else:updated[index] = action
        self.revision = save_actions(self.path, updated, expected_revision=self.revision)
        self.actions = updated; self.refresh(action.name)
        self.notice.setText(say('Сохранено в этом профиле.', 'Saved in this profile.'))

    def remove(self):
        index = self.choices.currentRow()
        if index < 0:return
        if QMessageBox.question(self, say('Удалить действие?', 'Delete action?'), self.actions[index].name,
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:return
        updated = self.actions[:index] + self.actions[index+1:]
        self.revision = save_actions(self.path, updated, expected_revision=self.revision)
        self.actions = updated; self.refresh(); self.new()

    def run(self):
        action = self.edited()
        if self.studio.run_quick_action(action):self.accept()
