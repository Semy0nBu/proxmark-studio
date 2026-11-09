"""UI state and firmware review tests; every hardware path is replaced."""
import os
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QObject, Signal, QCoreApplication, QEvent, Qt, QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from pm3gui.app import MainWindow
from pm3gui.firmware_ui import FirmwareDialog
from pm3gui.core import DumpData


class FakeWorker(QObject):
    progress = Signal(str)
    completed = Signal(dict)
    failed = Signal(str)
    finished = Signal()
    instances = []

    def __init__(self, prepared, parent=None):
        super().__init__(parent)
        self.prepared = prepared
        self.started = False
        self.instances.append(self)

    def start(self):
        self.started = True


class PresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.enterContext(patch('pm3gui.app.available_ports', return_value=[]))
        self.enterContext(patch('pm3gui.app.discover_client', return_value=''))
        self.enterContext(patch('pm3gui.app.QMessageBox.warning'))
        self.run_client = self.enterContext(patch('pm3gui.app.ClientRunner.run'))
        self.run_operation = self.enterContext(patch('pm3gui.app.ClientRunner.run_operation'))
        FakeWorker.instances = []
        self.enterContext(patch('pm3gui.firmware.FirmwareWorker', FakeWorker))
        catalog = self.enterContext(patch('pm3gui.firmware.FirmwareCatalog'))
        review = {'version':'GUI-test/v4.23346','client_path':str(self.root/'matching-client.exe'),
                  'image_path':str(self.root/'fullimage.elf'),'image_sha256':'a'*64,
                  'backup_directory':str(self.root/'backups')}
        self.prepared = SimpleNamespace(review=lambda:review)
        catalog.return_value.prepare.return_value = self.prepared
        self.window = MainWindow(self.root/'profile')
        self.dialog = None

    def tearDown(self):
        self.window._firmware_waiting = False
        self.window.firmware_worker = None
        if hasattr(self.window, '_firmware_release_timer'):
            self.window._firmware_release_timer.stop()
        if self.dialog is not None:
            self.dialog.close()
        self.window.dirty = False
        self.window.read_job = None
        self.window.antenna_job = None
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.temp.cleanup()

    def review_dialog(self):
        self.dialog = FirmwareDialog(self.window, 'blueshark')
        self.window.firmware_dialog = self.dialog
        return self.dialog

    def start_fake_install(self):
        dialog = self.review_dialog()
        dialog.flash_button.click()
        self.window._firmware_wait_for_release()
        return FakeWorker.instances[-1]

    def test_opening_review_or_changing_variant_never_disconnects_or_flashes(self):
        with patch.object(self.window.runner, 'disconnect') as disconnect:
            dialog = self.review_dialog()
            dialog.variant.setCurrentIndex(dialog.variant.findData('usb'))
            self.window.firmware_variant.setCurrentIndex(self.window.firmware_variant.findData('usb'))
            disconnect.assert_not_called()
        self.assertEqual(FakeWorker.instances, [])
        self.run_client.assert_not_called()
        self.run_operation.assert_not_called()

    def test_details_and_journal_are_collapsed_without_losing_controls(self):
        self.assertFalse(self.window.journal_toggle.isChecked())
        self.assertFalse(self.window.read_options.header.isChecked())
        self.window.connection_details.setExpanded(True)
        self.assertTrue(self.window.connection_details.header.isChecked())
        self.assertTrue(self.window.client_path.isEnabled())
        self.run_client.assert_not_called()

    def test_rapid_navigation_finishes_on_requested_page_without_lingering_effect(self):
        self.window.show()
        for index in (3,1,4,0,3):
            self.window.navigate(index)
        QTest.qWait(180)
        self.assertEqual(self.window.stack.currentIndex(), 3)
        self.assertIsNone(self.window.stack.currentWidget().graphicsEffect())
        self.run_client.assert_not_called()

    def test_flash_waits_for_owned_client_to_release_its_port(self):
        dialog = self.review_dialog()
        self.window.runner._session.process = Mock()
        with patch.object(self.window.runner, 'disconnect') as disconnect:
            dialog.flash_button.click()
            self.window._firmware_wait_for_release()
            disconnect.assert_called_once()
        self.assertTrue(self.window.firmware_busy)
        self.assertEqual(FakeWorker.instances, [])
        self.window.runner._session.process = None
        self.window._firmware_wait_for_release()
        self.assertTrue(FakeWorker.instances[0].started)

    def test_install_locks_other_device_actions_and_failure_preserves_settings(self):
        self.window.client_path.setText('previous.exe')
        worker = self.start_fake_install()
        self.assertFalse(self.window.read_button.isEnabled())
        self.assertFalse(self.window.overview_read_button.isEnabled())
        self.assertFalse(self.window.stop_button.isEnabled())
        self.window.read_card()
        self.window.run_command('hf search', 90000)
        self.window.start_antenna_measurement()
        self.run_client.assert_not_called()
        self.run_operation.assert_not_called()
        worker.failed.emit('Readback mismatch')
        worker.finished.emit()
        self.assertFalse(self.window.firmware_busy)
        self.assertEqual(self.window.client_path.text(), 'previous.exe')
        self.assertIn('Readback mismatch', self.dialog.status.text())
        self.assertTrue(self.window.read_button.isEnabled())

    def test_only_verified_install_updates_client_and_usb_profile(self):
        worker = self.start_fake_install()
        worker.completed.emit({'verified':True,'profile':'usb','label':'USB без Blue Shark','version':'GUI-test',
                               'client_path':str(self.root/'new-client.exe'),'port':'COM9'})
        worker.finished.emit()
        self.assertEqual(self.window._port(), 'COM9')
        self.assertEqual(self.window._transport(), 'usb')
        self.assertEqual(self.window.client_path.text(), str(self.root/'new-client.exe'))
        self.assertEqual(self.window.settings['firmware_profile'], 'usb')
        self.assertIn('установлена и проверена', self.dialog.status.text())
        self.run_client.assert_not_called()

    def test_worker_start_error_unlocks_the_interface(self):
        self.review_dialog()
        self.dialog.flash_button.click()
        with patch('pm3gui.firmware.FirmwareWorker', side_effect=RuntimeError('Cannot start worker')):
            self.window._firmware_wait_for_release()
        self.assertFalse(self.window.firmware_busy)
        self.assertTrue(self.window.read_button.isEnabled())
        self.assertIn('Cannot start worker', self.dialog.status.text())

    def test_battery_is_explicitly_unavailable_and_firmware_badge_uses_device_reply(self):
        self.window.transport_combo.setCurrentIndex(self.window.transport_combo.findData('blueshark'))
        self.assertIn('недоступен', self.window.battery_status.text())
        self.assertNotIn('%', self.window.battery_status.text())
        self.window.update_firmware_badge('OS......... GUI-BlueShark\nFPC USART for BT add-on... present\n')
        self.assertEqual(self.window.firmware_current.text(), 'Blue Shark')
        self.window.update_firmware_badge('OS......... GUI-cancel\nFPC USART for BT add-on... absent\n')
        self.assertIn('без Blue Shark', self.window.firmware_current.text())

    def test_relocated_card_action_runs_once_and_navigation_preserves_result(self):
        self.window.read_button.click()
        self.run_client.assert_called_once()
        self.assertEqual(self.run_client.call_args.args[1], 'hf search')
        for i in (1, 2, 3, 4, 0):
            self.window.navigate(i)
        self.run_client.assert_called_once()
        self.assertIsNotNone(self.window.read_job)
        self.assertIn('HF', self.window.device_info.text())

    def test_initial_port_population_does_not_claim_a_completed_operation(self):
        self.window._reset_connection_status()
        self.assertEqual(self.window.operation_badge.text(), 'Ожидание действия')
        self.assertIn('Нет подключения', self.window.global_connection.text())
        self.assertFalse(self.window.ui_has_operation)

    def test_global_journal_and_actual_connection_remain_accessible_on_every_page(self):
        self.window.show()
        self.window.runner._session.connected = True
        self.window.runner._session.transport = 'usb'
        self.window.ports.setEditText('COM9')
        self.window.runner._session.connected = True
        self.window._session_changed(True, 'Подтверждено устройством')
        self.window._log('Synthetic output\n')
        self.window.journal_toggle.setChecked(True)
        for i in range(5):
            self.window.navigate(i)
            self.assertTrue(self.window.journal.isVisible())
            self.assertIn('Подключено', self.window.global_connection.text())
            self.assertIn('COM9', self.window.global_connection.text())
        self.window.runner._session.connected = False
        self.window._session_changed(False, 'Связь потеряна')
        self.assertIn('Нет подключения', self.window.global_connection.text())
        self.assertIn('Synthetic output', self.window.log.toPlainText())
        self.run_client.assert_not_called()

    def test_date_sort_is_chronological_and_copy_tracks_sorted_rows(self):
        for key, date in [('111111111111','2026-01-30T10:00:00Z'), ('222222222222','2026-02-02T10:00:00Z')]:
            self.window.key_library.add_keys([key])
            self.window.key_library.connection.execute('UPDATE mf_keys SET created_at=? WHERE key_hex=?', (date,key))
        self.window.key_library.connection.commit()
        self.window.refresh_keys()
        self.window.key_table.sortItems(4, Qt.SortOrder.AscendingOrder)
        self.assertEqual(self.window.key_table.item(0,0).text(), '111111111111')
        self.window.key_table.selectRow(0)
        next(action for action in self.window.key_table.actions() if action.text() == 'Копировать').trigger()
        self.assertTrue(self.app.clipboard().text().startswith('111111111111\t'))

    def test_table_context_menu_copies_without_disabling_keyboard_copy(self):
        self.window.key_library.add_keys(['A0A1A2A3A4A5'])
        self.window.refresh_keys()
        self.window.show()
        self.window.navigate(2)
        self.app.processEvents()
        self.window.key_table.selectRow(0)
        def inspect_menu(menu, *args):
            action = next(a for a in menu.actions() if a.text()=='Копировать')
            self.assertTrue(action.isEnabled())
            action.trigger()
        from PySide6.QtWidgets import QMenu
        self.window.key_table.customContextMenuRequested.emit(QPoint(20,15))
        menu=self.window.key_table.findChildren(QMenu)[-1]
        inspect_menu(menu)
        menu.close()
        self.assertTrue(self.app.clipboard().text().startswith('A0A1A2A3A4A5\t'))
        self.assertTrue(self.window.key_table.actions()[0].isEnabled())

    def test_ui_state_persists_splitters_without_saving_uncommitted_connection_edits(self):
        self.window.settings_path.write_text(json.dumps({'client_path':'saved.exe','usb_port':'COM3','extra':42}),encoding='utf-8')
        self.window.show()
        self.window.navigate(1)
        self.app.processEvents()
        self.window.library_splitter.setSizes([370,700])
        expected = self.window.library_splitter.sizes()
        self.window.client_path.setText('unsaved.exe')
        self.window._save_presentation()
        saved = json.loads(self.window.settings_path.read_text(encoding='utf-8'))
        self.assertEqual(saved['client_path'], 'saved.exe')
        self.assertEqual(saved['extra'],42)
        other = MainWindow(self.window.data_dir)
        try:
            other.show()
            other.navigate(1)
            self.app.processEvents()
            self.assertTrue(other.geometry_restored)
            restored = other.library_splitter.sizes()
            self.assertAlmostEqual(restored[0]/sum(restored),expected[0]/sum(expected),delta=.01)
        finally:
            other.close()
            other.deleteLater()

    def test_codec_selection_does_not_mutate_data_and_errors_are_inline(self):
        self.window.converter_hex.setPlainText('FF')
        self.window.converter_text.setPlainText('original')
        self.window.converter_encoding.setCurrentIndex(self.window.converter_encoding.findData('ascii'))
        self.assertEqual(self.window.converter_hex.toPlainText(),'FF')
        self.assertEqual(self.window.converter_text.toPlainText(),'original')
        self.window.convert_hex()
        self.assertEqual(self.window.converter_text.toPlainText(),'original')
        self.assertEqual(self.window.converter_info.property('tone'),'error')
        self.window.converter_hex.setPlainText('41')
        self.window.convert_hex()
        self.assertEqual(self.window.converter_text.toPlainText(),'A')
        self.assertEqual(self.window.converter_info.property('tone'),'')

    def test_wrapped_card_names_remain_available_at_small_size_and_splitter_is_adjustable(self):
        name = 'Длинное название карты для проверки полного отображения ' * 3
        self.window.library.create_card(name, DumpData(b'1234','bin',{},16),uid='12345678')
        self.window.refresh_library()
        self.window.show()
        self.window.resize(920,640)
        self.window.navigate(1)
        self.app.processEvents()
        item = self.window.cards.item(0)
        self.assertIn(name.strip(),item.toolTip())
        self.assertGreater(item.sizeHint().height(), self.window.cards.fontMetrics().height()*3)
        before = self.window.library_splitter.sizes()
        self.window.library_splitter.setSizes([300,380])
        self.assertNotEqual(before,self.window.library_splitter.sizes())
        self.run_client.assert_not_called()

    def test_large_key_list_yields_to_events_and_new_filter_cancels_old_population(self):
        rows = [{'key_hex':f'{i:012X}','label':'synthetic','source':'fixture',
                 'created_at':'2026-11-09T00:00:00Z'} for i in range(750)]
        rows[0]['label'] = 'small'
        with patch.object(self.window.key_library, 'list_keys', return_value=rows):
            self.window.refresh_keys()
            self.assertEqual(self.window.key_table.rowCount(),250)
            self.assertTrue(self.window._key_fill_timer.isActive())
            self.assertIn('250/750',self.window.key_count.text())
            self.window.key_search.setText('small')
            self.app.processEvents()
            self.assertEqual(self.window.key_table.rowCount(),1)
            self.assertFalse(self.window._key_fill_timer.isActive())
            self.assertIn('найдено по фильтру: 1',self.window.key_count.text())
            self.window.key_search.clear()
            QTest.qWait(80)
            self.assertEqual(self.window.key_table.rowCount(),750)
            self.assertFalse(self.window._key_fill_timer.isActive())
            self.assertTrue(self.window.key_table.isSortingEnabled())
        self.run_client.assert_not_called()


if __name__ == '__main__':
    unittest.main()
