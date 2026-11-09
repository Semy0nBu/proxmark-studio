"""Render persisted messages in the selected language without rewriting user data."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, QProcessEnvironment
from PySide6.QtWidgets import QApplication

from pm3gui.app import MainWindow
from pm3gui.auto_capture_ui import AutoCaptureMixin
from pm3gui.core import DumpData
from pm3gui.device import ClientRunner, MAX_OUTPUT_BYTES, _Run
from pm3gui.i18n import language, set_language
from pm3gui.source_picker import SourcePicker
from pm3gui.studio_controller import StudioControllerMixin
from pm3gui.write_dialog import WriteDialog


def classic_dump():
    data = bytearray(1024)
    data[:4] = bytes.fromhex('A1B2C3D4')
    for sector in range(16):
        start = (sector * 4 + 3) * 16
        data[start:start + 16] = bytes.fromhex('FFFFFFFFFFFFFF078069FFFFFFFFFFFF')
    return DumpData(bytes(data), 'bin', {}, 16)


class RuntimeLocalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.previous_language = language()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.windows = []
        self.dialogs = []
        self.enterContext(patch('pm3gui.app.available_ports', return_value=[]))
        self.enterContext(patch('pm3gui.app.discover_client', return_value=''))
        self.run = self.enterContext(patch('pm3gui.app.ClientRunner.run'))
        self.operation = self.enterContext(patch('pm3gui.app.ClientRunner.run_operation'))

    def tearDown(self):
        for dialog in reversed(self.dialogs):
            dialog.close()
            dialog.deleteLater()
        for window in reversed(self.windows):
            window.dirty = False
            window.read_job = None
            window.antenna_job = None
            window.close()
            window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        set_language(self.previous_language)
        self.temp.cleanup()

    def window(self):
        window = MainWindow(self.root/'profile', language='en')
        self.windows.append(window)
        return window

    def test_library_revision_pickers_translate_only_reserved_notes_at_render_time(self):
        window = self.window()
        dump = classic_dump()
        set_language('ru')
        card_id = window.library.create_card('Моя карта', dump, uid='A1B2C3D4', protocol='MIFARE Classic 1K')
        window.library.save_revision(card_id, dump, 'Моя заметка: Импорт')
        before = window.library.list_revisions(card_id)
        self.assertEqual([row['note'] for row in before], ['Моя заметка: Импорт', 'Импорт'])
        set_language('en')
        picker = SourcePicker(window, 'hf')
        self.dialogs.append(picker)
        write = WriteDialog(window)
        self.dialogs.append(write)
        write.sources.setCurrentIndex(next(index for index in range(write.sources.count())
                                          if write.sources.itemData(index) == ('card', card_id)))
        for combo in (picker.versions, write.source_versions):
            values = [combo.itemText(index) for index in range(combo.count())]
            self.assertEqual(len(values), 2)
            self.assertTrue(any(value.endswith(' · Import') for value in values), values)
            self.assertTrue(any(value.endswith(' · Моя заметка: Импорт') for value in values), values)
            self.assertFalse(any('MSK' in value or 'МСК' in value for value in values))
        self.assertEqual(picker.cards.itemText(1), 'Моя карта')
        self.assertEqual(window.library.list_revisions(card_id), before)
        self.assertEqual(window.library.get_dump(card_id).data, dump.data)
        self.run.assert_not_called()
        self.operation.assert_not_called()

    def test_classic_key_tooltips_localize_saved_sources_and_keep_custom_sources(self):
        window = self.window()
        set_language('ru')
        window.key_library.record_verified_keys('A1B2C3D4', [
            {'sector': 0, 'type': 'A', 'key_hex': 'FFFFFFFFFFFF'},
        ])
        window.key_library.record_verified_keys('A1B2C3D4', [
            {'sector': 0, 'type': 'B', 'key_hex': 'FFFFFFFFFFFF'},
        ], source='Мой источник')
        before = window.key_library.list_card_keys('A1B2C3D4')
        self.assertEqual(before[0]['source'], 'Proxmark3: подтверждено клиентом')
        set_language('en')
        dump = classic_dump()
        window.studio.use_dump(dump, {'uid': 'A1B2C3D4', 'name': 'Fixture'})
        source_a = window.studio.key_overview.item(0, 1).toolTip()
        source_b = window.studio.key_overview.item(0, 2).toolTip()
        self.assertIn('Proxmark3: verified by the client', source_a)
        self.assertNotIn('подтверждено', source_a)
        self.assertIn('Мой источник', source_b)
        self.assertNotIn('MSK', source_a + source_b)
        self.assertEqual(window.key_library.list_card_keys('A1B2C3D4'), before)
        self.assertEqual(window.studio.dump.data, dump.data)
        self.run.assert_not_called()
        self.operation.assert_not_called()

    def test_english_output_limit_marker_does_not_translate_native_output(self):
        set_language('en')
        runner = ClientRunner()
        output = []
        runner.output.connect(output.append)
        run = _Run('', [], str(self.root), QProcessEnvironment(), ('fixture', 0, 0))
        native = '[+] Произвольный вывод клиента\n'
        runner._consume_output(run, native.encode('utf-8'))
        run.output_bytes = MAX_OUTPUT_BYTES - 1
        runner._consume_output(run, b'ab')
        runner._consume_output(run, b'cd')
        text = ''.join(output)
        self.assertTrue(text.startswith(native + 'a'))
        self.assertEqual(text.count('[Output truncated: 4 MiB limit reached.]'), 1)
        self.assertNotIn('Вывод сокращён', text)
        self.assertEqual(run.output_bytes, MAX_OUTPUT_BYTES)
        self.run.assert_not_called()
        self.operation.assert_not_called()

    def test_intake_read_and_search_fallbacks_keep_exit_code_in_english(self):
        set_language('en')
        for stage, method, expected in (
                ('family_read', '_auto_read_failed', 'Reading ended with code 7.'),
                ('search', '_auto_fail', 'Search ended with code 7.')):
            with self.subTest(stage=stage):
                target = Mock()
                owner = SimpleNamespace(auto_intake={'stopping': False},
                    read_job={'stage': stage, 'request': SimpleNamespace(allow_failure=False)})
                setattr(owner, method, target)
                with patch('pm3gui.auto_capture_ui.transport_failed', return_value=False):
                    AutoCaptureMixin._advance_auto_capture(owner, 'native text', 7)
                target.assert_called_once_with(expected)

    def test_studio_failed_step_fallback_is_english_and_native_log_is_untouched(self):
        set_language('en')
        output = Mock()
        failed = Mock()
        native = '[!] Произвольный текст клиента'
        owner = SimpleNamespace(read_job={'stage': 'running', 'events': [{}], 'rf': False,
                    'request': SimpleNamespace(allow_failure=False, allow_capture_stop=False)},
                    studio=SimpleNamespace(output=output, sections=SimpleNamespace(currentRow=lambda: 0)),
                    _studio_failed=failed)
        StudioControllerMixin._advance_studio(owner, native, 7)
        failed.assert_called_once_with('Step ended with code 7. See the log.')
        output.appendPlainText.assert_called_once_with(native)
        self.assertEqual(owner.studio.last_output, native)


if __name__ == '__main__':
    unittest.main()
