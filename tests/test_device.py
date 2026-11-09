"""Real child-process tests with a fake CLI; no RFID hardware is opened."""

import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from pm3gui.device import (
    ClientRunner, DeviceSettings, MAX_OUTPUT_BYTES, MAX_RECOVERY_TIMEOUT_MS, MAX_REPEATED_AUTH2_ERRORS, RECOVERY_TAIL_CHARACTERS, _StreamCleaner,
    available_ports, build_invocation, build_operation_invocation, discover_client, parse_connection_status,
    operation_requires_resync, operation_supports_bulk_cancel,
)
from pm3gui.operations import Operation, parse_recovered_keys


FAKE_CLIENT = r'''
import json, os, pathlib, sys, time
config=json.loads(pathlib.Path(__file__).with_suffix('.json').read_text())
args=sys.argv[1:]
if '--help' in args:
    print('fake CLI '+('--incognito' if config.get('incognito',True) else 'legacy'))
    sys.exit(config.get('help_exit',0))
if config.get('stream'):
    for part in [b'\x1b[3', b'2m\xd0', b'\x9f\xd1\x80', b'\xd0\xb8\xd0\xb2\xd0\xb5\xd1\x82', b'\x1b[0', b'm\n']:
        os.write(1,part)
        time.sleep(0.02)
print('ARGS='+json.dumps(args),flush=True)
print('CWD='+os.getcwd(),flush=True)
if config.get('size'):
    block=b'x'*65536
    for _ in range(config['size']//len(block)+1):
        os.write(1,block)
if config.get('ending'):
    print('\n'+config['ending'],flush=True)
for line in config.get('lines',[]):
    print(line,flush=True)
    time.sleep(config.get('line_delay',0))
time.sleep(config.get('delay',0))
sys.exit(config.get('exit',0))
'''


class DeviceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.script = self.root / "fake client.py"
        self.script.write_text(FAKE_CLIENT, encoding="utf-8")
        self.config = self.script.with_suffix(".json")
        self.configure()
        self.runner = ClientRunner(argument_prefix=[str(self.script)])
        self.settings = DeviceSettings(sys.executable, "COM123", str(self.root))
        self.outputs = []
        self.results = []
        self.busy_events = []
        self.runner.output.connect(self.outputs.append)
        self.runner.finished.connect(lambda code, text: self.results.append((code, text)))
        self.runner.busy_changed.connect(self.busy_events.append)

    def tearDown(self):
        if self.runner.busy:
            self.runner.cancel()
            self.wait_for(lambda: not self.runner.busy)
        self.temp.cleanup()

    def configure(self, **values):
        self.config.write_text(json.dumps(values), encoding="utf-8")

    def wait_for(self, predicate, timeout=6000):
        if predicate():
            return
        loop = QEventLoop()
        poll = QTimer()
        poll.setInterval(5)
        poll.timeout.connect(lambda: loop.quit() if predicate() else None)
        watchdog = QTimer()
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(loop.quit)
        poll.start()
        watchdog.start(timeout)
        loop.exec()
        poll.stop()
        watchdog.stop()
        self.assertTrue(predicate(), "Timed out waiting for fake CLI")

    def run_and_wait(self, **kwargs):
        initial = len(self.results)
        self.runner.run(self.settings, "hw version", **kwargs)
        self.wait_for(lambda: len(self.results) > initial)
        return self.results[-1]

    def test_invocation_is_scoped_and_argv_not_shell(self):
        before = dict(os.environ)
        executable, args, cwd, environment = build_invocation(self.settings, "hf search")
        self.assertEqual(args, ["-p", "COM123", "-f", "-c", "hf search"])
        self.assertEqual(executable, str(Path(sys.executable).resolve()))
        self.assertEqual(cwd, str(self.root.resolve()))
        self.assertEqual(environment.value("HOME"), os.environ.get("HOME", ""))
        self.assertEqual(dict(os.environ), before)
        _, offline_args, _, _ = build_invocation(self.settings, "hw version", True)
        self.assertNotIn("-p", offline_args)

    def test_bundled_libraries_and_qt_are_child_only(self):
        executable = self.root / "proxmark3.exe"
        executable.touch()
        (self.root / "libs" / "shell").mkdir(parents=True)
        settings = DeviceSettings(str(executable), "COM1")
        before = dict(os.environ)
        _, _, _, env = build_invocation(settings, "hw version")
        self.assertIn(str(self.root / "libs"), env.value("PATH"))
        self.assertIn(str(self.root / "libs" / "shell"), env.value("PATH"))
        self.assertEqual(env.value("QT_PLUGIN_PATH"), str(self.root / "libs"))
        self.assertEqual(env.value("QT_QPA_PLATFORM_PLUGIN_PATH"), str(self.root / "libs"))
        (self.root / "libs" / "platforms").mkdir()
        _, _, _, modern_env = build_invocation(settings, "hw version")
        self.assertEqual(modern_env.value("QT_QPA_PLATFORM_PLUGIN_PATH"), str(self.root / "libs" / "platforms"))
        self.assertEqual(dict(os.environ), before)

    def test_command_and_port_validation(self):
        for command in ("hw version; hf mf restore", "hw version\nquit", "hw version | x",
                        "hw version\x00", "hf mf restore", "hw version ", "hw\tversion"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                build_invocation(self.settings, command)
        for port in ("", "-p", "COM3;", "COM3\n", "COM0", "tcp:localhost:1234", "COM¹"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                build_invocation(DeviceSettings(sys.executable, port), "hw version")
        with self.assertRaises(ValueError):
            build_invocation(self.settings, "lf search", True)

    def test_discovery_only_known_project_location(self):
        location = self.root / "reference" / "legacy" / "test" / "client" / "proxmark3.exe"
        location.parent.mkdir(parents=True)
        location.touch()
        self.assertEqual(discover_client(self.root), str(location.resolve()))
        self.assertEqual(discover_client(self.root / "other"), "")

    def test_available_ports_does_not_open_devices(self):
        class Port:
            device, description, hwid = "COM7", "Proxmark3", "USB VID:PID=9AC4:4B8F"
        with patch("pm3gui.device.list_ports.comports", return_value=[Port()]):
            self.assertEqual(available_ports()[0]["device"], "COM7")

    def test_port_hints_classify_bluetooth_and_pm3_without_opening(self):
        ports = [
            SimpleNamespace(device="COM8", description="Serial", hwid="USB VID:PID=1234:5678", vid=0x1234, pid=0x5678),
            SimpleNamespace(device="COM7", description="Proxmark", hwid="USB VID:PID=9AC4:4B8F"),
            SimpleNamespace(device="COM6", description="Standard Serial over Bluetooth link", hwid="BTHENUM\\DEV_1234"),
            SimpleNamespace(device="COM5", description="UART", hwid="BTHMODEM\\1234"),
            SimpleNamespace(device="COM4", description="Device", hwid="", vid=0x9AC4, pid=0x4B8F),
            SimpleNamespace(device="COM3", description=None, hwid=None),
        ]
        with patch("pm3gui.device.list_ports.comports", return_value=ports):
            result = available_ports()
        self.assertEqual([(item["device"], item["kind"]) for item in result], [
            ("COM3", "other"), ("COM4", "usb"), ("COM5", "bluetooth"),
            ("COM6", "bluetooth"), ("COM7", "usb"), ("COM8", "other")])

    def test_blueshark_uses_only_explicit_com_and_no_virtual_baud_override(self):
        settings = DeviceSettings(sys.executable, "COM42", str(self.root), "blueshark")
        for command in ("hw version", "hf search", "lf search"):
            with self.subTest(command=command):
                _, args, _, _ = build_invocation(settings, command)
                self.assertEqual(args, ["-p", "COM42", "-f", "-c", command])
        _, args, _, _ = build_operation_invocation(settings, Operation("hf14a_select"))
        self.assertEqual(args, ["-p", "COM42", "-f", "-c", "hf 14a reader --skip"])
        _, offline, _, _ = build_invocation(settings, "hw version", offline=True)
        self.assertNotIn("-p", offline)
        for bad in ("", "COM0", "bt:00:11:22:33:44:55", "COM42\n"):
            with self.subTest(port=bad), self.assertRaises(ValueError):
                build_invocation(DeviceSettings(sys.executable, bad, transport="blueshark"), "hw version")
        for bad in ("bluetooth", "bt", "", None):
            with self.subTest(transport=bad), self.assertRaises(ValueError):
                DeviceSettings(transport=bad)
        settings.transport = "invalid"
        with self.assertRaises(ValueError):
            build_operation_invocation(settings, Operation("hf14a_select"))

    def test_connection_status_requires_rf_device_os_and_negotiated_transport(self):
        version = "Iceman/codex/GUI-BlueShark/v4.23346-dirty 2026-11-08 4bc161944"
        uart = "[+] Communicating with PM3 over FPC UART\n  OS......... " + version + "\n"
        usb = uart.replace("FPC UART", "USB-CDC")
        for output, expected in ((uart, "blueshark"), (usb, "usb"), (uart.replace("FPC UART", "FPC USART"), "blueshark")):
            with self.subTest(expected=expected, output=output):
                result = parse_connection_status(output, expected)
                self.assertTrue(result["connected"])
                self.assertEqual(result["transport"], expected)
                self.assertEqual(result["firmware"], version)
        ansi = "\x1b[32m" + uart.replace("FPC UART", "FPC \x1b[33mUART\x1b[0m") + "\x1b[0m"
        self.assertTrue(parse_connection_status(ansi, "blueshark")["connected"])
        for output in ("", "Client: Iceman/master OS:Windows", "Using UART port COM42\n  OS......... " + version,
                       "[+] Communicating with PM3 over FPC UART", uart + "OFFLINE mode\n", uart + usb):
            with self.subTest(output=output):
                self.assertFalse(parse_connection_status(output, "blueshark")["connected"])
        mismatch = parse_connection_status(usb, "blueshark")
        self.assertFalse(mismatch["connected"])
        self.assertEqual(mismatch["transport"], "usb")
        self.assertIn("COM", mismatch["message"])
        self.assertFalse(parse_connection_status(uart, "blueshark", returncode=1)["connected"])
        self.assertFalse(parse_connection_status(usb, "blueshark")["connected"])
        with self.assertRaises(ValueError):
            parse_connection_status(uart, "invalid")

    def test_actual_subprocess_incognito_and_working_directory(self):
        code, message = self.run_and_wait(offline=True)
        self.assertEqual(code, 0)
        self.assertIn("журнал", message)
        text = "".join(self.outputs)
        self.assertIn('"--incognito"', text)
        self.assertNotIn('"-p"', text)
        self.assertIn("CWD=" + str(self.root), text)
        self.assertEqual(self.busy_events, [True, False])

    def test_typed_read_runs_with_existing_process_controls(self):
        operation = Operation("mfu_dump", self.root / "card")
        initial = len(self.results)
        self.runner.run_operation(self.settings, operation)
        self.wait_for(lambda: len(self.results) > initial)
        self.assertEqual(self.results[-1][0], 0)
        args_line = next(line for line in "".join(self.outputs).splitlines() if line.startswith("ARGS="))
        arguments = json.loads(args_line[5:])
        self.assertEqual(arguments[arguments.index("-c") + 1], operation.render())
        self.assertIn("--incognito", arguments)
        self.assertEqual(self.busy_events, [True, False])
        with self.assertRaises(ValueError):
            self.runner.run(self.settings, operation.render())
        with self.assertRaises(ValueError):
            build_operation_invocation(self.settings, "hf mfu dump -f unsafe")
        class CustomOperation(Operation):
            pass
        with self.assertRaises(ValueError):
            build_operation_invocation(self.settings, CustomOperation("mfc_info"))

    def test_antenna_catalog_scopes_offscreen_graph_and_finite_timeout(self):
        sweep = Operation("antenna_sweep", self.root / "lf-sweep")
        with patch.dict(os.environ, {"QT_QPA_PLATFORM": "windows"}):
            _, arguments, _, env = build_operation_invocation(self.settings, sweep)
            self.assertEqual(env.value("QT_QPA_PLATFORM"), "offscreen")
            self.assertEqual(arguments[-1], sweep.render())
            self.assertEqual(os.environ["QT_QPA_PLATFORM"], "windows")
            _, _, _, ordinary = build_operation_invocation(self.settings, Operation("antenna_hf"))
            self.assertEqual(ordinary.value("QT_QPA_PLATFORM"), "windows")
        for operation in [sweep, Operation("antenna_hf"), Operation("antenna_lf", lf_divisor=88)]:
            with self.subTest(kind=operation.kind), self.assertRaises(ValueError):
                self.runner.run_operation(self.settings, operation, timeout_ms=30001)
        self.runner.run_operation(self.settings, Operation("antenna_lf", sample_count=2, lf_divisor=88))
        self.wait_for(lambda: bool(self.results))
        self.assertEqual(self.results[-1][0], 0)
        self.assertIn("lf tune --iter 2 --divisor 88 --value --verbose", "".join(self.outputs))

    def test_recovery_has_a_bounded_long_timeout_and_preserves_original_artifacts(self):
        original = self.root / "hf-mf-AABBCCDD-key.bin"
        original.write_bytes(bytes(192))
        recovery = Operation("mfc_recover", auth_key="FFFFFFFFFFFF", known_sector=0)
        for timeout in (0, True, MAX_RECOVERY_TIMEOUT_MS + 1):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.runner.run_operation(self.settings, recovery, timeout_ms=timeout)
        with self.assertRaises(ValueError):
            self.runner.run(self.settings, "hw version", timeout_ms=300001)
        with self.assertRaises(ValueError):
            self.runner.run_operation(self.settings, Operation("hf14a_select"), timeout_ms=300001)
        self.runner.run_operation(self.settings, recovery, timeout_ms=MAX_RECOVERY_TIMEOUT_MS)
        self.wait_for(lambda: bool(self.results))
        self.assertEqual(self.results[-1][0], 0)
        self.assertEqual(original.read_bytes(), bytes(192))
        collision = self.root / "hf-mf-AABBCCDD-key-gui-recovery.bin"
        collision.write_bytes(b"original recovered keys")
        with self.assertRaises(ValueError):
            build_operation_invocation(self.settings, recovery)
        self.assertEqual(collision.read_bytes(), b"original recovered keys")

    def test_long_recovery_can_be_cancelled_and_normal_run_continues(self):
        self.configure(delay=10)
        self.runner.run_operation(self.settings, Operation("mfc_recover"), timeout_ms=MAX_RECOVERY_TIMEOUT_MS)
        self.wait_for(lambda: "ARGS=" in "".join(self.outputs))
        self.runner.cancel()
        self.wait_for(lambda: not self.runner.busy)
        self.assertEqual(self.results[-1][0], -2)
        self.configure()
        self.assertEqual(self.run_and_wait()[0], 0)
        self.assertEqual(len(self.results), 2)

    def test_exact_bulk_catalog_uses_separate_flag_and_requires_resync(self):
        supported = [Operation('lf_capture_search_buffer')]
        supported += [Operation('expert_tool', client_command=command) for command in (
            'data hexsamples', 'data samples', 'lf read', 'lf sniff', 'hf texkom reader', 'hf search')]
        supported += [Operation('hf_tool', hf_command=command)
                      for command in ('hf texkom reader', 'hf search')]
        supported += [Operation('lf_tool', lf_command=command) for command in ('lf read', 'lf sniff')]
        self.settings.keep_open = True
        for operation in supported:
            with self.subTest(operation=operation):
                self.assertTrue(operation_supports_bulk_cancel(operation))
                self.assertTrue(operation_requires_resync(operation))
                with patch.object(self.runner._session, 'begin') as begin:
                    self.runner.run_operation(self.settings, operation)
                self.assertTrue(begin.call_args.kwargs['bulk_cancel'])
                self.assertFalse(begin.call_args.kwargs['preserve_tail'])
        with patch.object(self.runner._session, 'begin') as begin:
            self.runner.run(self.settings, 'hf search')
        self.assertTrue(begin.call_args.kwargs['bulk_cancel'])
        for command in ('hw version', 'lf search'):
            with self.subTest(command=command), patch.object(self.runner._session, 'begin') as begin:
                self.runner.run(self.settings, command)
                self.assertFalse(begin.call_args.kwargs['bulk_cancel'])

    def test_bulk_allowlist_does_not_expand_recovery_tail_or_neighboring_commands(self):
        for operation in (
                Operation('lf_search_buffer'), Operation('hf14a_select'),
                Operation('expert_tool', client_command='data load'),
                Operation('expert_tool', client_command='data hexsamples extra'),
                Operation('hf_tool', hf_command='hf 14a reader'),
                Operation('lf_tool', lf_command='lf search')):
            with self.subTest(operation=operation):
                self.assertFalse(operation_supports_bulk_cancel(operation))
                self.assertFalse(operation_requires_resync(operation))
        self.settings.keep_open = True
        with patch.object(self.runner._session, 'begin') as begin:
            self.runner.run_operation(self.settings, Operation('mfc_recover'))
        self.assertTrue(begin.call_args.kwargs['preserve_tail'])
        self.assertFalse(begin.call_args.kwargs['bulk_cancel'])
        with self.assertRaises(ValueError):
            operation_supports_bulk_cancel('hf search')
        with self.assertRaises(ValueError):
            operation_requires_resync(None)

    def test_recovery_keeps_final_key_table_after_the_output_limit(self):
        ending = '[+] Sec | Blk | key A |res| key B |res\n[+] 000 | 003 | FFFFFFFFFFFF | H | ------------ | 0\n'
        self.configure(size=MAX_OUTPUT_BYTES + 300000, ending=ending)
        self.runner.run_operation(self.settings, Operation("mfc_recover"))
        self.wait_for(lambda: bool(self.results))
        output = "".join(self.outputs)
        self.assertEqual(self.results[-1][0], 0)
        self.assertLess(len(output), MAX_OUTPUT_BYTES + RECOVERY_TAIL_CHARACTERS + 1000)
        self.assertIn("Последний фрагмент восстановления", output)
        self.assertEqual(parse_recovered_keys(output), [
            {"sector": 0, "type": "A", "key_hex": "FFFFFFFFFFFF", "method": "hardnested"}])

    def test_old_client_without_incognito(self):
        self.configure(incognito=False)
        code, _ = self.run_and_wait()
        self.assertEqual(code, 0)
        self.assertNotIn("--incognito", "".join(self.outputs))

    def test_incremental_unicode_and_split_ansi(self):
        self.configure(stream=True)
        self.run_and_wait()
        text = "".join(self.outputs)
        self.assertIn("Привет", text)
        self.assertNotIn("\ufffd", text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("32m", text)
        cleaner = _StreamCleaner()
        self.assertEqual(cleaner.feed("ok\x1b]title"), "ok")
        self.assertEqual(cleaner.feed(" hidden\x1b"), "")
        self.assertEqual(cleaner.feed("\\next"), "next")

    def test_nonzero_and_failed_probe(self):
        self.configure(exit=7)
        code, message = self.run_and_wait()
        self.assertEqual(code, 7)
        self.assertIn("кодом 7", message)
        other = ClientRunner(argument_prefix=[str(self.script)])
        results = []
        other.finished.connect(lambda code, text: results.append((code, text)))
        self.configure(help_exit=4)
        other.run(self.settings, "hw version")
        self.wait_for(lambda: bool(results))
        self.assertEqual(results[0][0], 4)
        self.assertIn("Проверка", results[0][1])

    def test_failed_start_and_timeout_validation(self):
        for timeout in (0, -1, 300001, True, 1.5):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.runner.run(self.settings, "hw version", timeout_ms=timeout)
        invalid = self.root / "invalid.exe"
        invalid.write_bytes(b"not an executable")
        self.runner.run(DeviceSettings(str(invalid), "COM1"), "hw version")
        self.wait_for(lambda: bool(self.results))
        self.assertEqual(self.results[0][0], -1)
        self.assertIn("Не удалось запустить", self.results[0][1])
        self.assertFalse(self.runner.busy)

    def test_busy_cancel_and_later_run_survives_old_timers(self):
        self.configure(delay=10)
        self.runner.run(self.settings, "hw version")
        self.wait_for(lambda: "ARGS=" in "".join(self.outputs))
        with self.assertRaises(ValueError):
            self.runner.run(self.settings, "hf search")
        self.runner.cancel()
        self.wait_for(lambda: not self.runner.busy)
        self.assertEqual(self.results[-1][0], -2)
        self.assertIn("отменена", self.results[-1][1])
        self.configure(delay=1.4)
        self.assertEqual(self.run_and_wait()[0], 0)
        self.assertEqual(len(self.results), 2)

    def test_timeout_and_cumulative_output_limit(self):
        self.configure(delay=10)
        code, message = self.run_and_wait(timeout_ms=150)
        self.assertEqual(code, -3)
        self.assertIn("время", message)
        self.configure(size=MAX_OUTPUT_BYTES + 200000)
        self.outputs.clear()
        self.assertEqual(self.run_and_wait()[0], 0)
        self.assertLess(len("".join(self.outputs)), MAX_OUTPUT_BYTES + 200)
        self.assertEqual("".join(self.outputs).count("Вывод сокращён"), 1)

    def test_targeted_recovery_has_short_timeouts_and_stops_repeated_auth2(self):
        operation = Operation("mfc_hardnested_target", classic_size="4k", known_sector=0,
                              auth_key="FFFFFFFFFFFF", target_sector=36)
        with self.assertRaises(ValueError):
            self.runner.run_operation(self.settings, operation, timeout_ms=300001)
        nested = Operation("mfc_nested_target", classic_size="4k", known_sector=0,
                           auth_key="FFFFFFFFFFFF", target_sector=36)
        with self.assertRaises(ValueError):
            self.runner.run_operation(self.settings, nested, timeout_ms=120001)
        error = "[!] AcquireEncryptedNonces: Auth2 error len=1"
        self.configure(lines=[error] * MAX_REPEATED_AUTH2_ERRORS, line_delay=.005, delay=10)
        self.runner.run_operation(self.settings, operation)
        self.wait_for(lambda: bool(self.results))
        self.assertEqual(self.results[-1][0], -4)
        self.assertIn("короткие", self.results[-1][1])
        self.assertIn("профиль", "".join(self.outputs))
        self.assertFalse(self.runner.busy)
        # A subsequent ordinary command survives the old stop/kill timers.
        self.configure(delay=1.4)
        self.assertEqual(self.run_and_wait()[0], 0)

    def test_auth2_guard_is_scoped_and_resets_only_for_actual_progress(self):
        error = "[!] AcquireEncryptedNonces: Auth2 error len=1"
        self.configure(lines=[error] * 20)
        self.assertEqual(self.run_and_wait()[0], 0)
        operation = Operation("mfc_hardnested_target", known_sector=0,
                              auth_key="FFFFFFFFFFFF", target_sector=1)
        self.configure(lines=[error] * 11 + ["[=] 2 | 100 | Acquiring nonces |"] + [error] * 11
                       + ["[=] Brute force completed. Key found: A0A1A2A3A4A5"] + [error] * 11
                       + [error.replace("len=1", "len=16")] * 20)
        initial = len(self.results)
        self.runner.run_operation(self.settings, operation)
        self.wait_for(lambda: len(self.results) > initial)
        self.assertEqual(self.results[-1][0], 0)
        # Reprinting the same nonce count is not new progress.
        self.configure(lines=["[=] 1 | 100 | Acquiring nonces |"] + [error] * 6
                       + ["[=] 2 | 100 | Acquiring nonces |"] + [error] * 6, delay=10)
        initial = len(self.results)
        self.runner.run_operation(self.settings, operation)
        self.wait_for(lambda: len(self.results) > initial)
        self.assertEqual(self.results[-1][0], -4)

    def test_shutdown_waits_for_one_shot_child_and_blocks_reentrant_restart(self):
        # Use the actual interpreter, not Windows venv's redirector child.
        self.settings.client_path = getattr(sys, '_base_executable', sys.executable)
        self.configure(delay=10)
        self.runner.run(self.settings, 'hw version')
        self.wait_for(lambda: 'ARGS=' in ''.join(self.outputs))
        child_exited, refused = [], []
        self.runner._active.process.finished.connect(lambda *_: child_exited.append(True))

        def restart_on_finish(*_):
            try:
                self.runner.run(self.settings, 'hf search')
            except ValueError:
                refused.append(True)

        self.runner.finished.connect(restart_on_finish)
        self.runner.shutdown()
        self.runner.finished.disconnect(restart_on_finish)
        self.assertEqual(child_exited, [True])
        self.assertEqual(refused, [True])
        self.assertFalse(self.runner.busy)
        self.assertEqual([code for code, _ in self.results], [-2])
        self.configure()
        self.assertEqual(self.run_and_wait()[0], 0)
        self.assertEqual(len(self.results), 2)


if __name__ == "__main__":
    unittest.main()
