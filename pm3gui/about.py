"""Release identity and local files for desktop users."""
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPushButton, QTextBrowser, QVBoxLayout

from .i18n import tr
from .paths import application_root
from .version import APP_NAME, APP_VERSION



from .ui import SoftButton

class AboutDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.setWindowTitle(tr('О программе'))
        self.resize(520, 280)
        layout = QVBoxLayout(self)
        title = QLabel(APP_NAME)
        title.setObjectName('title')
        layout.addWidget(title)
        layout.addWidget(QLabel(tr('Версия {0}', APP_VERSION)))
        details = QLabel(tr('Windows x64 · интерфейс для клиента RRG/Iceman Proxmark3'))
        details.setWordWrap(True)
        layout.addWidget(details)
        data = QLabel(tr('Библиотека: {0}', str(owner.data_dir)))
        data.setWordWrap(True)
        layout.addWidget(data)
        data_button = SoftButton(tr('Открыть папку библиотеки'))
        data_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(owner.data_dir))))
        layout.addWidget(data_button)
        notices = application_root() / 'THIRD_PARTY_NOTICES.md'
        license_button = SoftButton(tr('Лицензии компонентов'))
        license_button.setEnabled(notices.is_file())
        license_button.clicked.connect(lambda: self.show_licenses(notices))
        layout.addWidget(license_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def show_licenses(self, notices):
        dialog = QDialog(self)
        dialog.setWindowTitle(tr('Лицензии компонентов'))
        dialog.resize(680, 500)
        layout = QVBoxLayout(dialog)
        text = QTextBrowser()
        text.setOpenExternalLinks(True)
        text.setMarkdown(notices.read_text(encoding='utf-8'))
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()
