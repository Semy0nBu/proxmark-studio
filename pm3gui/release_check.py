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
