"""USB-CLI protocol tests with a fragmented fake Flipper; no real port opened."""
import hashlib
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import serial

from pm3gui.flipper import (
    FlipperCancelled, FlipperError, USER_DICTIONARY, _CLI,
    available_flippers, fetch_key_files,
)


class FakeSerial:
    def __init__(self, files=None, *, directories=None, corrupt_md5=False,
                 fail_read=False, advertised_extra=0):
        self.files = files or {}
        self.extra_directories = directories or {}
        self.corrupt_md5 = corrupt_md5
        self.fail_read = fail_read
        self.advertised_extra = advertised_extra
        self.buffer = bytearray(b'Flipper USB CLI\r\n>: ')
        self.closed = False
        self.commands = []
        self.current = b''
        self.chunk_size = 0
        self.awaiting = False

    @property
    def in_waiting(self):
        return len(self.buffer)

    def read(self, count):
        if self.fail_read and self.awaiting:
            raise serial.SerialException('synthetic USB unplug')
        count = min(count, 7, len(self.buffer))
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        return result

    def reset_input_buffer(self):
        self.buffer.clear()

    def close(self):
        self.closed = True

    def write(self, value):
        if value == b'y' and self.awaiting:
            part = self.current[:self.chunk_size]
            self.current = self.current[len(part):]
            self.buffer.extend(part)
            if self.current:
                self.buffer.extend(b'\r\nReady?\r\n')
            else:
                self.awaiting = False
                self.buffer.extend(b'\r\n>: ')
            return 1
        command = value.decode('utf-8').rstrip('\r')
        self.commands.append(command)
        self.buffer.extend(value.rstrip(b'\r') + b'\r\n')
        if command == 'device_info':
            self.reply(b'hardware_model: Flipper Zero\r\nhardware_target: 7')
            return len(value)
        match = re.fullmatch(r'storage (list|stat|read_chunks|md5) "([^"]+)"(?: (\d+))?', command)
        if not match:
            raise AssertionError(f'Unexpected or mutating fake command: {command}')
        kind, path, size = match.groups()
        if kind == 'list':
            self.list_directory(path)
        elif path not in self.files:
            self.reply(b'Storage error: file/dir not exist')
        elif kind == 'stat':
            self.reply(f'File, size: {len(self.files[path])}b'.encode())
        elif kind == 'md5':
            digest = '0' * 32 if self.corrupt_md5 else hashlib.md5(self.files[path]).hexdigest()
            self.reply(digest.encode())
        else:
            self.current = self.files[path]
            self.chunk_size = int(size)
            self.awaiting = bool(self.current)
            self.buffer.extend(f'Size: {len(self.current) + self.advertised_extra}\r\n'.encode())
            self.buffer.extend(b'\r\nReady?\r\n' if self.current else b'\r\n>: ')
        return len(value)

    def reply(self, body):
        self.buffer.extend(body + b'\r\n\r\n>: ')

    def list_directory(self, path):
        entries = {}
        for remote, value in self.files.items():
            if not remote.startswith(path + '/'):
                continue
            remainder = remote[len(path) + 1:]
            if '/' in remainder:
                entries[remainder.split('/', 1)[0]] = '[D]'
            else:
                entries[remainder] = f'[F] {{name}} {len(value)}b'
        lines = []
        for name, fmt in entries.items():
            lines.append(f'\t[D] {name}' if fmt == '[D]' else '\t' + fmt.format(name=name))
        lines.extend(self.extra_directories.get(path, []))
        self.reply(('\r\n'.join(lines) or '\tEmpty').encode('utf-8'))


class FlipperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.destination = Path(self.temp.name) / 'download'
        self.flippers = self.enterContext(patch('pm3gui.flipper.available_flippers', return_value=[
            {'device': 'COM44', 'description': 'Flipper Zero', 'serial_number': 'FLIP_TEST'}]))

    def tearDown(self):
        self.temp.cleanup()

    def fetch(self, fake, **kwargs):
        with patch('pm3gui.flipper.serial.Serial', return_value=fake) as factory:
            result = fetch_key_files('COM44', self.destination, **kwargs)
        factory.assert_called_once_with('COM44', 115200, timeout=0.15, write_timeout=3)
        self.assertTrue(fake.closed)
        return result

    def test_discovery_excludes_proxmark_and_other_stm_devices(self):
        def port(device, vid, pid, number, description='USB serial'):
            return SimpleNamespace(device=device, vid=vid, pid=pid, serial_number=number,
                                   description=description, product=None, manufacturer=None)
        with patch('pm3gui.flipper.list_ports.comports', return_value=[
            port('COM3', 0x9AC4, 0x4B8F, 'ICEMAN__TEST'),
            port('COM44', 0x0483, 0x5740, 'FLIP_TEST'),
            port('COM45', 0x0483, 0x5740, 'STM_TEST'),
            port('COM46', 0x0483, 0x5740, '', 'Flipper Zero'),
        ]):
            result = available_flippers()
        self.assertEqual([entry['device'] for entry in result], ['COM44', 'COM46'])

    def test_wrong_port_rejected_before_serial_open(self):
        with patch('pm3gui.flipper.serial.Serial') as factory:
            with self.assertRaises(FlipperError):
                fetch_key_files('COM3', self.destination)
        factory.assert_not_called()

    def test_realistic_download_is_binary_exact_and_read_only(self):
        data = b'Filetype: Flipper NFC device\n' + bytes(range(256)) * 30 + b'>: Ready?\r\n'
        fake = FakeSerial({USER_DICTIONARY: b'# Candidate keys\n112233445566\n',
                           '/ext/nfc/.cache/A1B2C3D4.keys': b'Filetype: Flipper NFC keys\n',
                           '/ext/nfc/Folder/\u041a\u0430\u0440\u0442\u0430.nfc': data,
                           '/ext/nfc/assets/mf_classic_dict.nfc': b'not user data',
                           '/ext/nfc/ignore.txt': b'other data'})
        messages = []
        report = self.fetch(fake, progress=messages.append)
        self.assertEqual(len(report['files']), 3)
        self.assertEqual(report['errors'], [])
        for file in report['files']:
            self.assertEqual(Path(file['local']).read_bytes(), fake.files[file['remote']])
            self.assertEqual(file['verified_by'], 'size+remote_md5')
        self.assertFalse(any('storage list "/ext/nfc/assets"' == command for command in fake.commands))
        self.assertFalse(any('112233445566' in message for message in messages))
        self.assertTrue(all(command == 'device_info' or re.match(r'storage (list|stat|read_chunks|md5) ', command)
                            for command in fake.commands))

    def test_missing_user_dictionary_does_not_prevent_card_cache_download(self):
        fake = FakeSerial({'/ext/nfc/.cache/A1B2C3D4.keys': b'synthetic'})
        report = self.fetch(fake)
        self.assertEqual(len(report['files']), 1)
        self.assertTrue(any(item['remote'] == USER_DICTIONARY for item in report['skipped']))
        self.assertEqual(report['errors'], [])

    def test_shadow_and_original_are_downloaded_independently_with_verified_raw_hashes(self):
        original = b'Filetype: Flipper NFC device\n# original partial snapshot\n'
        shadow = b'Filetype: Flipper NFC device\n# updated shadow snapshot\n' + bytes(range(256))
        files = {'/ext/nfc/Folder/\u041a\u0430\u0440\u0442\u0430.nfc': original,
                 '/ext/nfc/Folder/\u041a\u0430\u0440\u0442\u0430.SHD': shadow}
        fake = FakeSerial(dict(files))
        report = self.fetch(fake)
        self.assertEqual({item['remote'] for item in report['files']}, set(files))
        self.assertEqual(report['errors'], [])
        for item in report['files']:
            expected = files[item['remote']]
            self.assertEqual(Path(item['local']).read_bytes(), expected)
            self.assertEqual(item['size'], len(expected))
            self.assertEqual(item['md5'], hashlib.md5(expected).hexdigest())
            self.assertEqual(item['sha256'], hashlib.sha256(expected).hexdigest())
            self.assertEqual(item['verified_by'], 'size+remote_md5')
        self.assertEqual(fake.files, files)
        self.assertEqual(len({item['local'] for item in report['files']}), 2)
        self.assertTrue(all(command == 'device_info' or re.match(r'storage (list|stat|read_chunks|md5) ', command)
                            for command in fake.commands))

    def test_shadows_obey_file_count_total_and_individual_byte_quotas(self):
        for limit in ('MAX_FILES', 'MAX_TOTAL_BYTES', 'MAX_FILE_BYTES'):
            with self.subTest(limit=limit):
                self.destination = Path(self.temp.name) / limit
                fake = FakeSerial({'/ext/nfc/card.nfc': b'a', '/ext/nfc/card.shd': b'bbbb'})
                with patch('pm3gui.flipper.' + limit, 1):
                    report = self.fetch(fake)
                self.assertEqual([item['remote'] for item in report['files']], ['/ext/nfc/card.nfc'])
                self.assertTrue(any(item['remote'] == '/ext/nfc/card.shd' for item in report['skipped']))
                self.assertFalse(any(command.startswith('storage read_chunks "/ext/nfc/card.shd"')
                                     for command in fake.commands))
                self.assertEqual((self.destination / 'nfc/card.nfc').read_bytes(), b'a')
                self.assertFalse((self.destination / 'nfc/card.shd').exists())

    def test_shadow_with_failed_remote_checksum_is_not_published(self):
        fake = FakeSerial({'/ext/nfc/card.shd': b'synthetic shadow'}, corrupt_md5=True)
        report = self.fetch(fake)
        self.assertEqual(report['files'], [])
        self.assertEqual(len(report['errors']), 1)
        self.assertEqual(report['errors'][0]['remote'], '/ext/nfc/card.shd')
        self.assertEqual(list(self.destination.rglob('*.shd')), [])

    def test_file_count_bytes_and_directory_depth_are_bounded(self):
        fake = FakeSerial({USER_DICTIONARY: b'a', '/ext/nfc/card.nfc': b'b',
                           '/ext/nfc/a/b/c/d/deep.nfc': b'not downloaded'})
        with patch('pm3gui.flipper.MAX_FILES', 1):
            report = self.fetch(fake)
        self.assertEqual(len(report['files']), 1)
        self.assertTrue(any('числа файлов' in item['reason'] for item in report['skipped']))
        self.assertFalse(any('/a/b/c/d"' in command for command in fake.commands))

    def test_oversized_files_are_not_requested(self):
        fake = FakeSerial({USER_DICTIONARY: b'12345'})
        with patch('pm3gui.flipper.MAX_TOTAL_BYTES', 4):
            report = self.fetch(fake)
        self.assertEqual(report['files'], [])
        self.assertFalse(any(command.startswith('storage read_chunks') for command in fake.commands))

    def test_unsafe_filename_and_local_overwrite_are_refused(self):
        fake = FakeSerial({USER_DICTIONARY: b'key'}, directories={'/ext/nfc': [
            '[F] ../escape.nfc 10b', '[F] NUL.nfc 10b', '[D] ..', '[F] bad"name.nfc 10b']})
        report = self.fetch(fake)
        self.assertEqual(len(report['files']), 1)
        self.assertEqual(len(report['skipped']), 4)
        self.assertFalse((self.destination.parent / 'escape.nfc').exists())
        with patch('pm3gui.flipper.serial.Serial') as factory:
            with self.assertRaises(FlipperError):
                fetch_key_files('COM44', self.destination)
        factory.assert_not_called()

    def test_remote_checksum_failure_never_publishes_a_local_file(self):
        fake = FakeSerial({USER_DICTIONARY: b'candidate'}, corrupt_md5=True)
        report = self.fetch(fake)
        self.assertEqual(report['files'], [])
        self.assertEqual(len(report['errors']), 1)
        self.assertEqual(list(self.destination.rglob('*.nfc')), [])

    def test_changed_size_and_disconnect_do_not_publish_partial_files(self):
        for options in [{'advertised_extra': 1}, {'fail_read': True}]:
            fake = FakeSerial({USER_DICTIONARY: b'candidate'}, **options)
            with self.subTest(options=options), patch('pm3gui.flipper.serial.Serial', return_value=fake):
                with self.assertRaises(FlipperError):
                    fetch_key_files('COM44', self.destination)
            self.assertTrue(fake.closed)
            self.assertEqual(list(self.destination.rglob('*.nfc')), [])

    def test_busy_port_explains_how_to_release_it(self):
        with patch('pm3gui.flipper.serial.Serial', side_effect=serial.SerialException('access denied')):
            with self.assertRaisesRegex(FlipperError, 'qFlipper'):
                fetch_key_files('COM44', self.destination)

    def test_cancellation_before_open_and_during_transfer(self):
        with patch('pm3gui.flipper.serial.Serial') as factory:
            with self.assertRaises(FlipperCancelled):
                fetch_key_files('COM44', self.destination, cancelled=lambda: True)
        factory.assert_not_called()
        fake = FakeSerial({USER_DICTIONARY: b'candidate'})
        with patch('pm3gui.flipper.serial.Serial', return_value=fake):
            with self.assertRaises(FlipperCancelled):
                fetch_key_files('COM44', self.destination, cancelled=lambda: fake.awaiting)
        self.assertTrue(fake.closed)
        self.assertEqual(list(self.destination.rglob('*.nfc')), [])

    def test_bounded_wait_reports_unresponsive_device(self):
        fake = FakeSerial()
        fake.buffer.clear()
        cli = _CLI(fake)
        with self.assertRaisesRegex(FlipperError, 'не отвечает'):
            cli.until(b'>: ', timeout=0.005)


if __name__ == '__main__':
    unittest.main()
