"""Localization, persisted choices, and language-independent device commands."""
import ast
import json
import os
from pathlib import Path
import re
from string import Formatter
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QCoreApplication, QEvent, QLocale
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QApplication, QAbstractButton, QComboBox, QLabel,
                              QLineEdit, QPlainTextEdit, QTabWidget, QTableWidget, QTextBrowser, QWidget)
from pm3gui.app import MainWindow
from pm3gui.about import AboutDialog
from pm3gui.core import DumpData
from pm3gui.hf_forms import TITLES, LABELS, action_title, parameters, form_arguments
from pm3gui.hf_tools_ui import HFToolsDialog
from pm3gui.firmware import _PROFILES
from pm3gui.firmware_ui import FirmwareDialog
from pm3gui.i18n import (display_key_label, display_key_source, display_revision_note,
                         english_catalog, language, save_language, set_language, tr)
from pm3gui.keys import format_key_timestamp
from pm3gui.lab_dialogs import EmulationDialog, LFWriteDialog, ResumeDialog
from pm3gui.memory_map import DATA_ACCESS, MemoryMapDialog
from pm3gui.operations import Detection, Operation, parse_detection
from pm3gui.presentation import ENCODINGS
from pm3gui.write_dialog import WriteDialog
from pm3gui.write_scenario import save_scenario
from pm3gui.writing import WriteOptions

ROOT = Path(__file__).resolve().parents[1]
CYRILLIC = re.compile('[А-Яа-яЁё]')


def image():
    data = bytearray(1024)
    data[:4] = bytes.fromhex('A1B2C3D4')
    for sector in range(16):
        data[(sector*4+3)*16:(sector*4+4)*16] = bytes.fromhex('FFFFFFFFFFFFFF078069FFFFFFFFFFFF')
    return DumpData(bytes(data), 'bin', {}, 16)


class LocalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        set_language('ru')
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.windows = []
        self.dialogs = []
        self.enterContext(patch('pm3gui.app.available_ports', return_value=[]))
        self.enterContext(patch('pm3gui.app.discover_client', return_value=''))
        self.run = self.enterContext(patch('pm3gui.app.ClientRunner.run'))
        self.operation = self.enterContext(patch('pm3gui.app.ClientRunner.run_operation'))

    def tearDown(self):
        for d in reversed(self.dialogs):
            d.close()
        for w in reversed(self.windows):
            w.dirty = False
            w.read_job = None
            w.antenna_job = None
            w.close()
            w.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        set_language('ru')
        self.temp.cleanup()

    def window(self, **kwargs):
        w = MainWindow(self.root/'profile', **kwargs)
        self.windows.append(w)
        return w

    def assert_english_widgets(self, root):
        texts = [root.windowTitle()]
        for widget in root.findChildren(QWidget):
            if isinstance(widget, QComboBox) and widget is getattr(root, 'language_selector', None):
                continue  # Language names are always written in their native language.
            texts.extend((widget.toolTip(), widget.statusTip(), widget.whatsThis()))
            if isinstance(widget, (QAbstractButton, QLabel, QLineEdit)):
                texts.append(widget.text())
            if isinstance(widget, (QLineEdit, QPlainTextEdit)):
                texts.append(widget.placeholderText())
            if isinstance(widget, QPlainTextEdit):
                texts.append(widget.toPlainText())
            if isinstance(widget, QComboBox):
                texts.extend(widget.itemText(i) for i in range(widget.count()))
            if isinstance(widget, QTabWidget):
                texts.extend(widget.tabText(i) for i in range(widget.count()))
            if isinstance(widget, QTableWidget):
                texts.extend(widget.horizontalHeaderItem(i).text() for i in range(widget.columnCount())
                             if widget.horizontalHeaderItem(i))
                texts.extend(widget.item(r,c).text() for r in range(widget.rowCount())
                             for c in range(widget.columnCount()) if widget.item(r,c))
        texts.extend(a.text() for a in root.findChildren(QAction))
        self.assertEqual([t for t in texts if CYRILLIC.search(t)], [])

    def test_catalog_covers_explicit_messages_and_static_titles(self):
        catalog = english_catalog()
        required = set(TITLES.values()) | set(LABELS.values()) | set(DATA_ACCESS.values())
        required.update(label for label, *_ in _PROFILES.values())
        required.update(title for title, _ in ENCODINGS)
        for path in [*ROOT.joinpath('pm3gui').glob('*.py'), ROOT/'run_gui.py']:
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='tr':
                    if node.args and isinstance(node.args[0],ast.Constant):
                        required.add(node.args[0].value)
        self.assertEqual({s for s in required if CYRILLIC.search(s)}-set(catalog), set())
        for source, target in catalog.items():
            with self.subTest(source=source):
                self.assertTrue(target)
                self.assertIsNone(CYRILLIC.search(target))
                fields = lambda s: {f for _, f, _, _ in Formatter().parse(s) if f is not None}
                self.assertEqual(fields(source), fields(target))

    def test_english_main_window_has_no_untranslated_application_text(self):
        w = self.window(language='en')
        self.assertEqual(w.page_names, ['Workspace','Cards','MIFARE keys','Antennas','Encodings','Laboratory'])
        self.assertEqual(w.read_button.text(), 'Read and save')
        self.assertEqual(w.workspace_bands.tabText(0), 'HF · 13.56 MHz')
        self.assertEqual(w.converter_encoding.itemText(1), 'Windows-1251 · Cyrillic')
        self.assertEqual(QLocale().name(), 'en_US')
        self.assert_english_widgets(w)
        self.run.assert_not_called(); self.operation.assert_not_called()

    def test_choice_is_saved_without_rebuilding_or_interrupting_active_work(self):
        w = self.window()
        original = {'language':'ru','client_path':'saved.exe','usb_port':'COM7','custom':{'keep':42}}
        w.settings_path.write_text(json.dumps(original), encoding='utf-8')
        w.client_path.setText('unsaved.exe')
        w.converter_hex.setPlainText('D0 9F D1 80')
        w.read_job = job = {'stage':'fixture'}
        editor = w.hex_edit
        with patch.object(w.runner, 'disconnect') as disconnect:
            w.language_selector.setCurrentIndex(w.language_selector.findData('en'))
            disconnect.assert_not_called()
        self.assertEqual(json.loads(w.settings_path.read_text(encoding='utf-8')), dict(original,language='en'))
        self.assertIs(w.hex_edit, editor); self.assertIs(w.read_job, job)
        self.assertEqual(w.converter_hex.toPlainText(), 'D0 9F D1 80')
        self.assertEqual(w.ui_language, 'ru'); self.assertEqual(language(), 'ru')
        self.assertEqual(w.page_names[0], 'Рабочая область')
        self.assertIn('English', w.language_notice.text())
        w.read_job = None
        w._save_presentation()
        other = self.window()
        self.assertEqual(other.ui_language, 'en')
        self.assertEqual(other.page_names[0], 'Workspace')
        self.assertEqual(other.client_path.text(), 'saved.exe')
        self.run.assert_not_called(); self.operation.assert_not_called()

    def test_failed_language_save_restores_selector_and_preserves_bad_file(self):
        w = self.window(language='en')
        w.settings['language'] = 'en'
        w.settings_path.write_text('{broken', encoding='utf-8')
        w.language_selector.setCurrentIndex(w.language_selector.findData('ru'))
        self.assertEqual(w.language_selector.currentData(), 'en')
        self.assertEqual(w.settings_path.read_text(encoding='utf-8'), '{broken')
        self.assertIn('Could not save', w.language_notice.text())
        self.assertEqual(w.ui_language, 'en')

    def test_invalid_languages_fall_back_and_nonobject_settings_are_not_replaced(self):
        for value in (None, [], {}, 'fr', 7):
            self.assertEqual(set_language(value), 'ru')
        w = self.window()
        w.settings_path.write_text('{"language": []}', encoding='utf-8')
        self.assertEqual(w._load_settings()['language'], 'ru')
        w.settings_path.write_text('[]', encoding='utf-8')
        with self.assertRaises(ValueError):
            save_language(w.settings_path, 'en')
        self.assertEqual(w.settings_path.read_text(encoding='utf-8'), '[]')

    def test_english_dialogs_forms_and_map_use_runtime_language(self):
        # All these modules were imported before English was selected.
        w = self.window(language='en')
        cid = w.library.create_card('Test Classic 1K', image(), uid='A1B2C3D4', protocol='MIFARE Classic 1K')
        w.refresh_library(); w.load_card(cid)
        for factory in (lambda: AboutDialog(w), lambda: FirmwareDialog(w,'usb'), lambda: HFToolsDialog(w),
                        lambda: HFToolsDialog(w,'lf'), lambda: WriteDialog(w),
                        lambda: EmulationDialog(w), lambda: LFWriteDialog(w),
                        lambda: ResumeDialog(w), lambda: MemoryMapDialog(w,w.loaded_dump)):
            dialog = factory(); self.dialogs.append(dialog)
            self.assert_english_widgets(dialog)
        self.assertEqual(Detection().protocol, 'Not identified')
        self.assertEqual(action_title('hf mf rdbl'), 'Read block')
        self.assertIn('Block', parameters('hf mf wrbl')[0].label)
        with self.assertRaisesRegex(ValueError, 'Complete the .Block'):
            form_arguments('hf mf wrbl', {})
        self.run.assert_not_called(); self.operation.assert_not_called()

    def test_user_text_key_values_and_payload_are_never_translated(self):
        w = self.window(language='en')
        data = 'Пример данных'.encode('cp1251')
        cid = w.library.create_card('Рабочая область', DumpData(data,'bin',{},16),
                                    uid='A1B2C3D4',protocol='Своя карта',notes='Сохранить')
        w.key_library.add_keys(['A0A1A2A3A4A5'],label='Мой ключ',source='Мой источник')
        w.refresh_library(); w.refresh_keys(); w.load_card(cid)
        self.assertEqual(w.card_name.text(), 'Рабочая область')
        self.assertEqual(w.notes.text(), 'Сохранить')
        self.assertEqual(w.loaded_dump.data, data)
        self.assertEqual(w.key_table.item(0,0).text(), 'A0A1A2A3A4A5')
        self.assertEqual(w.key_table.item(0,2).text(), 'Мой ключ')
        self.assertEqual(w.key_table.item(0,3).text(), 'Мой источник')
        self.assertEqual(len(w.library.list_revisions(cid)), 1)

    def test_key_provenance_default_is_selected_when_recording(self):
        w = self.window(language='en')
        w.key_library.record_verified_keys('A1B2C3D4', [{'sector':0,'type':'A','key_hex':'FFFFFFFFFFFF'}])
        self.assertEqual(w.key_library.list_card_keys('A1B2C3D4')[0]['source'], 'Proxmark3: verified by the client')

    def test_about_displays_notices_without_an_external_markdown_viewer(self):
        w = self.window(language='en')
        dialog = AboutDialog(w);self.dialogs.append(dialog)
        notices = self.root/'THIRD_PARTY_NOTICES.md'
        notices.write_text('# Component licenses\n\nGPL and LGPL license texts are included.\n', encoding='utf-8')
        def inspect(license_dialog):
            self.assertEqual(license_dialog.windowTitle(), 'Component licenses')
            self.assertIn('GPL and LGPL', license_dialog.findChild(QTextBrowser).toPlainText())
            self.assert_english_widgets(license_dialog)
            return 0
        with patch('pm3gui.about.QDialog.exec', inspect), patch('pm3gui.about.QDesktopServices.openUrl') as external:
            dialog.show_licenses(notices)
            external.assert_not_called()

    def test_reserved_saved_messages_follow_the_language_and_preserve_custom_text(self):
        pairs = [
            ('Proxmark3: подтверждено клиентом', 'Proxmark3: verified by the client'),
            ('Proxmark3: автозапись A/B и проверка на карте', 'Proxmark3: A/B keys written and verified on the card'),
            ('Импорт: Карта {0}.nfc', 'Imported: Карта {0}.nfc'),
            ('Proxmark3: адресный сценарий / nested', 'Proxmark3: targeted workflow / nested'),
            ('Лаборатория: Card {0}.bin', 'Studio: Card {0}.bin'),
        ]
        for russian, english in pairs:
            for saved in (russian, english):
                set_language('en');self.assertEqual(display_key_source(saved), english)
                set_language('ru');self.assertEqual(display_key_source(saved), russian)
        set_language('en')
        self.assertEqual(display_key_source('manual'), 'Manual entry')
        self.assertEqual(display_key_source('Imported sector keys'), 'Imported sector keys')
        self.assertEqual(display_key_source('Studio: dump fields'), 'Studio: dump fields')
        self.assertEqual(display_key_source('Мой источник'), 'Мой источник')
        self.assertEqual(display_key_label('Добавлен вручную', 'manual'), 'Added manually')
        self.assertEqual(display_key_label('Мой ключ', 'manual'), 'Мой ключ')
        self.assertEqual(display_key_label('Добавлен вручную', 'Мой источник'), 'Добавлен вручную')
        self.assertEqual(display_revision_note('Изменение в редакторе'), 'Edited in the card editor')
        self.assertEqual(display_revision_note('Сохранить'), 'Сохранить')
        self.assertEqual(display_revision_note('Моя заметка {0}'), 'Моя заметка {0}')

    def test_legacy_key_sources_dates_and_search_are_english_without_rewriting_library(self):
        legacy = self.window(language='ru')
        key_a, key_b, key_c = '112233445566', 'A1B2C3D4E5F6', '778899AABBCC'
        with patch('pm3gui.keys._now', return_value='2026-11-08T22:15:07+00:00'):
            legacy.key_library.record_verified_keys('A1B2C3D4', [
                {'sector':0, 'type':'A', 'key_hex':key_a}])
            imported = self.root/'example.dic';imported.write_text(key_a+'\n'+key_b+'\n')
            legacy.key_library.import_file(imported)
            legacy.key_library.add_keys([key_c], label='Добавлен вручную', source='manual')
        card_id = legacy.library.create_card('Test Classic 1K', image(), uid='A1B2C3D4')
        legacy.library.save_revision(card_id, image(), 'Изменение в редакторе')
        before = '\n'.join(legacy.library.connection.iterdump())
        w = self.window(language='en')
        sources = {w.key_table.item(row,0).text():w.key_table.item(row,3).text()
                   for row in range(w.key_table.rowCount())}
        self.assertEqual(sources, {key_a:'Proxmark3: verified by the client',
                                   key_b:'Imported: example.dic', key_c:'Manual entry'})
        labels = {w.key_table.item(row,0).text():w.key_table.item(row,2).text()
                  for row in range(w.key_table.rowCount())}
        self.assertEqual(labels[key_c], 'Added manually')
        for row in range(w.key_table.rowCount()):
            self.assertEqual(w.key_table.item(row,4).text(), '2026-11-09 01:15:07')
        for query, expected in [('Imported', {key_a,key_b}), ('Импорт', {key_a,key_b}),
                                ('Manual entry', {key_c}), ('Added manually', {key_c}),
                                ('verified by the client', {key_a})]:
            w.key_search.setText(query)
            self.assertEqual({w.key_table.item(row,0).text() for row in range(w.key_table.rowCount())}, expected)
        w.key_search.clear();w.load_card(card_id)
        self.assertTrue(any('Edited in the card editor' in w.revisions.itemText(i) for i in range(w.revisions.count())))
        self.assertTrue(any('Import' in w.revisions.itemText(i) for i in range(w.revisions.count())))
        def check_card_keys(dialog):
            self.assert_english_widgets(dialog)
            table = dialog.findChild(QTableWidget)
            self.assertEqual(table.item(0,6).text(), 'Proxmark3: verified by the client')
            self.assertEqual(table.item(0,4).text(), '2026-11-09 01:15:07')
            self.assertEqual(table.item(0,5).text(), '2026-11-09 01:15:07')
            return 0
        with patch('pm3gui.app.QDialog.exec', check_card_keys):w.show_card_keys()
        self.assert_english_widgets(w)
        self.assertNotRegex(' '.join(w.revisions.itemText(i) for i in range(w.revisions.count())), r'\b(?:MSK|МСК)\b')
        self.assertEqual('\n'.join(w.library.connection.iterdump()), before)
        self.run.assert_not_called();self.operation.assert_not_called()

    def test_profile_change_cannot_be_mistaken_for_a_verified_english_connection(self):
        w = self.window(language='en')
        w.current_command = 'hw version'
        w.current_step_profile = w._device_profile()
        w.current_step_transport = 'usb'
        w.ports.setEditText('COM99')
        w.operation_output = '[+] Communicating with PM3 over USB-CDC\nOS......... Iceman/test-build\n'
        w._finished(0, 'Complete')
        self.assertIn('changed', w.connection_status.text())
        self.assertNotIn('Connection verified', w.connection_status.text())
        self.assertNotIn('Ready to read', w.device_info.text())

    def test_commands_detection_and_scenario_bytes_are_language_independent(self):
        commands = []
        for locale in ('ru','en'):
            set_language(locale)
            args = form_arguments('hf mf wrbl', {'blk':'1','data':'AA'*16,'key':'FF'*6})
            commands.append((args, Operation('hf_tool',hf_command='hf mf wrbl',hf_args=args).render()))
            detected = parse_detection('UID: A1 B2 C3 D4\nATQA: 00 04\nSAK: 08 [2]')
            self.assertEqual((detected.family,detected.uid,detected.classic_size), ('mfc','A1B2C3D4','1k'))
            path = self.root/(locale+'.json')
            save_scenario(path,image(),WriteOptions('mfc'))
        self.assertEqual(commands[0],commands[1])
        ru = json.loads((self.root/'ru.json').read_text(encoding='utf-8'))
        en = json.loads((self.root/'en.json').read_text(encoding='utf-8'))
        self.assertEqual(en['name'], 'HF auto-write')
        del ru['name']; del en['name']; self.assertEqual(ru,en)

    def test_english_dates_and_error_status_keep_their_meaning(self):
        w = self.window(language='en')
        self.assertEqual(format_key_timestamp('2026-11-09T12:34:56Z'), '2026-11-09 15:34:56')
        w.ui_has_operation = True
        for message in ('Reading incomplete: test', 'Could not read card', 'Ошибка чтения'):
            w.device_info.setText(message)
            self.assertEqual(w.device_info.property('tone'), 'error')
        w.device_info.setText('All blocks read')
        self.assertEqual(w.device_info.property('tone'), '')


if __name__ == '__main__':
    unittest.main()
