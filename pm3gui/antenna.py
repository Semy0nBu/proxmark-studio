"""Small native plot for measured antenna curves and voltage history."""
from __future__ import annotations
from .i18n import tr

import math
import json
import re
import time
from datetime import datetime, timezone

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from .operations import Operation, parse_antenna_measurement
from .theme import COLORS


class AntennaControllerMixin:
    """Serializes measurement, field-off and device confirmation for the window."""
    def start_antenna_measurement(self, checked=False, *, sweep=False):
        if self.runner.busy or self.read_job is not None or self.antenna_job is not None or getattr(self, 'firmware_busy', False):
            self.error(tr('Дождитесь завершения текущей операции.'))
            return
        try:
            self.current_session = self._new_session()
            if sweep:
                operation = Operation('antenna_sweep', output_prefix=self.current_session / 'lf-sweep')
            else:
                profile = self.antenna_mode.currentData()
                if profile == 'hf':
                    operation = Operation('antenna_hf', sample_count=self.antenna_samples.value())
                else:
                    divisor = profile if isinstance(profile, int) else round(12000 / self.antenna_frequency.value() - 1)
                    operation = Operation('antenna_lf', sample_count=self.antenna_samples.value(), lf_divisor=divisor)
            operation.render()
            self.antenna_series = []
            self.antenna_job = {'stage': 'measure', 'operation': operation,
                                'session': self.current_session, 'step_number': 0,
                                'repeat': self.antenna_repeat.isChecked() and not sweep,
                                'deadline': time.monotonic()+120, 'started_at': time.monotonic(),
                                'cancel_requested': False, 'error': '', 'pending_result': None}
            self._run_antenna_batch()
        except (OSError, ValueError) as exc:
            self.antenna_job = None
            self._busy_changed(False)
            self.antenna_status.setText(str(exc))
            self.error(exc)

    def _run_antenna_batch(self):
        job = self.antenna_job
        if job is None:
            return
        if job['cancel_requested'] or (job['stage'] == 'wait' and time.monotonic() >= job['deadline']):
            self._end_antenna_job()
            return
        job['stage'] = 'measure'
        job['pending_result'] = None
        self._start_step(job['operation'], 30000)
        self._busy_changed(self.runner.busy)
        self.antenna_status.setText(tr('Измеряем антенну… Затем поле будет выключено и связь проверена.'))

    def cancel_antenna_measurement(self):
        job = self.antenna_job
        if job is None:
            return
        job['cancel_requested'] = True
        job['repeat'] = False
        self.antenna_timer.stop()
        if job['stage'] in {'off', 'verify'}:
            return
        if job['stage'] == 'measure':
            self.runner.cancel()
        elif job['stage'] == 'wait':
            self._end_antenna_job()
        else:
            self._antenna_cleanup()

    def _antenna_cleanup(self, error=''):
        job = self.antenna_job
        if job is None:
            return
        if error:
            job['error'] = error
        self.antenna_timer.stop()
        job['stage'] = 'off'
        self._start_step(Operation('antenna_field_off'), 8000)
        self.antenna_status.setText(tr('Выключаем поле и проверяем связь с платой…'))
        self._busy_changed(self.runner.busy)

    def _advance_antenna(self, output, code):
        job = self.antenna_job
        if job['stage'] == 'measure':
            if not job['cancel_requested']:
                try:
                    job['pending_result'] = parse_antenna_measurement(output, job['operation'], returncode=code)
                except (OSError, ValueError) as exc:
                    job['error'] = str(exc)
            self._antenna_cleanup()
            return
        if job['stage'] == 'off':
            if code:
                job['error'] = job['error'] or tr('Клиент не подтвердил команду выключения поля.')
            job['stage'] = 'verify'
            self._start_step('hw version', 8000)
            return
        if job['stage'] != 'verify':
            raise ValueError(tr('Неожиданный этап измерения антенны.'))
        if code or not re.search(r'\bOS(?:\.{2,}|[ \t]*:)[ \t]+\S+', output, re.I):
            job['error'] = tr('Не удалось подтвердить выход платы из измерения. Проверьте соединение и нажмите кнопку платы при необходимости.')
        if not job['error'] and not job['cancel_requested'] and job['pending_result'] is not None:
            profile = job.get('device_profile', self._device_profile())
            envelope = {'version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
                        'measurement': job['pending_result'], 'note': self.antenna_note.text().strip(),
                        'client_path': profile['client_path'], 'port': profile['port'],
                        'transport': profile['transport'], 'session': str(job['session'])}
            report = job['session'] / f"antenna-{job['step_number']:03d}.json"
            report.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding='utf-8')
            self.antenna_last_result = envelope
            self._show_antenna_measurement(envelope)
        if job['repeat'] and not job['error'] and not job['cancel_requested'] and time.monotonic() < job['deadline']:
            job['stage'] = 'wait'
            self._busy_changed(self.runner.busy)
            self.antenna_status.setText(tr('Серия сохранена. Следующий замер через секунду; наблюдение можно остановить.'))
            self.antenna_timer.start(1000)
            return
        self._end_antenna_job()

    def _end_antenna_job(self):
        job = self.antenna_job
        self.antenna_timer.stop()
        self.antenna_job = None
        self._busy_changed(self.runner.busy)
        if job['error']:
            message = tr('Измерение не завершено: ') + job['error']
        elif job['cancel_requested']:
            message = tr('Наблюдение остановлено. Поле выключено; плата отвечает.')
        else:
            message = tr('Измерение завершено. Поле выключено; плата отвечает. Результат сохранён в папке сеанса.')
        self.antenna_status.setText(message)
        self.device_info.setText(message)
        self.statusBar().showMessage(message, 15000)

    def _show_antenna_measurement(self, envelope):
        measurement = envelope['measurement']
        for key, widget in self.antenna_metrics.items():
            value = measurement.get(key)
            widget.setText(tr('{0} В', f'{value:.2f}') if value is not None else '—')
        details = []
        if measurement.get('lf_peak_khz') is not None:
            details.append(tr('Пик LF: {0} кГц · {1} В', f"{measurement['lf_peak_khz']:.2f}", f"{measurement['lf_peak_v']:.2f}"))
        for key, title in [('lf_q_bandwidth', tr('Q LF (полоса)')), ('lf_q_voltage', tr('Q LF (напряжение)')), ('hf_q_voltage', 'Q HF')]:
            if measurement.get(key) is not None:
                details.append(f"{title}: {measurement[key]:.1f}")
        if measurement.get('average_v') is not None:
            details.append(tr('Среднее: {0} В · диапазон {1}–{2} В', f"{measurement['average_v']:.2f}", f"{measurement['min_v']:.2f}", f"{measurement['max_v']:.2f}"))
            if measurement['band'] == 'lf':
                details.append(tr('Фактически: {0} кГц', f"{measurement['frequency_khz']:.2f}"))
        translations = {'ok': tr('норма'), 'marginal': tr('понижено'), 'unusable': tr('очень низко')}
        for key, title in [('lf_status', 'LF'), ('hf_status', 'HF')]:
            if measurement.get(key):
                details.append(tr('Оценка клиента {0}: {1}', f'{title}', f'{translations.get(measurement[key], measurement[key])}'))
        details.extend(measurement.get('warnings', []))
        self.antenna_details.setText(' · '.join(details))
        curve = measurement.get('lf_curve')
        if curve:
            self.antenna_plot.set_points(curve)
            self.antenna_full_range.setEnabled(True)
        elif measurement.get('average_v') is not None:
            self.antenna_series.append({'time_seconds': time.monotonic()-self.antenna_job['started_at'],
                                        'voltage_v': measurement['average_v']})
            self.antenna_plot.set_points(self.antenna_series[-120:], kind='time')
            self.antenna_full_range.setEnabled(False)
        else:
            self.antenna_plot.set_points([])
        self._show_antenna_baseline()

    def _show_antenna_baseline(self):
        if not self.antenna_baseline:
            return
        differences = []
        reference = self.antenna_baseline['measurement']
        current = self.antenna_last_result['measurement'] if self.antenna_last_result else {}
        for key, title in [('hf_v', 'HF'), ('lf125_v', 'LF 125'), ('lf134_v', 'LF 134,83')]:
            first, second = current.get(key), reference.get(key)
            if isinstance(first, (int, float)) and isinstance(second, (int, float)) and math.isfinite(first) and math.isfinite(second):
                differences.append(tr('{0}: {1} В', f'{title}', f'{first - second:+.2f}'))
        if (not differences and current.get('band') == reference.get('band') == 'lf'
                and current.get('frequency_khz') == reference.get('frequency_khz')):
            first, second = current.get('average_v'), reference.get('average_v')
            if isinstance(first, (int, float)) and isinstance(second, (int, float)) and math.isfinite(first) and math.isfinite(second):
                differences.append(tr('LF {0}: {1} В', f"{current['frequency_khz']:.2f}", f'{first - second:+.2f}'))
        self.antenna_baseline_label.setText(tr('К эталону: ') + ' · '.join(differences) if differences else tr('Эталон сохранён для другого режима.'))

    def save_antenna_baseline(self):
        if self.antenna_last_result is None or self.antenna_job is not None:
            return
        try:
            envelope = {**self.antenna_last_result, 'note': self.antenna_note.text().strip()}
            text = json.dumps(envelope, ensure_ascii=False, indent=2)
            (self.data_dir / 'antenna-baseline.json').write_text(text, encoding='utf-8')
            self.antenna_baseline = json.loads(text)
            self._show_antenna_baseline()
        except (OSError, ValueError) as exc:
            self.error(exc)


