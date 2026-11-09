"""Strict read-only connection checks shared by keepalive and RF cleanup."""
import json
import re
import time
from PySide6.QtCore import QTimer
from .device import _StreamCleaner, parse_connection_status
from .i18n import tr

CLEANUP_VERSION_MAX_PROBES = 8
CLEANUP_VERSION_RECHECK_DELAY_MS = 250


_PROBE_ERROR = re.compile(
    r'(?:OFFLINE\s+mode|UART\s+CreateFile\s+failed|'
    r'invalid serial port|No response from Proxmark3|cannot communicate|'
    r'communication error|connection (?:lost|closed)|received packet.*payload too short|'
    r'resynchronising|Capabilities structure version.*not the one expected)'
    r'|^\s*(?:\[[^\]\n]+\]\s*)?ERROR\b'
    r'|^\s*\[(?:!+|-)\][^\n]*\b(?:error|failed|failure|timeout|aborted|cancelled|lost|closed)\b',
    re.I | re.M)


def version_probe_status(output, expected_transport, code=0, *, verified_transport=None):
    """Classify one response; only a silent Blue Shark miss may recheck."""
    result = parse_connection_status(output, expected_transport, code,
                                     verified_transport=verified_transport)
    text = _StreamCleaner().feed(output.replace('\r\n','\n').replace('\r','\n'))
    diagnostic_error = bool(_PROBE_ERROR.search(text))
    if diagnostic_error and result['connected']:
        result.update(connected=False, message=tr('Клиент не подтвердил рабочее соединение с устройством.'))
    result['read_only_recheck_allowed'] = (expected_transport == 'blueshark' and code == 0
        and not diagnostic_error and result['firmware'] is None and result['transport'] == 'blueshark')
    return result


def record_cleanup_version_check(job, output, code, result):
    """Retain both replies independently of the primary operation's report."""
    checks = job.setdefault('cleanup_version_checks', [])
    checks.append({'attempt': job.get('cleanup_version_attempt', 1), 'exit_code': code,
                   'probe_number': job.get('cleanup_version_probe_count', 1),
                   'recheck': bool(job.get('cleanup_version_recheck_used')), 'connected': result['connected'],
                   'output': output[:65536], 'output_truncated': len(output) > 65536})
    try:
        temporary = job['session']/'cleanup-version-checks.tmp'
        temporary.write_text(json.dumps({'version': 1, 'checks': checks}, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        temporary.replace(job['session']/'cleanup-version-checks.json')
    except OSError:
        job['log_warning'] = tr('Не удалось обновить отчёт.')


def stop_cleanup_version_checks(job):
    """Invalidate even a timeout callback that Qt has already queued."""
    job['cleanup_version_generation'] = job.get('cleanup_version_generation', 0)+1
    timer = job.pop('cleanup_version_timer', None)
    if timer is not None:
        timer.stop();timer.deleteLater()


def schedule_cleanup_version_check(owner, job, stage, on_failure):
    """Poll only liveness, never repeat an RF operation or a write.

    A missing OS reply is not successful cleanup. The caller retains its lock
    until a fresh reply or explicit uncertain state, within one total deadline.
    """
    if job.get('cleanup_version_timer') is not None:return True
    remaining = int((job.get('cleanup_version_deadline', 0)-time.monotonic())*1000)
    if (job.get('cleanup_version_probe_count', 1) >= CLEANUP_VERSION_MAX_PROBES
            or remaining <= CLEANUP_VERSION_RECHECK_DELAY_MS):
        return False
    generation = job.get('cleanup_version_generation', 0)
    timer = QTimer(owner);timer.setSingleShot(True);job['cleanup_version_timer'] = timer
    job['cleanup_version_recheck_used'] = True
    def dispatch():
        if job.get('cleanup_version_timer') is not timer:
            timer.deleteLater();return
        job.pop('cleanup_version_timer')
        timer.deleteLater()
        if (owner.read_job is not job or job.get('cleanup_version_generation', 0) != generation
                or job.get('stage') != stage):return
        budget = int((job.get('cleanup_version_deadline', 0)-time.monotonic())*1000)
        if budget <= 0 or job.get('cleanup_version_probe_count', 1) >= CLEANUP_VERSION_MAX_PROBES:
            on_failure(tr('Остановка не подтверждена. Проверьте соединение и повторите «Остановить».'));return
        job['cleanup_version_probe_count'] = job.get('cleanup_version_probe_count', 1)+1
        try:owner._start_step('hw version', budget)
        except (ValueError, OSError) as exc:on_failure(str(exc))
    timer.timeout.connect(dispatch);timer.start(CLEANUP_VERSION_RECHECK_DELAY_MS)
    return True
