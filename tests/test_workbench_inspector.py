"""Real native inspector controls using synthetic snapshots and no device I/O."""
from copy import deepcopy
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import re
import unittest

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPlainTextEdit, QWidget

from pm3gui.core import DumpData, parse_hex
from pm3gui.i18n import set_language
from pm3gui.operations import CLASSIC_GEOMETRIES, sector_first_block, sector_trailer
from pm3gui.studio_data import encode_access, structured_dump
from pm3gui.workbench_ui import WorkbenchInspector


KEY_A = bytes.fromhex('A1A2A3A4A5A6')
KEY_B = bytes.fromhex('B1B2B3B4B5B6')


def classic(size='1k', metadata=None, conditions=(0, 0, 0, 3)):
    sectors, length = CLASSIC_GEOMETRIES[size]
    data = bytearray(length)
    for sector in range(sectors):
        offset = sector_trailer(sector)*16
        data[offset:offset+16] = KEY_A + encode_access(conditions) + KEY_B
    return DumpData(bytes(data), 'bin', metadata or {}, 16)


def partial(known):
    dump = classic()
    known = sorted(set(known))
    read = [block for block in range(64) if all(i in known for i in range(block*16, block*16+16))]
    dump.metadata['partial_dump'] = {
        'version': 1, 'kind': 'mfc', 'uid': 'A1B2C3D4', 'block_size': 16,
        'expected_bytes': 1024, 'complete': False, 'known_bytes': known,
        'read_blocks': read, 'missing_blocks': sorted(set(range(64))-set(read)),
        'incomplete_blocks': [block for block in range(64) if block not in read],
    }
    return dump


class Owner(QWidget):
    def __init__(self, dump=None, protocol='MIFARE Classic 1K'):
        super().__init__()
        self.loaded_dump = dump
        self.protocol = QLineEdit(protocol)
        self.hex_edit = QPlainTextEdit()
        if dump:self.hex_edit.setPlainText(dump.data.hex(' ').upper())
        self.dirty = False
        self.text_error = ''
        self.map_calls = 0
        self.hex_edit.textChanged.connect(self.mark_dirty)

    def mark_dirty(self):self.dirty = True

    def _working_dump(self):
        if self.text_error:raise ValueError(self.text_error)
        dump = self.loaded_dump
        return DumpData(parse_hex(self.hex_edit.toPlainText()), dump.format, dump.metadata, dump.block_size)

    def open_memory_map(self):self.map_calls += 1


class WorkbenchInspectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        set_language('en')
        self.owner = None
        self.panel = None

    def tearDown(self):
        if self.panel is not None:
            self.panel.timer.stop(); self.panel.close(); self.panel.deleteLater()
        if self.owner is not None:self.owner.close(); self.owner.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        set_language('ru')

    def create(self, dump=None, protocol='MIFARE Classic 1K'):
        self.owner = Owner(dump, protocol)
        self.panel = WorkbenchInspector(self.owner)
        return self.panel

    def cursor_byte(self, offset):
        digits = list(re.finditer('[0-9A-Fa-f]', self.owner.hex_edit.toPlainText()))
        cursor = self.owner.hex_edit.textCursor(); cursor.setPosition(digits[offset*2].start())
        self.owner.hex_edit.setTextCursor(cursor)
        self.panel.update_cursor()

    def test_empty_state_and_navigation_do_not_mutate_the_document(self):
        panel = self.create()
        self.assertFalse(panel.classic); self.assertFalse(panel.map_button.isEnabled())
        self.assertEqual(panel.tiles, []); self.assertFalse(self.owner.dirty)

    def test_all_supported_geometries_require_an_explicit_classic_family(self):
        panel = self.create(classic('mini'), 'MIFARE Classic Mini')
        for size, (count, _) in CLASSIC_GEOMETRIES.items():
            dump = classic(size); self.owner.loaded_dump = dump
            self.owner.protocol.setText('MIFARE Classic '+size.upper())
            self.owner.hex_edit.setPlainText(dump.data.hex(' ')); self.owner.dirty = False
            panel.refresh()
            self.assertTrue(panel.classic); self.assertEqual(panel.sector_count, count)
            self.assertEqual(len(panel.tiles), count); self.assertFalse(self.owner.dirty)
            self.assertTrue(panel.select_sector(count-1)); self.assertFalse(self.owner.dirty)

    def test_4k_boundary_sectors_and_cursor_address_are_correct(self):
        panel = self.create(classic('4k'), 'MIFARE Classic 4K')
        source = deepcopy(self.owner.loaded_dump)
        text = self.owner.hex_edit.toPlainText()
        for sector, block in ((31,124), (32,128), (39,240)):
            self.assertTrue(panel.select_sector(sector))
            self.assertEqual(panel.sector, sector); self.assertEqual(panel.selected_byte, block*16)
            self.assertEqual(panel.block_label.text(), str(block))
            self.assertTrue(panel.tiles[sector].isChecked())
        self.assertEqual(self.owner.hex_edit.toPlainText(), text)
        self.assertEqual(self.owner.loaded_dump, source); self.assertFalse(self.owner.dirty)
        self.assertEqual(self.owner.map_calls, 0)

    def test_large_sector_access_groups_and_trailer_follow_current_block(self):
        panel = self.create(classic('4k', conditions=(0,3,4,3)), 'MIFARE Classic 4K')
        for block, read, write in ((128,'A / B','A / B'), (132,'A / B','A / B'),
                                    (133,'B','B'), (137,'B','B'), (138,'A / B','B'), (142,'A / B','B')):
            self.cursor_byte(block*16)
            self.assertEqual(panel.sector, 32)
            self.assertEqual(panel.read_rights.text(), read)
            self.assertEqual(panel.write_rights.text(), write)
        self.cursor_byte(143*16)
        self.assertEqual(panel.read_title.text(), 'Read ACL')
        self.assertEqual(panel.role_label.text(), 'Trailer A/B/ACL')
        self.assertEqual(panel.read_rights.text(), 'A / B')
        self.assertEqual(panel.write_rights.text(), 'B')

    def test_full_classic_read_masks_a_and_b_without_explicit_key_slots(self):
        panel = self.create(classic(metadata={'read_validation': {'kind':'mfc'}}))
        self.assertEqual(panel.key_a.text(), 'Unknown'); self.assertEqual(panel.key_b.text(), 'Unknown')
        self.assertEqual(sum(panel.mask), 832)
        self.assertIn('832', panel.coverage.text())
        self.assertIsNotNone(panel.sectors[0].conditions)
        self.assertEqual(panel.tiles[0].property('memoryState'), 'partial')

    def test_recorded_key_is_available_but_never_a_widget_value(self):
        metadata = {'read_validation': {'kind':'mfc', 'known_key_slots': [
            {'sector':0, 'type':'A', 'key_hex':KEY_A.hex()}]}}
        panel = self.create(classic(metadata=metadata))
        self.assertEqual(panel.key_a.text(), 'Recorded · hidden')
        self.assertEqual(panel.key_b.text(), 'Unknown')
        texts = ' '.join(label.text()+' '+label.toolTip() for label in panel.findChildren(QLabel))
        texts += ' '.join(tile.toolTip() for tile in panel.tiles)
        self.assertNotIn(KEY_A.hex().upper(), texts); self.assertNotIn(KEY_B.hex().upper(), texts)
        self.assertNotIn(KEY_A.hex(), texts); self.assertNotIn(KEY_B.hex(), texts)

    def test_recorded_zero_key_is_not_treated_as_unknown(self):
        dump = classic(metadata={'read_validation': {'kind':'mfc', 'known_key_slots': [
            {'sector':0, 'type':'A', 'key_hex':'000000000000'}]}})
        data = bytearray(dump.data); data[48:54] = bytes(6); dump.data = bytes(data)
        panel = self.create(dump)
        self.assertEqual(panel.key_a.text(), 'Recorded · hidden')

    def test_edited_key_does_not_inherit_old_read_metadata_evidence(self):
        metadata = {'read_validation': {'kind':'mfc', 'known_key_slots': [
            {'sector':0, 'type':'A', 'key_hex':KEY_A.hex()}]}}
        panel = self.create(classic(metadata=metadata))
        changed = bytearray(self.owner.loaded_dump.data); changed[48] = 0
        self.owner.hex_edit.setPlainText(bytes(changed).hex(' ')); panel.refresh()
        self.assertEqual(panel.key_a.text(), 'Unknown')
        self.assertEqual(self.owner.loaded_dump.data[48:54], KEY_A)

    def test_partial_unknown_acl_does_not_decode_zero_fill_as_rights(self):
        panel = self.create(partial(range(54)))
        self.assertEqual(panel.key_a.text(), 'Recorded · hidden')
        self.assertEqual(panel.key_b.text(), 'Unknown')
        self.assertEqual(panel.access_bits.text(), 'Unknown')
        self.assertEqual(panel.read_rights.text(), '—'); self.assertEqual(panel.write_rights.text(), '—')
        self.assertEqual(panel.tiles[0].property('memoryState'), 'partial')
        panel.select_sector(1)
        self.assertEqual(panel.tiles[1].property('memoryState'), 'missing')
        self.assertEqual(panel.key_a.text(), 'Unknown')
        self.assertEqual(panel.access_bits.text(), 'Unknown')
        self.assertIn('Unread', panel.status.text())

    def test_known_zero_data_stays_known(self):
        panel = self.create(partial(range(16)))
        self.assertEqual(self.owner.loaded_dump.data[:16], bytes(16))
        self.assertTrue(all(panel.mask[:16])); self.assertIn('16 / 1024', panel.coverage.text())
        self.assertFalse(any(panel.mask[16:]))

    def test_known_corrupt_acl_is_invalid_and_not_an_access_permission(self):
        dump = classic(); data = bytearray(dump.data); data[54] ^= 1; dump.data = bytes(data)
        panel = self.create(dump)
        self.assertEqual(panel.access_bits.text(), 'Invalid')
        self.assertEqual(panel.tiles[0].property('memoryState'), 'invalid')
        self.assertEqual(panel.read_rights.text(), '—')

    def test_uid_block_does_not_claim_writeability_from_acl_alone(self):
        panel = self.create(classic())
        self.assertEqual(panel.role_label.text(), 'UID block')
        self.assertEqual(panel.write_rights.text(), 'Card-specific')
        self.assertIn('manufacturer-block',panel.write_rights.toolTip())
        self.cursor_byte(16)
        self.assertEqual(panel.write_rights.text(), 'A / B')
        self.assertEqual(panel.write_rights.toolTip(), '')

    def test_untyped_and_nonclassic_same_size_buffers_do_not_get_a_classic_map(self):
        panel = self.create(DumpData(bytes(1024),'bin',{},16), '')
        for protocol in ('', 'LEGIC', 'EM410x'):
            self.owner.protocol.setText(protocol); panel.refresh()
            self.assertFalse(panel.classic); self.assertEqual(panel.tiles, [])
            self.assertFalse(panel.map_button.isEnabled())
            self.assertNotIn('Key', panel.status.text())

    def test_lf_identifier_remains_a_record_not_complete_memory(self):
        panel = self.create(DumpData(bytes.fromhex('A1B2C3D4E5'),'bin',{'record_kind':'identifier'},1),'EM410x')
        self.assertEqual(panel.kind.text(), 'Identifier'); self.assertFalse(panel.classic)
        self.assertIn('not a chip memory image', panel.status.text())
        self.cursor_byte(3); self.assertEqual(panel.selected_byte,3)
        self.assertEqual(panel.block_label.text(), '3')

    def test_structured_snapshots_are_an_inspection_only_state(self):
        dump = structured_dump({'protocol':'EM410x','id':'A1B2C3D4E5'},'EM410x',kind='identifier-record')
        panel = self.create(dump, 'EM410x')
        self.assertIn('Structured record', panel.kind.text()); self.assertFalse(panel.classic)
        self.assertFalse(panel.select_sector(0)); self.assertFalse(panel.map_button.isEnabled())
        self.assertFalse(self.owner.dirty)

    def test_invalid_hex_clears_previous_keys_and_map_without_further_edits(self):
        panel = self.create(classic())
        self.owner.hex_edit.setPlainText('0'); panel.refresh()
        self.assertFalse(panel.classic); self.assertEqual(panel.key_a.text(), '—')
        self.assertEqual(panel.tiles, []); self.assertIn('Invalid', panel.kind.text())
        self.assertFalse(panel.select_sector(0)); self.assertEqual(self.owner.hex_edit.toPlainText(),'0')

    def test_geometry_metadata_disagreement_is_not_silently_relabelled(self):
        panel = self.create(classic('4k'), 'MIFARE Classic 1K')
        self.assertFalse(panel.classic); self.assertEqual(panel.tiles, [])
        self.assertIn('Invalid', panel.kind.text())

    def test_whitespace_between_nibbles_maps_cursor_by_real_bytes(self):
        panel = self.create(classic())
        self.owner.hex_edit.setPlainText('\n'.join(' '.join(f'{b:02X}') for b in self.owner.loaded_dump.data))
        self.owner.dirty = False; panel.refresh()
        self.assertTrue(panel.select_sector(2))
        selected = self.owner.hex_edit.textCursor().selectedText().replace('\u2029','\n')
        self.assertEqual(parse_hex(selected), self.owner.loaded_dump.data[128:144])
        self.assertEqual(panel.selected_byte, 128); self.assertFalse(self.owner.dirty)

    def test_large_generic_record_does_not_build_offset_sets_or_sector_tiles(self):
        panel = self.create(DumpData(bytes(8192), 'bin', {}, 16), '')
        self.assertFalse(panel.classic); self.assertEqual(panel._positions, ())
        self.assertEqual(panel.tiles, []); self.assertEqual(panel.byte_label.text(), '0x0000')
        self.assertIn('8192', panel.coverage.text())
        self.assertLess(len(panel._large_prefixes), 10)
        self.cursor_byte(4097)
        self.assertEqual(panel.selected_byte,4097)
        cursor=self.owner.hex_edit.textCursor();cursor.setPosition(len(self.owner.hex_edit.toPlainText()))
        self.owner.hex_edit.setTextCursor(cursor);panel.update_cursor()
        self.assertEqual(panel.selected_byte,8191)

    def test_blocked_text_signals_can_be_refreshed_explicitly(self):
        panel = self.create(classic())
        original = self.owner.hex_edit.toPlainText()
        self.owner.hex_edit.blockSignals(True)
        self.owner.hex_edit.setPlainText('F')
        self.owner.hex_edit.blockSignals(False)
        panel.refresh()
        self.assertFalse(panel.classic)
        self.owner.hex_edit.blockSignals(True); self.owner.hex_edit.setPlainText(original); self.owner.hex_edit.blockSignals(False)
        panel.refresh()
        self.assertTrue(panel.classic)

    def test_memory_map_is_an_explicit_offline_action_only(self):
        panel = self.create(classic()); panel.select_sector(2)
        self.assertEqual(self.owner.map_calls, 0)
        panel.map_button.click(); self.assertEqual(self.owner.map_calls, 1)

    def test_selection_and_statuses_are_localized(self):
        set_language('ru')
        panel = self.create(partial(range(16))); panel.select_sector(1)
        self.assertIn('Сектор 1', panel.heading.text())
        self.assertEqual(panel.key_a.text(),'Неизвестен'); self.assertIn('Не прочитано', panel.status.text())


if __name__ == '__main__':unittest.main()
