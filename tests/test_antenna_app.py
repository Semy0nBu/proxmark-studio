"""Exercise antenna GUI workflows without opening a serial port."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import PropertyMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from pm3gui.app import MainWindow
from pm3gui.operations import OPERATION_TIMEOUT_MS


def finite_output(band="HF", frequency_khz=125.0, samples=(10000, 12000)):
    header = ("[=] Measuring HF antenna\n" if band == "HF"
              else f"[=] Measuring LF antenna at {frequency_khz:.2f} kHz\n")
    initial_max = 65 if band == "HF" else 71
    rows = [f"[=] 0 mV / 0 V / {initial_max} Vmax   \r"]
    for index, value in enumerate(samples):
        rows.append(f"[=] {value} mV / {value // 1000} V / {max(samples[:index + 1]) // 1000} Vmax   \r")
    return (header + "".join(rows)
            + f"[=] Min....... {min(samples)} mV\n"
            + f"[=] Max....... {max(samples)} mV\n"
            + f"[=] Average... {sum(samples) / len(samples):.3f} mV\n"
            + "[=] Done!\n")


def sweep_output(path):
    return ("[=] -------- LF Antenna ----------\n"
            "[+] 125.00 kHz ........... 64.00 V\n"
            "[+] 134.83 kHz ........... 45.05 V\n"
            "[+] 125.00 kHz optimal.... 64.00 V\n"
            "[+] Approx. Q factor measurement\n"
            "[+] Frequency bandwidth... 12.0\n"
            "[+] Peak voltage.......... 11.2\n"
            "[+] LF antenna............ ok\n"
            "[=] -------- HF Antenna ----------\n"
            "[+] 13.56 MHz............. 44.00 V\n"
            "[+] Approx. Q factor measurement\n"
            "[+] Peak voltage.......... 7.7\n"
            "[+] HF antenna ( ok )\n"
            "[=] Q factor must be measured without tag on the antenna\n"
            f"[+] Saved 256 bytes to PM3 file `{path.resolve()}`\n")


class AntennaAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run_client = self.enterContext(patch("pm3gui.app.ClientRunner.run"))
        self.run_client.side_effect = lambda *args, **kwargs: self.window._busy_changed(True)
        self.run_operation = self.enterContext(patch("pm3gui.app.ClientRunner.run_operation"))
        self.run_operation.side_effect = self.run_typed_operation
        self.cancel_client = self.enterContext(patch("pm3gui.app.ClientRunner.cancel"))
        self.warning = self.enterContext(patch("pm3gui.app.QMessageBox.warning"))
        self.enterContext(patch("pm3gui.app.QMessageBox.question", return_value=QMessageBox.StandardButton.Cancel))
        self.enterContext(patch("pm3gui.app.available_ports", return_value=[]))
        self.enterContext(patch("pm3gui.app.discover_client", return_value=""))
        self.window = MainWindow(self.root / "library")

    def run_typed_operation(self, settings, operation, **kwargs):
        # Keep the real runner's validation even though no QProcess is created.
        timeout = kwargs.get("timeout_ms", operation.recommended_timeout_ms)
        self.window.runner._validate_start(timeout, OPERATION_TIMEOUT_MS.get(operation.kind, 300000))
        self.window._busy_changed(True)
        return operation.render()

    def tearDown(self):
        self.window.dirty = False
        self.window.read_job = None
        self.window.antenna_job = None
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.application.processEvents()
        self.temp.cleanup()

    def select_mode(self, value):
        index = self.window.antenna_mode.findData(value)
        self.assertGreaterEqual(index, 0, f"Missing antenna profile {value!r}")
        self.window.antenna_mode.setCurrentIndex(index)

    def finish_step(self, output="", code=0):
        self.window.operation_output = output
        self.window._busy_changed(False)
        self.window._finished(code, "Synthetic client result")

    def assert_cleanup_then_finish(self):
        self.assertEqual(self.run_operation.call_args.args[1].kind, "antenna_field_off")
        self.finish_step("[+] FPGA switched off\n")
        self.assertEqual(self.run_client.call_args.args[1], "hw version")
        self.finish_step("[ ARM ]\n  OS......... Iceman/test-build\n")

    def save_good_measurement(self, samples=(10000, 12000)):
        self.select_mode("hf")
        self.window.antenna_samples.setValue(len(samples))
        self.window.start_antenna_measurement()
        self.finish_step(finite_output(samples=samples))
        self.assert_cleanup_then_finish()
        self.assertIsNone(self.window.antenna_job)
        self.assertIsNotNone(self.window.antenna_last_result)
        return copy.deepcopy(self.window.antenna_last_result)

    def test_navigation_and_defaults_do_not_contact_hardware(self):
        self.assertEqual(self.window.stack.count(), 6)
        self.window.navigate(3)
        self.assertEqual(self.window.stack.currentIndex(), 3)
        for mode in ("hf", 95, 88, "custom"):
            self.assertGreaterEqual(self.window.antenna_mode.findData(mode), 0)
        self.assertEqual(self.window.antenna_samples.value(), 5)
        self.assertEqual((self.window.antenna_samples.minimum(), self.window.antenna_samples.maximum()), (1, 20))
        self.assertEqual((self.window.antenna_frequency.minimum(), self.window.antenna_frequency.maximum()), (47, 600))
        self.assertFalse(self.window.antenna_repeat.isChecked())
        self.assertIsNone(self.window.antenna_job)
        self.run_client.assert_not_called()
        self.run_operation.assert_not_called()

    def test_bluetooth_report_records_the_job_profile_not_edited_controls(self):
        self.window.transport_combo.setCurrentIndex(self.window.transport_combo.findData('blueshark'))
        self.window.ports.setEditText('COM42')
        self.window.client_path.setText('original-client.exe')
        self.select_mode('hf')
        self.window.antenna_samples.setValue(2)
        self.window.start_antenna_measurement()
        self.window.ports.setEditText('COM3')
        self.window.client_path.setText('changed-client.exe')
        self.finish_step(finite_output())
        self.assert_cleanup_then_finish()
        report = self.window.antenna_last_result
        self.assertEqual((report['transport'], report['port'], report['client_path']),
                         ('blueshark', 'COM42', 'original-client.exe'))
        for call in self.run_operation.call_args_list + self.run_client.call_args_list:
            self.assertEqual((call.args[0].transport, call.args[0].port), ('blueshark', 'COM42'))

    def test_fixed_and_custom_profiles_use_bounded_typed_operations(self):
        for mode, kind, divisor, frequency in (
            ("hf", "antenna_hf", None, None),
            (95, "antenna_lf", 95, 125.0),
            (88, "antenna_lf", 88, 12000 / 89),
            ("custom", "antenna_lf", 59, 200.0),
        ):
            with self.subTest(mode=mode):
                self.select_mode(mode)
                self.window.antenna_frequency.setValue(200)
                self.window.antenna_samples.setValue(2)
                self.window.start_antenna_measurement()
                operation = self.run_operation.call_args.args[1]
                self.assertEqual(operation.kind, kind)
                self.assertEqual(operation.sample_count, 2)
                if divisor is not None:
                    self.assertEqual(operation.lf_divisor, divisor)
                output = finite_output("HF" if mode == "hf" else "LF", frequency or 125)
                self.finish_step(output)
                self.assert_cleanup_then_finish()
                self.assertIsNone(self.window.antenna_job)
                self.assertEqual(self.window.antenna_last_result["measurement"]["sample_count"], 2)

    def test_measurement_and_baseline_commit_only_after_field_off_and_version(self):
        self.select_mode("hf")
        self.window.antenna_samples.setValue(2)
        self.window.antenna_measure_button.click()
        self.finish_step(finite_output())
        self.assertIsNone(self.window.antenna_last_result)
        self.assertFalse(self.window.antenna_baseline_button.isEnabled())
        self.assertEqual(self.run_operation.call_args.args[1].kind, "antenna_field_off")
        self.finish_step("[+] FPGA switched off\n")
        self.assertIsNone(self.window.antenna_last_result)
        self.finish_step("[ ARM ]\n  OS......... Iceman/test-build\n")
        result = self.window.antenna_last_result
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["measurement"]["samples_v"], [10.0, 12.0])
        self.assertEqual(result["measurement"]["average_v"], 11.0)
        self.assertTrue(result["created_at"])
        self.assertIsNone(self.window.antenna_job)
        self.assertTrue(self.window.antenna_baseline_button.isEnabled())
        self.window.antenna_baseline_button.click()
        saved = json.loads((self.window.data_dir / "antenna-baseline.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, result)
        self.assertFalse(self.window.antenna_plot.grab().isNull())

    def test_partial_or_failed_measurement_preserves_last_good_and_baseline(self):
        previous = self.save_good_measurement()
        self.window.save_antenna_baseline()
        baseline_path = self.window.data_dir / "antenna-baseline.json"
        baseline = baseline_path.read_bytes()
        for output, code in ((finite_output().replace("[=] Done!\n", ""), 0),
                             (finite_output() + "[!] timeout while waiting for Proxmark HF measure, aborting\n", 0),
                             (finite_output(), -3)):
            with self.subTest(code=code, partial="Done!" not in output):
                self.window.start_antenna_measurement()
                self.finish_step(output, code)
                self.assert_cleanup_then_finish()
                self.assertEqual(self.window.antenna_last_result, previous)
                self.assertEqual(baseline_path.read_bytes(), baseline)
                self.assertIsNone(self.window.antenna_job)

    def test_failed_version_check_does_not_commit_even_a_valid_measurement(self):
        previous = self.save_good_measurement()
        self.window.start_antenna_measurement()
        self.finish_step(finite_output(samples=(20000, 22000)))
        self.assertEqual(self.run_operation.call_args.args[1].kind, "antenna_field_off")
        self.finish_step("[+] FPGA switched off\n")
        self.finish_step("[!] Connection timed out\n", -3)
        self.assertEqual(self.window.antenna_last_result, previous)
        self.assertIsNone(self.window.antenna_job)

    def test_sweep_imports_the_actual_lf_curve_after_cleanup(self):
        self.window.antenna_sweep_button.click()
        operation = self.run_operation.call_args.args[1]
        self.assertEqual(operation.kind, "antenna_sweep")
        graph = operation.output_prefix.with_suffix(".pm3")
        values = [-128] * 256
        values[95], values[88] = -3, -40
        graph.write_text("\n".join(map(str, values)) + "\n", encoding="ascii")
        self.finish_step(sweep_output(graph))
        self.assertIsNone(self.window.antenna_last_result)
        self.assert_cleanup_then_finish()
        result = self.window.antenna_last_result["measurement"]
        self.assertEqual(result["band"], "both")
        self.assertEqual((result["lf_peak_khz"], result["lf_peak_v"], result["hf_v"]), (125.0, 64.0, 44.0))
        self.assertEqual(result["lf_q_bandwidth"], 12.0)
        self.assertEqual(result["lf_q_voltage"], 11.2)
        self.assertEqual(result["hf_q_voltage"], 7.7)
        self.assertEqual(len(result["lf_curve"]), 237)
        at_125 = next(point for point in result["lf_curve"] if point["divisor"] == 95)
        self.assertEqual(at_125["voltage_v"], 64.0)
        self.assertFalse(self.window.antenna_plot.grab().isNull())
        self.assertIsNone(self.window.antenna_job)

    def test_invalid_sweep_file_cannot_replace_previous_result(self):
        previous = self.save_good_measurement()
        self.window.start_antenna_measurement(sweep=True)
        operation = self.run_operation.call_args.args[1]
        graph = operation.output_prefix.with_suffix(".pm3")
        graph.write_text("-128\n" * 255, encoding="ascii")
        self.finish_step(sweep_output(graph))
        self.assert_cleanup_then_finish()
        self.assertEqual(self.window.antenna_last_result, previous)
        self.assertIsNone(self.window.antenna_job)

    def test_cancelled_run_ignores_late_success_and_still_switches_field_off(self):
        previous = self.save_good_measurement()
        self.window.start_antenna_measurement()
        with patch("pm3gui.app.ClientRunner.busy", new_callable=PropertyMock, return_value=True):
            self.window.cancel_antenna_measurement()
        self.cancel_client.assert_called_once()
        self.finish_step(finite_output(samples=(20000, 22000)), -2)
        self.assert_cleanup_then_finish()
        self.assertEqual(self.window.antenna_last_result, previous)
        self.assertIsNone(self.window.antenna_job)

    def test_repeat_wait_retains_device_mutex_and_can_start_next_finite_batch(self):
        self.select_mode("hf")
        self.window.antenna_samples.setValue(2)
        self.window.antenna_repeat.setChecked(True)
        self.window.start_antenna_measurement()
        self.finish_step(finite_output())
        self.assert_cleanup_then_finish()
        self.assertIsNotNone(self.window.antenna_job)
        self.assertTrue(self.window.antenna_stop_button.isEnabled())
        before = (self.run_client.call_count, self.run_operation.call_count)
        self.window.read_card()
        self.window.run_command("hf search", 90000)
        self.assertEqual((self.run_client.call_count, self.run_operation.call_count), before)
        self.assertIsNone(self.window.read_job)
        self.window._run_antenna_batch()
        self.assertEqual(self.run_operation.call_args.args[1].kind, "antenna_hf")
        self.assertEqual(self.run_operation.call_args.args[1].sample_count, 2)
        self.assertEqual(sum(call.args[1].kind == "antenna_hf" for call in self.run_operation.call_args_list), 2)

    def test_repeat_deadline_stops_without_starting_another_batch(self):
        self.select_mode("hf")
        self.window.antenna_samples.setValue(2)
        self.window.antenna_repeat.setChecked(True)
        with patch("pm3gui.antenna.time.monotonic", return_value=100):
            self.window.start_antenna_measurement()
            self.finish_step(finite_output())
            self.assert_cleanup_then_finish()
        self.assertEqual(self.window.antenna_job["stage"], "wait")
        previous = copy.deepcopy(self.window.antenna_last_result)
        before = (self.run_client.call_count, self.run_operation.call_count)
        with patch("pm3gui.antenna.time.monotonic", return_value=221):
            self.window._run_antenna_batch()
        self.assertEqual((self.run_client.call_count, self.run_operation.call_count), before)
        self.assertEqual(self.window.antenna_last_result, previous)
        self.assertIsNone(self.window.antenna_job)
        self.assertFalse(self.window.antenna_timer.isActive())
        self.assertTrue(self.window.antenna_measure_button.isEnabled())
        self.assertFalse(self.window.antenna_stop_button.isEnabled())

    def test_cancel_during_repeat_wait_releases_mutex_without_an_extra_command(self):
        self.select_mode("hf")
        self.window.antenna_samples.setValue(2)
        self.window.antenna_repeat.setChecked(True)
        self.window.start_antenna_measurement()
        self.finish_step(finite_output())
        self.assert_cleanup_then_finish()
        self.assertEqual(self.window.antenna_job["stage"], "wait")
        previous = copy.deepcopy(self.window.antenna_last_result)
        before = (self.run_client.call_count, self.run_operation.call_count)
        self.window.antenna_stop_button.click()
        self.assertEqual((self.run_client.call_count, self.run_operation.call_count), before)
        self.cancel_client.assert_not_called()
        self.assertEqual(self.window.antenna_last_result, previous)
        self.assertIsNone(self.window.antenna_job)
        self.assertFalse(self.window.antenna_timer.isActive())
        self.window.read_card()
        self.assertIsNotNone(self.window.read_job)

    def test_card_job_prevents_starting_antenna_measurement(self):
        self.window.read_card()
        self.assertIsNotNone(self.window.read_job)
        before = (self.run_client.call_count, self.run_operation.call_count)
        self.window.start_antenna_measurement()
        self.assertEqual((self.run_client.call_count, self.run_operation.call_count), before)
        self.assertIsNone(self.window.antenna_job)


if __name__ == "__main__":
    unittest.main()
