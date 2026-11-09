"""Firmware checks use synthetic ELF, memory streams and fake subprocess I/O."""
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer
from PySide6.QtWidgets import QApplication
from pm3gui.firmware import (
    BOOTROM_SIZE, FLASH_BASE, INTERNAL_SIZE, OS_START, SPI_SIZE, ClientResult,
    FirmwareCancelled, FirmwareCatalog, FirmwareError, FirmwareWorker,
    _SystemFlashIO, _crc_a_wire, execute_flash, inspect_fullimage, read_internal_flash,
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def elf_bytes():
    data = bytearray(304)
    data[:16] = b'\x7fELF\x01\x01\x01' + bytes(9)
    struct.pack_into('<HHIIIIIHHHHHH', data, 16, 2, 40, 1, OS_START+1, 52, 0, 0, 52, 32, 2, 0, 0, 0)
    struct.pack_into('<8I', data, 52, 1, 256, OS_START, OS_START, 32, 32, 5, 4)
    struct.pack_into('<8I', data, 84, 1, 288, 0x200000, OS_START+64, 16, 16, 6, 4)
    data[256:] = bytes(range(48))
    return bytes(data)


class FakeIO:
    def __init__(self, prepared):
        self.prepared = prepared
        self.old = bytes([0xA5]) * INTERNAL_SIZE
        new = bytearray(self.old)
        image = prepared.image_path.read_bytes()
        for segment in prepared.segments:
            offset = segment.address-FLASH_BASE
            new[offset:offset+segment.size] = image[segment.file_offset:segment.file_offset+segment.size]
        self.internal = [self.old, self.old, bytes(new)]
        self.spi = [b'S' * SPI_SIZE] * 3
        self.calls = []
        self.exclusive_calls = 0
        self.resident_on_second_check = False
        self.flash_exit = 0
        self.flash_output = '[+] All done\n'
        self.spi_error = False
        self.flash_calls = 0
        self.cancelled = False
        self.cancel_after_flash = False
        self.port = SimpleNamespace(device='COM3', vid=0x9AC4, pid=0x4B8F, serial_number='ICEMAN_TEST')

    def ports(self):
        return [self.port]

    def ensure_exclusive(self):
        self.exclusive_calls += 1
        if self.resident_on_second_check and self.exclusive_calls == 2:
            raise FirmwareError('Another client exists')

    def read_internal(self, port, cancelled):
        self.calls.append(('read_internal', port, bool(cancelled())))
        return self.internal.pop(0)

    def wait_usb(self, port, serial_number):
        self.calls.append(('wait_usb', port, serial_number))
        return port

    def run_client(self, prepared, args, cwd, logfile, timeout):
        self.calls.append(('client', list(args), timeout))
        channel = '[+] Communicating with PM3 over USB-CDC\n'
        if '--flash' in args:
            self.flash_calls += 1
            if self.cancel_after_flash:
                self.cancelled = True
            output = self.flash_output
            rc = self.flash_exit
        elif args[-1] == 'hw version':
            output = (channel + '  Device.................... RDV4\n  AT91SAM7S512\n'
                      + '  OS......... ' + prepared.version + '-unclean ' + prepared.arm_checksum + '\n'
                      + '  FPC USART for BT add-on... ' + ('present' if prepared.profile.key == 'blueshark' else 'absent') + '\n')
            rc = 0
        elif args[-1].startswith('mem dump -f '):
            name = args[-1].split()[-1]
            data = self.spi.pop(0)
            (Path(cwd)/(name+'.bin')).write_bytes(data)
            output = channel + 'Saved 262144 bytes to binary file\n'
            if self.spi_error:
                output += '[#] reading flash memory failed :: pos0\n'
            rc = 0
        else:
            raise AssertionError(args)
        Path(logfile).write_text(output, encoding='utf-8')
        return ClientResult(rc, output)


class MemorySerial:
    def __init__(self, data, fragment=97, short_write=False):
        self.data = bytearray(data)
        self.fragment = fragment
        self.short_write = short_write
        self.requests = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def reset_input_buffer(self):
        pass

    @property
    def in_waiting(self):
        return len(self.data)

    def write(self, data):
        self.requests.append(data)
        return len(data)-int(self.short_write)

    def read(self, count):
        size = min(count, self.fragment, len(self.data))
        result = bytes(self.data[:size])
        del self.data[:size]
        return result


def old_packet(command, offset=0, count=0, payload=b''):
    return struct.pack('<4Q', command, offset, count, 0) + payload.ljust(512, b'\0')


def ng_packet(command, payload=b'', *, status=0, crc=None, ng=True):
    header = struct.pack('<4sHbbH', b'PM3b', len(payload) | (0x8000 if ng else 0), status, 0, command)
    return header + payload + struct.pack('<H', _crc_a_wire(header+payload) if crc is None else crc)


class FirmwareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.setup = self.root/'outputs/device-setup'
        self.setup.mkdir(parents=True)
        self.manifests = {}
        for key, stem in [('usb', 'gui-cancel'), ('blueshark', 'gui-blueshark')]:
            bundle = self.root/stem
            bundle.mkdir()
            image = bundle/'fullimage.elf'
            image.write_bytes(elf_bytes())
            client = bundle/'client/proxmark3.exe'
            client.parent.mkdir()
            client.write_bytes(b'MZ synthetic '+key.encode())
            (client.parent/'libs').mkdir()
            (client.parent/'libs/runtime-test.dll').write_bytes(b'reviewed DLL fixture')
            (client.parent/'libs/.pm3gui-runtime-manifest.json').write_text(json.dumps({
                'kind': 'proxmark3-gui-local-ucrt64-runtime-v1',
                'files': [{'relative_target': 'runtime-test.dll', 'sha256': digest(b'reviewed DLL fixture')}]}))
            self.manifests[key] = {
                'platform': 'PM3RDV4', 'flash_capacity': INTERNAL_SIZE,
                'platform_extras': ['BTADDON'] if key == 'blueshark' else [],
                'custom_version': 'Iceman/'+stem+'/v4.23346-dirty', 'arm_sources_checksum': '123456789',
                'matching_client_runtime_root': str(client.parent),
                'artifact_files': {'fullimage.elf': {'path': str(image), 'bytes': len(elf_bytes()), 'sha256': digest(elf_bytes())},
                                   'proxmark3.exe': {'path': str(client), 'sha256': digest(client.read_bytes())},
                                   'default_version_pm3.c': {'sha256': 'a'*64}},
            }
            self.write_manifest(key)
        self.catalog = FirmwareCatalog(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def write_manifest(self, key):
        stem = 'gui-cancel' if key == 'usb' else 'gui-blueshark'
        (self.setup/(stem+'-build-manifest.json')).write_text(json.dumps(self.manifests[key]), encoding='utf-8')

    def prepared(self, key='usb'):
        return self.catalog.prepare(key, port='COM3')

    def test_prepare_and_catalog_only_read_files_and_expose_concrete_review(self):
        with patch('pm3gui.firmware.serial.Serial', side_effect=AssertionError('opened port')), patch(
                'pm3gui.firmware.list_ports.comports', side_effect=AssertionError('enumerated devices')):
            profiles = self.catalog.profiles()
            self.assertEqual([p.available for p in profiles], [True, True])
            prepared = self.prepared('blueshark')
        review = prepared.review()
        self.assertEqual(review['flash_scope'], 'main-fullimage-only')
        self.assertEqual(review['port'], 'COM3')
        self.assertEqual(review['internal_backup_bytes'], 524288)
        self.assertEqual(review['spi_backup_bytes'], 262144)
        self.assertTrue(review['bootrom_preserved'])
        self.assertFalse(prepared.backup_directory.exists())
        self.assertEqual(review['flash_argv'].count('--image'), 1)
        self.assertNotIn('--unlock-bootloader', review['flash_argv'])
        self.assertNotIn('--force', review['flash_argv'])

    def test_missing_manifest_is_an_unavailable_profile_not_an_exception(self):
        (self.setup/'gui-cancel-build-manifest.json').unlink()
        self.assertFalse(self.catalog.profiles()[0].available)
        self.assertTrue(self.catalog.profiles()[0].reason)
        with self.assertRaises(FirmwareError):
            self.prepared()

    def test_external_manifest_relative_artifacts_are_supported(self):
        manifest = self.manifests['usb']
        root = self.root/'gui-cancel'
        manifest['matching_client_runtime_root'] = 'client'
        manifest['artifact_files']['fullimage.elf']['path'] = 'fullimage.elf'
        manifest['artifact_files']['proxmark3.exe']['path'] = 'client/proxmark3.exe'
        path = root/'portable.json'
        path.write_text(json.dumps(manifest), encoding='utf-8')
        self.assertEqual(self.catalog.prepare('usb', manifest_path=path).image_path, root/'fullimage.elf')

    def test_compatible_client_only_extension_overrides_stale_client_hash(self):
        prepared = self.prepared('blueshark')
        new = b'MZ reviewed client-only extension'
        prepared.client_path.write_bytes(new)
        entries = {name: {'exists': True, 'sha256': value} for name, value in [
            ('armsrc/obj/fullimage.elf', prepared.image_sha256), ('armsrc/mifarecmd.c', 'b'*64),
            ('common/default_version_pm3.c', 'a'*64), ('armsrc/version_pm3.c', 'a'*64)]}
        certificate = {'status': 'ready_offline_verified', 'arm_and_metadata_unchanged': True,
                       'client': str(prepared.client_path), 'new_exe_sha256': digest(new), 'before': entries, 'after': entries}
        path = self.setup/'gui-scoped-cancel-client-build-result.json'
        path.write_text(json.dumps(certificate), encoding='utf-8')
        updated = self.prepared('blueshark')
        self.assertEqual(updated.client_sha256, digest(new))
        certificate['after'] = dict(entries, **{'armsrc/obj/fullimage.elf': {'exists': True, 'sha256': 'c'*64}})
        path.write_text(json.dumps(certificate), encoding='utf-8')
        with self.assertRaises(FirmwareError):
            self.prepared('blueshark')

    def test_rejects_wrong_platform_capacity_bt_profile_and_changed_image(self):
        for field, value in [('platform', 'PM3GENERIC'), ('flash_capacity', 262144), ('platform_extras', ['BTADDON'])]:
            original = self.manifests['usb'][field]
            self.manifests['usb'][field] = value
            self.write_manifest('usb')
            with self.subTest(field=field), self.assertRaises(FirmwareError):
                self.prepared()
            self.manifests['usb'][field] = original
        self.write_manifest('usb')
        prepared = self.prepared()
        prepared.image_path.write_bytes(b'changed')
        with self.assertRaises(FirmwareError):
            self.prepared()

    def test_runtime_manifest_and_reviewed_dll_are_required_and_rechecked(self):
        prepared = self.prepared()
        (prepared.runtime_dir/'runtime-test.dll').write_bytes(b'changed')
        with self.assertRaises(FirmwareError):
            self.prepared()
        with self.assertRaises(FirmwareError):
            execute_flash(prepared, io=FakeIO(prepared))

    def test_elf_rejects_bootrom_overlap_segments_phdr_and_invalid_entry(self):
        original = elf_bytes()
        mutations = [(64, '<I', 0x100000), (96, '<I', OS_START+16), (42, '<H', 31),
                     (44, '<H', 1000), (72, '<I', 4), (24, '<I', OS_START+1000)]
        for offset, fmt, value in mutations:
            data = bytearray(original)
            struct.pack_into(fmt, data, offset, value)
            path = self.root/'invalid.elf'
            path.write_bytes(data)
            with self.subTest(offset=offset), self.assertRaises(FirmwareError):
                inspect_fullimage(path)
        self.assertEqual(sum(s.size for s in inspect_fullimage(self.root/'gui-cancel/fullimage.elf')), 48)

    def test_success_backs_up_twice_flashes_only_fullimage_and_verifies_all_regions(self):
        prepared = self.prepared('blueshark')
        io = FakeIO(prepared)
        result = execute_flash(prepared, io=io)
        self.assertTrue(result['verified'])
        self.assertTrue(result['bootrom_unchanged'] and result['spi_unchanged'])
        self.assertEqual(result['compared_elf_bytes'], 48)
        self.assertEqual(io.flash_calls, 1)
        self.assertEqual(io.exclusive_calls, 2)
        writes = [call[1] for call in io.calls if call[0] == 'client' and '--flash' in call[1]]
        self.assertEqual(writes, [prepared.flash_argv()[1:]])
        for name, size in [('before-internal.bin', INTERNAL_SIZE), ('before-spi-1.bin', SPI_SIZE), ('after-internal.bin', INTERNAL_SIZE)]:
            self.assertEqual((prepared.backup_directory/name).stat().st_size, size)
        self.assertEqual(json.loads(Path(result['report_path']).read_text(encoding='utf-8'))['status'], 'installed_readback_verified')

    def test_bad_usb_and_resident_client_stop_before_any_flash(self):
        prepared = self.prepared()
        io = FakeIO(prepared)
        io.port.vid = 0x0483
        with self.assertRaises(FirmwareError):
            execute_flash(prepared, io=io)
        self.assertEqual(io.flash_calls, 0)
        io = FakeIO(prepared)
        io.resident_on_second_check = True
        with self.assertRaises(FirmwareError):
            execute_flash(prepared, io=io)
        self.assertEqual(io.flash_calls, 0)

    def test_disagreeing_backups_stop_before_flash(self):
        prepared = self.prepared()
        io = FakeIO(prepared)
        io.internal[1] = bytes(INTERNAL_SIZE)
        with self.assertRaises(FirmwareError):
            execute_flash(prepared, io=io)
        self.assertEqual(io.flash_calls, 0)

    def test_full_saved_spi_with_read_error_is_not_a_verified_backup(self):
        prepared = self.prepared()
        io = FakeIO(prepared)
        io.spi_error = True
        with self.assertRaises(FirmwareError):
            execute_flash(prepared, io=io)
        self.assertEqual(io.flash_calls, 0)
        report = json.loads((prepared.backup_directory/'report.json').read_text(encoding='utf-8'))
        self.assertIsNone(report['bootrom_preserved'])
        self.assertFalse(report['verified'])

    def test_exit0_and_all_done_after_flash_failure_never_claim_installation(self):
        prepared = self.prepared()
        io = FakeIO(prepared)
        io.flash_output = 'The flashing procedure failed...\n[+] All done\n'
        with self.assertRaises(FirmwareError):
            execute_flash(prepared, io=io)
        report = json.loads((prepared.backup_directory/'report.json').read_text(encoding='utf-8'))
        self.assertTrue(report['flash_may_have_written'])
        self.assertIsNone(report['firmware_flashed'])
        self.assertIsNone(report['bootrom_preserved'])
        self.assertFalse(report['verified'])
        self.assertEqual(len(io.internal), 1)
        self.assertEqual(io.flash_calls, 1)

    def test_bootrom_or_elf_or_spi_mismatch_never_reports_verified_success(self):
        for fault in ('bootrom', 'elf', 'spi'):
            prepared = self.prepared()
            io = FakeIO(prepared)
            if fault == 'spi':
                io.spi[-1] = bytes(SPI_SIZE)
            else:
                changed = bytearray(io.internal[-1])
                changed[0 if fault == 'bootrom' else OS_START-FLASH_BASE] ^= 1
                io.internal[-1] = bytes(changed)
            with self.subTest(fault=fault), self.assertRaises(FirmwareError):
                execute_flash(prepared, io=io)
            state = json.loads((prepared.backup_directory/'report.json').read_text(encoding='utf-8'))
            self.assertFalse(state['verified'])
            self.assertIsNone(state['bootrom_preserved'])
            self.assertEqual(state['status'], 'failed_after_flash')
            self.assertEqual(io.flash_calls, 1)

    def test_failed_flasher_has_no_automatic_second_write(self):
        prepared = self.prepared()
        io = FakeIO(prepared)
        io.flash_exit = 4
        with self.assertRaises(FirmwareError):
            execute_flash(prepared, io=io)
        self.assertEqual(io.flash_calls, 1)
        self.assertEqual(len(io.internal), 1)

    def test_changed_reviewed_files_and_cancel_stop_before_io(self):
        prepared = self.prepared()
        io = FakeIO(prepared)
        with self.assertRaises(FirmwareCancelled):
            execute_flash(prepared, io=io, cancelled=lambda: True)
        self.assertEqual(io.calls, [])
        prepared.client_path.write_bytes(b'tampered')
        with self.assertRaises(FirmwareError):
            execute_flash(prepared, io=io)
        self.assertEqual(io.calls, [])

    def test_cancellation_after_write_start_does_not_interrupt_verification(self):
        prepared = self.prepared()
        io = FakeIO(prepared)
        io.cancel_after_flash = True
        self.assertTrue(execute_flash(prepared, io=io, cancelled=lambda: io.cancelled)['verified'])
        self.assertEqual(io.flash_calls, 1)
        self.assertFalse([call for call in io.calls if call[0] == 'read_internal'][-1][2])

    def test_worker_signals_verified_result_with_fake_io_only(self):
        prepared = self.prepared()
        worker = FirmwareWorker(prepared, io=FakeIO(prepared))
        loop, results, errors = QEventLoop(), [], []
        worker.completed.connect(lambda value: (results.append(value), loop.quit()))
        worker.failed.connect(lambda value: (errors.append(value), loop.quit()))
        QTimer.singleShot(5000, loop.quit)
        worker.start()
        loop.exec()
        self.assertTrue(worker.wait(2000))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]['verified'])
        worker.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_resident_process_check_refuses_without_killing(self):
        fake = SimpleNamespace(returncode=0, stdout='"proxmark3.exe","1234","Console","1","1 K"\n')
        with patch('pm3gui.firmware.os.name', 'nt'), patch('pm3gui.firmware.subprocess.run', return_value=fake) as run:
            with self.assertRaises(FirmwareError):
                _SystemFlashIO().ensure_exclusive()
        self.assertEqual(Path(run.call_args.args[0][0]).name.casefold(), 'tasklist.exe')

    def test_flasher_uses_matching_private_qt_python_and_a_bounded_path(self):
        prepared = self.prepared()
        python = prepared.client_path.parent / 'python'
        encodings = python / 'lib/python3.14/encodings'
        encodings.mkdir(parents=True)
        (encodings/'__init__.py').write_text('', encoding='utf-8')
        (python/'bin').mkdir()
        (prepared.runtime_dir/'platforms').mkdir()
        scripts = prepared.client_path.parent / 'pyscripts'
        scripts.mkdir()
        foreign = str(self.root / 'foreign runtime')
        process = SimpleNamespace(poll=lambda: 0, returncode=0)
        events = []
        with patch.dict(os.environ, {'PATH': foreign, 'PYTHONHOME': foreign, 'PYTHONPATH': foreign,
                                     'QT_PLUGIN_PATH': foreign, 'QT_QPA_PLATFORM_PLUGIN_PATH': foreign,
                                     'QT_QPA_PLATFORM': 'offscreen'}), \
                patch('pm3gui.firmware.external_process_libraries') as external, \
                patch('pm3gui.firmware.subprocess.Popen', side_effect=lambda *a, **kw: events.append('spawn') or process) as popen:
            external.return_value.__enter__.side_effect = lambda: events.append('enter')
            external.return_value.__exit__.side_effect = lambda *a: events.append('restore')
            result = _SystemFlashIO().run_client(prepared, ['--help'], self.root, self.root/'client.log', 1)
        self.assertEqual(events, ['enter', 'spawn', 'restore'])
        self.assertEqual(result.returncode, 0)
        environment = popen.call_args.kwargs['env']
        self.assertEqual(environment['PYTHONHOME'], str(python))
        self.assertEqual(environment['PYTHONPATH'], str(scripts))
        self.assertEqual(environment['PYTHONNOUSERSITE'], '1')
        self.assertEqual(environment['QT_PLUGIN_PATH'], str(prepared.runtime_dir))
        self.assertEqual(environment['QT_QPA_PLATFORM_PLUGIN_PATH'], str(prepared.runtime_dir/'platforms'))
        self.assertNotIn('QT_QPA_PLATFORM', environment)
        self.assertNotIn(foreign, environment['PATH'])
        self.assertIn(str(python/'bin'), environment['PATH'])