class AntennaPlot(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.points = []
        self.kind = 'frequency'
        self.full_range = False
        self._hover = None
        self.setMinimumHeight(230)
        self.setMouseTracking(True)

    def set_points(self, points, *, kind='frequency'):
        x_key = 'frequency_khz' if kind == 'frequency' else 'time_seconds'
        self.points = sorted([(float(point[x_key]), float(point['voltage_v']))
                              for point in points
                              if math.isfinite(float(point[x_key])) and math.isfinite(float(point['voltage_v']))])
        self.kind = kind
        self._hover = None
        self.update()

    def set_full_range(self, enabled):
        self.full_range = bool(enabled)
        self.update()

    def _bounds(self):
        if self.kind == 'frequency' and not self.full_range:
            low, high = 80., 180.
        elif self.points:
            low, high = self.points[0][0], self.points[-1][0]
            if low == high:
                low, high = low - .5, high + .5
        else:
            low, high = 0., 1.
        visible = [voltage for frequency, voltage in self.points if low <= frequency <= high]
        top = max(visible, default=1.) * 1.15
        return low, high, max(top, 1.)

    def _rect(self):
        return QRectF(54, 27, max(10, self.width()-76), max(10, self.height()-68))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(COLORS['inset']))
        plot = self._rect()
        low, high, top = self._bounds()
        painter.setPen(QColor(COLORS['muted']))
        title = tr('LF: напряжение по частоте (приблизительно)') if self.kind == 'frequency' else tr('Напряжение во времени')
        painter.drawText(12, 18, title)
        for index in range(5):
            y = plot.bottom() - plot.height()*index/4
            painter.setPen(QPen(QColor('#383838'), 1))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(QColor(COLORS['muted']))
            painter.drawText(QRectF(0, y-9, 47, 18), Qt.AlignmentFlag.AlignRight, f'{top*index/4:.1f}')
            x = plot.left() + plot.width()*index/4
            painter.drawText(QRectF(x-35, plot.bottom()+5, 70, 18), Qt.AlignmentFlag.AlignCenter,
                             f'{low+(high-low)*index/4:.1f}')
        painter.drawText(QRectF(plot.left(), self.height()-18, plot.width(), 16), Qt.AlignmentFlag.AlignCenter,
                         tr('кГц') if self.kind == 'frequency' else tr('секунды'))
        painter.drawText(8, 38, tr('В'))

        def position(point):
            return QPointF(plot.left()+(point[0]-low)/(high-low)*plot.width(),
                           plot.bottom()-point[1]/top*plot.height())

        if self.kind == 'frequency':
            for frequency, color in [(125., '#eebc68'), (12000/89, '#82b5ff')]:
                if low <= frequency <= high:
                    x = position((frequency, 0)).x()
                    painter.setPen(QPen(QColor(color), 1, Qt.PenStyle.DashLine))
                    painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
        visible = [point for point in self.points if low <= point[0] <= high]
        painter.save()
        painter.setClipRect(plot.adjusted(-2, -2, 2, 2))
        painter.setPen(QPen(QColor(COLORS['accent']), 2))
        if visible:
            path = QPainterPath(position(visible[0]))
            for point in visible[1:]:
                path.lineTo(position(point))
            painter.drawPath(path)
            if len(visible) == 1:
                painter.drawEllipse(position(visible[0]), 3, 3)
        painter.restore()
        if not visible:
            painter.setPen(QColor(COLORS['muted']))
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, tr('Здесь появятся реальные измерения'))
        elif self._hover is not None:
            point = min(visible, key=lambda point: abs(position(point).x()-self._hover))
            painter.setPen(QPen(QColor('#ffffff'), 1))
            painter.drawEllipse(position(point), 4, 4)
            unit = tr('кГц') if self.kind == 'frequency' else tr('с')
            self.setToolTip(tr('{0} {1} · {2} В', f'{point[0]:.2f}', f'{unit}', f'{point[1]:.2f}'))

    def mouseMoveEvent(self, event):
        self._hover = event.position().x()
        self.update()

    def leaveEvent(self, event):
        self._hover = None
        self.update()
