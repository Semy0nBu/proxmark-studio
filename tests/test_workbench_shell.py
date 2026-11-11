"""Workbench controls remain attached to the existing, exclusive controllers.

Every profile is synthetic and every client-launch path is blocked. These are
native Qt integration checks, not a claim about a serial/Bluetooth transport.
"""
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from pm3gui.app import MainWindow
from pm3gui.core import DumpData, parse_hex
from pm3gui.i18n import set_language
from pm3gui.interface_mode import Guidance


def classic():
    data = bytearray(1024)
    for sector in range(16):
        data[(sector*4+3)*16:(sector*4+4)*16] = bytes.fromhex('FFFFFFFFFFFFFF078069FFFFFFFFFFFF')
    return DumpData(bytes(data), 'bin', {}, 16)


class WorkbenchShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.windows = []
        self.addCleanup(self.cleanup)
        self.enterContext(patch('pm3gui.app.available_ports', return_value=[]))
        self.enterContext(patch('pm3gui.app.discover_client', return_value=''))
        self.run = self.enterContext(patch('pm3gui.app.ClientRunner.run', side_effect=AssertionError('Unexpected device command')))
        self.operation = self.enterContext(patch('pm3gui.app.ClientRunner.run_operation', side_effect=AssertionError('Unexpected device operation')))
        self.stop = self.enterContext(patch.object(MainWindow, 'cancel_operation'))
        self.connect = self.enterContext(patch.object(MainWindow, 'toggle_connection'))
        self.window = self.create()

    def create(self):
        window = MainWindow(self.root/'profile', language='en')
        self.windows.append(window)
        return window

    def cleanup(self):
        for window in reversed(self.windows):
            window.runner._session.connected = False
            window.runner._active = None
            window.read_job = None
            window.antenna_job = None
            window.dirty = False
            window.preferences_menu.hide()
            window.close()
            window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        set_language('ru')
        self.temp.cleanup()

    def load(self, dump, protocol='MIFARE Classic 1K'):
        cid = self.window.library.create_card('Synthetic', dump, uid='A1B2C3D4', protocol=protocol)
        self.window.load_card(cid)
        self.window.refresh_library()
        self.window.workbench_inspector.refresh()
        return cid

    def test_top_navigation_preserves_all_page_routes_and_one_selection(self):
        w = self.window
        self.assertEqual(len(w.nav_buttons), 6)
        w.show()
        for index in (5, 2, 4, 1, 3, 0):
            w.nav_buttons[index].click()
            self.assertEqual(w.stack.currentIndex(), index)
            self.assertEqual([i for i,b in enumerate(w.nav_buttons) if b.isChecked()], [index])
            self.assertTrue(w.connection_strip.isVisible())
            self.assertTrue(w.preferences_button.isVisible())
        self.run.assert_not_called();self.operation.assert_not_called()

    def test_connect_and_stop_dispatch_once_from_every_page(self):
        w = self.window;w.show()
        # The controls have one controller connection even after reparenting.
        for index in range(6):
            w.navigate(index);w._busy_changed(False)
            self.assertTrue(w.session_button.isVisible())
            w.session_button.click()
            self.assertEqual(self.connect.call_count, index+1)
            w._busy_changed(True)
            self.assertTrue(w.stop_button.isEnabled())
            self.assertTrue(w.stop_button.isVisible())
            self.assertFalse(w.session_button.isEnabled())
            w.stop_button.click()
            self.assertEqual(self.stop.call_count, index+1)
        w._busy_changed(False)
        self.assertFalse(w.stop_button.isEnabled())

    def test_global_connection_only_uses_actual_session_and_link_loss_clears_it(self):
        w = self.window
        w.ports.setEditText('COM9')
        w._sync_connection_view()
        self.assertFalse(w.global_connection.property('connected'))
        self.assertIn('Not connected', w.global_connection.text())
        w.runner._session.transport = 'usb';w.runner._session.connected = True
        w._session_changed(True, 'Confirmed reply')
        for index in range(6):
            w.navigate(index)
            self.assertTrue(w.global_connection.property('connected'))
            self.assertIn('COM9', w.global_connection.text())
        w.runner._session.connected = False
        w._session_changed(False, 'Connection lost')
        self.assertFalse(w.global_connection.property('connected'))
        self.assertIn('Connection lost', w.global_connection.toolTip())
        self.assertEqual(w.session_button.text(), 'Connect')

    def test_connection_shortcut_opens_existing_settings_without_device_io(self):
        w = self.window
        w.navigate(5)
        w.connection_shortcut.click()
        self.assertEqual(w.stack.currentIndex(), 0)
        self.assertTrue(w.connection_details.header.isChecked())
        self.run.assert_not_called();self.operation.assert_not_called()

    def test_settings_popup_contains_language_and_mode_and_backup_routes_once(self):
        w = self.window
        action = w.preferences_menu.actions()[0]
        self.assertIs(w.interface_mode_selector.window(), w.preferences_menu.window())
        self.assertIsNotNone(action.defaultWidget())
        self.assertEqual([w.language_selector.itemData(i) for i in range(w.language_selector.count())], ['ru','en'])
        self.assertEqual([w.interface_mode_selector.itemData(i) for i in range(2)], ['guided','compact'])
        with patch.object(w, 'show_library_backup') as backup:
            w.backup_button.click()
            backup.assert_called_once()
        self.run.assert_not_called();self.operation.assert_not_called()

    def open_preferences(self):
        w = self.window;w.show();QTest.qWait(10)
        w.preferences_menu.popup(w.preferences_button.mapToGlobal(QPoint(0,w.preferences_button.height())))
        QTest.qWait(10)
        self.assertTrue(w.preferences_menu.isVisible())
        self.assertIs(self.app.focusWidget(), w.interface_mode_selector)

    def test_settings_tab_chain_wraps_both_ways_and_escape_restores_button_focus(self):
        w = self.window;self.open_preferences()
        self.assertEqual(w.interface_mode_selector.accessibleName(), 'Interface mode')
        self.assertEqual(w.language_selector.accessibleName(), 'Language')
        for control in (w.language_selector,w.backup_button,w.about_button,w.interface_mode_selector):
            QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Tab)
            self.assertIs(self.app.focusWidget(), control)
        for control in (w.about_button,w.backup_button,w.language_selector,w.interface_mode_selector):
            QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Tab, Qt.KeyboardModifier.ShiftModifier)
            self.assertIs(self.app.focusWidget(), control)
        w.language_selector.setEnabled(False)
        QTest.keyClick(w.interface_mode_selector, Qt.Key.Key_Tab)
        self.assertIs(self.app.focusWidget(), w.backup_button)
        QTest.keyClick(w.backup_button, Qt.Key.Key_Escape)
        self.assertFalse(w.preferences_menu.isVisible())
        self.assertIs(self.app.focusWidget(), w.preferences_button)

    def test_settings_can_open_with_keyboard_and_run_backup_once(self):
        w = self.window;w.show();w.preferences_button.setFocus()
        errors = [];finished = []
        def exercise():
            try:
                self.assertTrue(w.preferences_menu.isVisible())
                self.assertIs(self.app.focusWidget(), w.interface_mode_selector)
                QTest.keyClick(w.interface_mode_selector, Qt.Key.Key_Tab)
                QTest.keyClick(w.language_selector, Qt.Key.Key_Tab)
                self.assertIs(self.app.focusWidget(), w.backup_button)
                QTest.keyClick(w.backup_button, Qt.Key.Key_Space)
            except BaseException as exc:errors.append(exc)
            finally:
                w.preferences_menu.hide();finished.append(True)
        with patch.object(w, 'show_library_backup') as backup:
            timer = QTimer(w);timer.setSingleShot(True);timer.timeout.connect(exercise);timer.start(40)
            QTest.keyClick(w.preferences_button, Qt.Key.Key_Space)
            deadline = time.monotonic()+2
            while not finished and time.monotonic() < deadline:QTest.qWait(10)
            timer.stop()
            self.assertTrue(finished, 'The keyboard popup exercise did not run')
            if errors:raise errors[0]
            backup.assert_called_once()
        self.assertFalse(w.preferences_menu.isVisible())
        self.run.assert_not_called();self.operation.assert_not_called()

    def test_settings_about_is_keyboard_activatable_and_combo_popup_keeps_escape(self):
        w = self.window;self.open_preferences()
        w.interface_mode_selector.showPopup();QTest.qWait(10)
        popup = self.app.activePopupWidget()
        self.assertIsNotNone(popup)
        self.assertIsNot(popup, w.preferences_menu)
        QTest.keyClick(popup, Qt.Key.Key_Escape);QTest.qWait(10)
        self.assertTrue(w.preferences_menu.isVisible())
        w.about_button.setFocus()
        with patch('pm3gui.about.AboutDialog.exec', return_value=0) as about:
            QTest.keyClick(w.about_button, Qt.Key.Key_Space)
            about.assert_called_once()
        self.assertFalse(w.preferences_menu.isVisible())

    def test_preferences_preserve_uncommitted_connection_edits_and_unknown_settings(self):
        w = self.window
        original = {'language':'en','client_path':'saved-client.exe','usb_port':'COM3',
                    'custom':{'preserve':True},'ui':{'extension':'retained'}}
        w.settings_path.write_text(json.dumps(original), encoding='utf8')
        w.client_path.setText('uncommitted-client.exe')
        w.ports.setEditText('COM99')
        w.interface_mode_selector.setCurrentIndex(w.interface_mode_selector.findData('compact'))
        w.language_selector.setCurrentIndex(w.language_selector.findData('ru'))
        w._save_presentation()
        saved = json.loads(w.settings_path.read_text(encoding='utf8'))
        self.assertEqual(saved['custom'], original['custom'])
        self.assertEqual(saved['client_path'], 'saved-client.exe')
        self.assertEqual(saved['usb_port'], 'COM3')
        self.assertEqual(saved['language'], 'ru')
        self.assertEqual(saved['interface_mode'], 'compact')
        self.assertEqual(saved['ui']['layout'], 'workbench-v1')
        self.assertTrue(w.language_notice.isHidden() is False)
        self.assertEqual(w.ui_language, 'en')
        for guidance in w.findChildren(Guidance):
            self.assertFalse(guidance.header.isHidden())
            self.assertFalse(guidance.header.isChecked())

    def test_old_horizontal_layout_migrates_without_leaking_into_new_splitters(self):
        w = self.window
        w.card_editors.setOrientation(Qt.Orientation.Horizontal)
        old_state = bytes(w.card_editors.saveState()).hex()
        w.settings['ui'] = {'card_editors':old_state,'library':bytes(w.library_splitter.saveState()).hex(),
                            'text_preview':True,'extension':'keep'}
        w._restore_presentation()
        self.assertEqual(w.card_editors.orientation(), Qt.Orientation.Vertical)
        self.assertFalse(w.workbench_text_toggle.isChecked())
        w._save_presentation()
        self.assertEqual(json.loads(w.settings_path.read_text(encoding='utf8'))['ui']['extension'], 'keep')
        self.assertEqual(w.card_tabs.currentIndex(), 2)

    def test_inspector_and_offsets_follow_selected_old_revision_without_saving(self):
        w = self.window;cid = self.load(classic())
        old = w.loaded_revision
        changed = bytearray(w.loaded_dump.data);changed[128] = 0x42
        w.library.save_revision(cid, DumpData(bytes(changed),'bin',{},16), 'Synthetic second revision')
        w.load_card(cid, old);w.workbench_inspector.refresh()
        old_bytes = w.loaded_dump.data
        before = w.library.list_revisions(cid)
        self.assertEqual(w.card_tabs.currentIndex(), 2)
        for sector in (15, 0, 2):w.workbench_inspector.select_sector(sector)
        QTest.qWait(30)
        self.assertEqual(w.loaded_revision, old)
        self.assertEqual(w.loaded_dump.data, old_bytes)
        self.assertEqual(parse_hex(w.hex_edit.toPlainText()), old_bytes)
        self.assertFalse(w.dirty)
        self.assertFalse(w.save_button.isEnabled())
        self.assertEqual(w.library.list_revisions(cid), before)
        self.assertEqual(w.hex_edit.byte_offset_for_block(8), 128)
        self.run.assert_not_called();self.operation.assert_not_called()

    def test_lf_identifier_clears_classic_inspector_and_text_can_return_for_hf(self):
        w = self.window;self.load(classic())
        self.assertTrue(w.workbench_inspector.classic)
        w.workbench_text_toggle.setChecked(True)
        self.load(DumpData(bytes.fromhex('A1B2C3D4E5'),'bin',{'record_kind':'identifier'},1),'EM410x')
        self.assertFalse(w.workbench_inspector.classic)
        self.assertEqual(w.workbench_inspector.tiles, [])
        self.assertTrue(w.workbench_text_toggle.isHidden())
        self.assertTrue(w.card_editors.widget(1).isHidden())
        self.assertEqual(w.hex_edit.bytes_per_block, 1)
        self.load(classic())
        self.assertTrue(w.workbench_inspector.classic)
        self.assertFalse(w.workbench_text_toggle.isHidden())
        self.assertFalse(w.card_editors.widget(1).isHidden())
        self.assertEqual(w.hex_edit.bytes_per_block, 16)
        self.assertFalse(w.dirty)

    def test_byte_roles_and_unknown_ranges_track_inter_nibble_whitespace(self):
        w = self.window
        dump = classic()
        dump.metadata['partial_dump'] = {
            'version':1, 'kind':'mfc', 'uid':'A1B2C3D4', 'block_size':16,
            'expected_bytes':1024, 'complete':False, 'known_bytes':list(range(64)),
            'read_blocks':list(range(4)), 'missing_blocks':list(range(4,64)),
            'incomplete_blocks':list(range(4,64)),
        }
        cid = self.load(dump)
        # The normal load path must annotate before an edit is made.
        self.assertTrue(w.hex_edit.extraSelections())
        before = w.library.list_revisions(cid)
        # Each byte occupies four characters except for the final newline:
        # two nibbles separated by a space. This is valid input to parse_hex.
        text = '\n'.join(' '.join(f'{value:02X}') for value in dump.data)
        w.hex_edit.setPlainText(text)
        w._annotate_partial_bytes()
        selections = w.hex_edit.extraSelections()
        spans = {(item.cursor.selectionStart(),item.cursor.selectionEnd(),
                  item.format.foreground().color().name(),
                  item.format.background().color().name()) for item in selections}
        self.assertTrue(any(start==0 and end==63 and colour=='#edbd76'
                            for start,end,colour,_ in spans))
        self.assertTrue(any(start==192 and end==215 and colour=='#9abcf8'
                            for start,end,colour,_ in spans))
        self.assertTrue(any(start==216 and end==231 and colour=='#a7d6c9'
                            for start,end,colour,_ in spans))
        self.assertTrue(any(start==232 and end==255 and colour=='#9abcf8'
                            for start,end,colour,_ in spans))
        unknown = [item for item in selections if item.format.background().color().name()=='#4a3827']
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0].cursor.selectionStart(), 256)
        self.assertEqual(unknown[0].cursor.selectionEnd(), len(text))
        self.assertEqual(parse_hex(unknown[0].cursor.selectedText().replace('\u2029','\n')), dump.data[64:])
        self.assertEqual(parse_hex(w.hex_edit.toPlainText()), dump.data)
        self.assertEqual(w.loaded_dump.data, dump.data)
        self.assertEqual(w.library.list_revisions(cid), before)
        self.run.assert_not_called();self.operation.assert_not_called()

    def test_blocked_text_update_refreshes_inspector_and_encoding_error_remains_fixable(self):
        w = self.window;self.load(DumpData(b'A'*1024, 'bin', {}, 16))
        w.encoding.setCurrentIndex(w.encoding.findData('ascii'))
        w.workbench_text_toggle.setChecked(True)
        w.decoded.setPlainText('A'*1024)
        QTest.qWait(30)
        self.assertEqual(w.workbench_inspector.dump.data, b'A'*1024)
        w.decoded.setPlainText('Ж')
        QTest.qWait(30)
        self.assertTrue(w.text_error)
        self.assertTrue(w.hex_edit.isReadOnly())
        self.assertFalse(w.decoded.isReadOnly())
        self.assertTrue(w.workbench_text_toggle.isChecked())
        self.assertEqual(w.workbench_inspector.tiles, [])
        w.decoded.setPlainText('B'*1024)
        QTest.qWait(30)
        self.assertFalse(w.text_error)
        self.assertFalse(w.hex_edit.isReadOnly())
        self.assertEqual(w.workbench_inspector.dump.data, b'B'*1024)
        self.assertEqual(w.loaded_dump.data, b'A'*1024)


if __name__ == '__main__':unittest.main()