class InternalReaderTests(unittest.TestCase):
    def read(self, stream, **kwargs):
        return read_internal_flash('COM3', serial_factory=lambda *args, **options: stream, **kwargs)

    def test_fragmented_complete_old_backup_with_ng_debug_crc(self):
        data = bytes(range(256))*2048
        packets = [ng_packet(0x100, b'\0\0debug')]
        packets += [old_packet(0x10b, i, 512, data[i:i+512]) for i in range(0, INTERNAL_SIZE, 512)]
        packets.append(old_packet(0xff, 1))
        stream = MemorySerial(b''.join(packets))
        self.assertEqual(self.read(stream), data)
        self.assertTrue(stream.closed)
        self.assertEqual(stream.requests, [struct.pack('<4Q', 0x10A, 0, INTERNAL_SIZE, 0)+bytes(512)])

    def test_incomplete_out_of_bounds_conflicting_and_bad_crc_replies_reject(self):
        examples = [old_packet(0xff, 1), old_packet(0x10b, INTERNAL_SIZE-1, 2, b'xx'),
                    old_packet(0x10b, 0, 2, b'aa')+old_packet(0x10b, 0, 2, b'bb'),
                    ng_packet(0x100, b'debug', crc=0), ng_packet(0x100, b'', status=-1),
                    b'PM3b'+struct.pack('<HbbH', 8193, 0, 0, 0x100)]
        for data in examples:
            stream = MemorySerial(data)
            with self.subTest(length=len(data)), self.assertRaises(FirmwareError):
                self.read(stream)
            self.assertTrue(stream.closed)

    def test_timeout_cancel_and_short_write_are_bounded_and_close_serial(self):
        clock = iter([0, 0, 2])
        stream = MemorySerial(b'')
        with self.assertRaises(FirmwareError):
            self.read(stream, timeout=1, clock=lambda: next(clock))
        self.assertTrue(stream.closed)
        stream = MemorySerial(b'')
        with self.assertRaises(FirmwareCancelled):
            self.read(stream, cancelled=lambda: True)
        self.assertTrue(stream.closed)
        with self.assertRaises(FirmwareError):
            self.read(MemorySerial(b'', short_write=True))


if __name__ == '__main__':
    unittest.main()
