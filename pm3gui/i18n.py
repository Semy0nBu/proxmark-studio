"""Explicit application messages. Payload bytes, user text and CLI stay intact."""
import json
from pathlib import Path
from PySide6.QtCore import QCoreApplication,QLibraryInfo,QLocale,QTranslator

LANGUAGES={'ru':'Русский','en':'English'}
_language='ru'
_english=None
_qt_translator=None


def language():return _language


def set_language(value):
    global _language,_qt_translator
    _language=value if isinstance(value,str) and value in LANGUAGES else 'ru'
    QLocale.setDefault(QLocale('en_US' if _language=='en' else 'ru_RU'))
    app=QCoreApplication.instance()
    if app is not None:
        if _qt_translator is not None:app.removeTranslator(_qt_translator)
        _qt_translator=None
        if _language=='ru':
            translator=QTranslator(app)
            if translator.load('qtbase_ru',QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
                app.installTranslator(translator);_qt_translator=translator
    return _language


def english_catalog():
    global _english
    if _english is None:
        _english=json.loads(Path(__file__).with_name('locales').joinpath('en.json').read_text(encoding='utf-8'))
    return _english


def tr(source,*values):
    target=english_catalog().get(source,source) if _language=='en' else source
    return target.format(*values) if values else target


def _display_stored_message(value, sources):
    for source in sources:
        if value == source or value == english_catalog().get(source, source):
            return tr(source)
    return value


def display_key_source(value):
    """Localize reserved provenance text without changing stored records or paths."""
    aliases = {'manual': 'Ручной ввод', 'Imported sector keys': 'Импортированные ключи секторов',
               'Studio: dump fields': 'Лаборатория: поля дампа'}
    value = aliases.get(value, value)
    localized = _display_stored_message(value, (
        'Proxmark3: подтверждено клиентом',
        'Proxmark3: автозапись A/B и проверка на карте',
        'Ручной ввод', 'Импортированные ключи секторов', 'Лаборатория: поля дампа'))
    if localized != value:
        return localized
    for prefix in ('Импорт: ', 'Imported: '):
        if value.startswith(prefix):
            return tr('Импорт: {0}', value[len(prefix):])
    for prefix in ('Proxmark3: адресный сценарий / ', 'Proxmark3: targeted workflow / '):
        if value.startswith(prefix):
            return tr('Proxmark3: адресный сценарий / ') + value[len(prefix):]
    for prefix in ('Лаборатория: ', 'Studio: '):
        if value.startswith(prefix):
            return tr('Лаборатория: {0}', value[len(prefix):])
    return value


def display_key_label(value, source):
    if source == 'manual':
        return _display_stored_message(value, ('Добавлен вручную',))
    return value


def display_revision_note(value):
    """Only reserved revision notes are messages; arbitrary notes remain verbatim."""
    return _display_stored_message(value, (
        'Импорт', 'Изменение в редакторе', 'Неполное считывание с Proxmark3',
        'Повторное считывание с Proxmark3', 'Автоматическое чтение'))


def save_language(path,value):
    if not isinstance(value,str) or value not in LANGUAGES:raise ValueError(tr('Этот язык интерфейса не поддерживается.'))
    path=Path(path)
    current=json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
    if not isinstance(current,dict):raise ValueError(tr('Настройки должны содержать JSON-объект.'))
    current['language']=value
    temp=path.with_suffix('.language.tmp');temp.write_text(json.dumps(current,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');temp.replace(path)


class LanguageControllerMixin:
    def choose_language(self,*_):
        value=self.language_selector.currentData()
        try:save_language(self.settings_path,value)
        except (ValueError,OSError) as exc:
            blocked=self.language_selector.blockSignals(True)
            self.language_selector.setCurrentIndex(self.language_selector.findData(self.settings.get('language',self.ui_language)))
            self.language_selector.blockSignals(blocked)
            self.language_notice.setText(tr('Не удалось сохранить язык: {0}',str(exc)));self.language_notice.show();return
        self.settings['language']=value
        self.language_notice.setText(tr('После перезапуска: {0}',LANGUAGES[value]))
        self.language_notice.setVisible(value!=self.ui_language)
        self.statusBar().showMessage(tr('Язык сохранён. Перезапустите приложение, чтобы применить его.'),7000)
