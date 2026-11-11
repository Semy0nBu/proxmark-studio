"""Offline checks of the actual frozen GUI and its relocated client bundle."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import traceback

from PySide6.QtCore import QProcess
from PySide6.QtWidgets import QApplication, QLabel

from .paths import application_root, default_data_dir, is_frozen, resource_root
from .version import APP_VERSION


def check_card_workspace(window):
    """Exercise bundled workspace code with synthetic bytes and zero device I/O."""
    from dataclasses import replace
    from .core import DumpData
    from .card_snapshot import known_byte_mask
    from .writing import HFWritePlan, WriteOptions
    from .operation_recovery import (Binding, SourceSelection, WriteSnapshot, ReadbackEvidence,
        Termination, Dispatch, Link, State, Action, decide_recovery)
    from .write_readback import readback_flow

    image = bytearray(1024)
    image[:4] = bytes.fromhex('A1B2C3D4')
    for sector in range(16):
        image[(sector*4+3)*16:(sector*4+4)*16] = bytes.fromhex('FFFFFFFFFFFFFF078069FFFFFFFFFFFF')
    metadata = {'read_validation': {'kind': 'mfc'}}
    cid = window.library.create_card('Synthetic Classic check', DumpData(bytes(image), 'bin', metadata, 16),
                                     uid='A1B2C3D4', protocol='MIFARE Classic 1K')
    window.refresh_library();window.load_card(cid);window.card_workspace.refresh()
    old = window.loaded_revision
    source = window.workspace_bridge.current_source()
    assert len(source.snapshot.known_bytes) == 832
    assert window.card_tabs.count() == 4 and window.card_workspace.grid.count() == 16
    memory = window.workspace_bridge.create_memory_map()
    assert len(memory.dump.metadata['partial_dump']['known_bytes']) == 832
    memory.close();memory.deleteLater()
    image[16] = 7
    new = window.library.save_revision(cid, DumpData(bytes(image), 'bin', metadata, 16), 'Synthetic change')
    window.load_card(cid);window.card_workspace.refresh()
    window.card_workspace.left.setCurrentIndex(window.card_workspace.left.findData(old))
    window.card_workspace.right.setCurrentIndex(window.card_workspace.right.findData(new))
    window.card_workspace.compare()
    assert window.card_workspace.diff_table.rowCount() == 1
    assert window.library.get_dump(cid, old).data[16] == 0
    window.open_write_dialog()
    assert window.write_dialog.recovery_panel is not None
    window.write_dialog.close()
    plan = HFWritePlan(DumpData(bytes(image), 'bin', metadata, 16),
        WriteOptions('mfc', include_keys=True, include_unknown=True, first=3, last=3), window.data_dir/'synthetic-plan')
    assert plan.next_operation() == 'hf search'
    try:
        plan.consume('[+] UID: A1 B2 C3 D4\nMIFARE Classic 1K\n', 0)
    except ValueError:
        pass
    else:
        raise AssertionError('Masked keys entered the write plan')
    assert plan.entries == [] and not plan.approved
    zero_image = bytearray(image);zero_image[48:54] = bytes(6)
    known_zero = DumpData(bytes(zero_image), 'bin', {'read_validation': {'kind': 'mfc', 'known_key_slots': [
        {'sector': 0, 'type': 'A', 'key_hex': '000000000000'}]}}, 16)
    assert all(known_byte_mask(known_zero)[48:54])
    binding = Binding('synthetic-operation', 'synthetic-target', 'synthetic-source', 'synthetic-plan')
    snapshot = WriteSnapshot(binding, SourceSelection('synthetic-revision', 'synthetic-source', True),
        Termination.FAILED, Dispatch.MAY_HAVE_WRITTEN, Link.READY, frozenset({1, 2}), frozenset({1}),
        error_present=True)
    assert decide_recovery(snapshot).state is State.WRITE_UNCERTAIN
    checked = replace(snapshot, readback=ReadbackEvidence(binding, frozenset({1, 2}), frozenset({1}), True, True))
    result = decide_recovery(checked)
    assert result.state is State.DIFFERENCES_CONFIRMED and result.next_action is Action.PREPARE_NEW_PLAN
    assert not result.auto_execute and snapshot.may_have_written == frozenset({1})
    assert callable(readback_flow)
    for index in range(window.card_tabs.count()):
        window.card_tabs.setCurrentIndex(index)
        QApplication.processEvents()
    window.card_tabs.setCurrentIndex(0)
    return {'name': 'card-workspace-v2-and-unknown-key-write-guard', 'passed': True,
            'known_classic_bytes': 832, 'synthetic_comparison_changes': 1,
            'physical_device_access': False}


def check_interface_modes(window):
    """Exercise the bundled controls and writers without any reader access."""
    from .interface_mode import Guidance
    window.open_write_dialog()
    dialog=window.write_dialog
    source=bytes(dialog.dump.data)
    options=dialog.options()
    assert window.interface_mode=='guided' and not dialog.transfer.compact
    assert not dialog.write_button.isEnabled()
    window.interface_mode_selector.setCurrentIndex(window.interface_mode_selector.findData('compact'))
    assert window.interface_mode=='compact' and dialog.transfer.compact
    assert not dialog.transfer.details.header.isChecked()
    assert bytes(dialog.dump.data)==source and dialog.options()==options
    assert not dialog.write_button.isEnabled() and window.read_job is None
    assert json.loads(window.settings_path.read_text(encoding='utf8'))['interface_mode']=='compact'
    assert window._load_settings()['interface_mode']=='compact'
    hints=window.findChildren(Guidance)
    assert hints and all(not hint.header.isChecked() for hint in hints)
    dialog.close()
    family=window.open_family_wizard()
    assert family.transfer.compact
    for index in range(family.operation.count()):
        family.operation.setCurrentIndex(index)
        assert family.transfer.compact and not family.commit.isEnabled()
    family.close()
    window.open_lf_write()
    assert window.lf_write_dialog.transfer.compact
    assert not window.lf_write_dialog.write.isEnabled()
    window.lf_write_dialog.close()
    window.interface_mode_selector.setCurrentIndex(window.interface_mode_selector.findData('guided'))
    assert not dialog.transfer.compact and dialog.transfer.details.header.isChecked()
    assert all(hint.header.isChecked() for hint in hints)
    assert json.loads(window.settings_path.read_text(encoding='utf8'))['interface_mode']=='guided'
    return {'name':'interface-modes-and-source-target-wizards','passed':True,
            'modes':['guided','compact'],'preference_persisted':True,
            'source_and_parameters_preserved':True,'physical_device_access':False}


def check_stop_completion(window):
    """Exercise the real completion signal with a runner that cannot access I/O."""
    runner=window.runner
    assert not runner.busy and window.read_job is None
    original_run,original_cancel=runner.run,runner.cancel
    commands=[]

    def fake_run(settings,command,**kwargs):
        assert command in {'hf search','hw version'}
        commands.append(command);runner._active=object()

    def reply(code):
        token=runner.completion_token;runner._active=None
        runner.busy_changed.emit(False)
        runner.completed.emit(token,code,'Synthetic release check')

    runner.run=fake_run;runner.cancel=lambda:reply(-2)
    try:
        window.read_hf_card();window.cancel_operation()
        assert commands==['hf search','hw version']
        assert window.read_job['stage']=='cancel_resync'
        window._log('[+] Communicating with PM3 over USB-CDC\nOS......... Iceman/release-test\n')
        reply(0)
        assert window.read_job is None and window.ports.isEnabled()
        # No client remains to emit a completion in this second scenario.
        window.read_hf_card();runner._active=None;window.cancel_operation()
        assert commands==['hf search','hw version']*2
        assert window.read_job['stage']=='cancel_resync'
        window._log('[+] Communicating with PM3 over USB-CDC\nOS......... Iceman/release-test\n')
        reply(0)
        assert window.read_job is None and not runner.busy
    finally:
        runner._active=None;runner.run,runner.cancel=original_run,original_cancel
    return {'name':'stop-completion-ownership-and-idle-recovery','passed':True,'physical_device_access':False}


def run_release_check(report_path: Path, *, language='en') -> int:
    report = {'version': APP_VERSION, 'frozen': is_frozen(), 'checks': [],
              'hardware_commands': 0, 'personal_data_opened': False, 'passed': False}
    window = None
    temporary = tempfile.TemporaryDirectory(prefix='proxmark-studio-check-')
    try:
        from . import app as app_module
        from .core import DumpData
        from .device import _client_environment
        from .firmware import FirmwareCatalog
        from .i18n import english_catalog
        from .process_env import external_process_libraries
        from .theme import apply_theme
        from .about import AboutDialog

        application = QApplication.instance() or QApplication([])
        report['qt_platform'] = application.platformName()
        apply_theme(application)
        root = application_root()
        report.update(application_root=str(root), resource_root=str(resource_root()),
                      default_data_dir=str(default_data_dir()))
        profile = Path(temporary.name) / 'profile'
        original_ports = app_module.available_ports
        app_module.available_ports = lambda: []
        try:
            window = app_module.MainWindow(profile, language=language)
        finally:
            app_module.available_ports = original_ports
        assert window.library.list_cards() == []
        assert window.key_library.list_keys() == []
        assert window.loaded_dump is None and not window.runner.busy
        assert len(english_catalog()) >= 1200
        for name in ('check.svg', 'chevron.svg', 'hf_commands.json', 'lf_commands.json', 'client_commands.json'):
            assert Path(__file__).with_name(name).is_file(), name
        report['checks'].append({'name': 'fresh-profile-and-bundled-ui-resources', 'passed': True,
                                 'language': window.ui_language})
        payload = bytes(range(16))
        cid = window.library.create_card('Synthetic release check', DumpData(payload, 'bin', {}, 16))
        window.refresh_library()
        window.load_card(cid)
        assert window.loaded_dump.data == payload
        for index in range(window.stack.count()):
            window.navigate(index)
            application.processEvents()
        report['checks'].append({'name': 'library-roundtrip-and-all-pages', 'passed': True})
        from .library_backup import create_backup, restore_backup, validate_profile
        archive = Path(temporary.name) / 'synthetic.pm3backup'
        restored = Path(temporary.name) / 'restored'
        create_backup(profile, archive, password='synthetic release check')
        restored_report = restore_backup(archive, restored, password='synthetic release check', active_profile=profile)
        assert restored_report['cards'] == 1 and validate_profile(restored)['revisions'] == 1
        report['checks'].append({'name': 'encrypted-library-backup-and-separate-profile', 'passed': True})
        original_run, original_typed = window.runner.run, window.runner.run_operation
        def prohibit_device_io(*args, **kwargs):
            raise AssertionError('Frozen workspace checks must not dispatch device commands')
        window.runner.run = window.runner.run_operation = prohibit_device_io
        try:
            report['checks'].append(check_card_workspace(window))
            report['checks'].append(check_interface_modes(window))
        finally:
            window.runner.run, window.runner.run_operation = original_run, original_typed
        report['checks'].append(check_stop_completion(window))
        from PySide6.QtCore import QEventLoop, QTimer
        window.navigate(1)
        window.show()
        settle = QEventLoop()
        QTimer.singleShot(300, settle.quit)
        settle.exec()
        assert window.isVisible() and int(window.winId()) and not window.runner.busy
        screenshot = Path(report_path).resolve().with_suffix('.png')
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        assert window.grab().save(str(screenshot), 'PNG')
        report['checks'].append({'name': 'native-window-created-and-rendered', 'passed': True,
                                 'screenshot': str(screenshot)})
        about = AboutDialog(window)
        assert APP_VERSION in ' '.join(label.text() for label in about.findChildren(QLabel))
        about.close()

        native = root / 'client' / 'proxmark3.exe'
        metadata_path = root / 'release.json'
        metadata = json.loads(metadata_path.read_text(encoding='utf-8')) if metadata_path.is_file() else {}
        if native.is_file():
            assert Path(window._suggest_client()).resolve() == native.resolve()
            environment = _client_environment(native)

            def run_native(name, arguments, marker=None):
                process = QProcess()
                process.setProcessEnvironment(environment)
                process.setWorkingDirectory(temporary.name)
                process.setProgram(str(native))
                process.setArguments(arguments)
                with external_process_libraries():
                    process.start()
                if not process.waitForFinished(20000):
                    process.kill()
                    process.waitForFinished(3000)
                    raise RuntimeError(name + ': timeout')
                output = bytes(process.readAllStandardOutput()) + bytes(process.readAllStandardError())
                assert process.exitCode() == 0, name + ': ' + output.decode('utf-8', 'replace')[-2000:]
                if marker:
                    assert marker.encode() in output, name + ': missing expected result'
                report['checks'].append({'name': name, 'passed': True,
                                         'output_sha256': hashlib.sha256(output).hexdigest()})

            assert hashlib.sha256(native.read_bytes()).hexdigest() == metadata.get('native_client_sha256')
            run_native('native-version', ['--incognito', '--version'], 'Iceman')
            run_native('native-command-pipe', ['--help'], '--gui-pipe-bulk-cancel')
            run_native('native-script-resources', ['--incognito', '-c', 'script list'])
            lua = Path(temporary.name) / 'release-check.lua'
            lua.write_text('print("RELEASE_LUA_OK")\n', encoding='utf-8')
            run_native('native-lua', ['--incognito', '-l', str(lua)], 'RELEASE_LUA_OK')
            python = Path(temporary.name) / 'release-check.py'
            python.write_text(
                'import json, hashlib, pathlib, struct, sys\n'
                'assert pathlib.Path(sys.prefix).resolve() == pathlib.Path('+repr(str(native.parent/'python'))+').resolve()\n'
                'roots = [pathlib.Path(sys.prefix).resolve(), pathlib.Path('+repr(str(native.parent))+').resolve(), pathlib.Path('+repr(str(root/'share/proxmark3'))+').resolve(), pathlib.Path.cwd().resolve()]\n'
                'assert all(any(pathlib.Path(p or ".").resolve().is_relative_to(r) for r in roots) for p in sys.path), sys.path\n'
                'assert sys.dont_write_bytecode\n'
                'print("RELEASE_PYTHON_OK")\n', encoding='utf-8')
            run_native('native-private-python', ['--incognito', '-y', str(python)], 'RELEASE_PYTHON_OK')
            assert not list((native.parent/'python').rglob('__pycache__')), 'Native Python modified the program directory'
            for key in ('usb', 'blueshark'):
                prepared = FirmwareCatalog(root).prepare(key, 'COM1', backup_root=profile/'firmware-backups')
                assert prepared.client_path == native.resolve()
                assert not prepared.backup_directory.exists()
                report['checks'].append({'name': 'firmware-catalog-' + key, 'passed': True,
                                         'image_sha256': prepared.image_sha256})
        else:
            assert metadata.get('bundle_kind') == 'gui-only', 'The full release is missing its native client'
            report['checks'].append({'name': 'gui-only-explicit-client-selection', 'passed': True})
        assert not window.runner.busy
        report['passed'] = True
    except Exception:
        report['error'] = traceback.format_exc()
    finally:
        if window is not None:
            window.dirty = False
            window.close()
        temporary.cleanup()
        report_path = Path(report_path).resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0 if report['passed'] else 1
