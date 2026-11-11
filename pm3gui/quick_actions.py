"""Profile-local named client actions, with no automatic execution on load."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .client_catalog import split_command, uses_rf
from .studio_data import say

MAX_ACTIONS = 100
MAX_STEPS = 20
MAX_FILE_BYTES = 1024 * 1024
_UNSPECIFIED = object()


def actions_revision(path):
    path = Path(path)
    if not path.exists():return None
    with path.open('rb') as stream:raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:raise ValueError('Quick actions file is too large.')
    return hashlib.sha256(raw).hexdigest()


@contextmanager
def _save_lock(path):
    # The OS releases this advisory lock if the application exits or crashes.
    # Keep a small sidecar, avoiding a stale exclusive-create lock after reboot.
    with Path(str(path)+'.lock').open('a+b') as stream:
        stream.seek(0, 2)
        if not stream.tell():stream.write(b'0'); stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:yield
        finally:
            stream.seek(0)
            if os.name == 'nt':msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class QuickAction:
    name: str
    lines: tuple[str, ...]
    timeout_seconds: int = 120
    offline: bool = False

    def validated(self):
        if (not isinstance(self.name, str) or not self.name.strip()
                or len(self.name) > 80 or any(ord(c) < 32 for c in self.name)):
            raise ValueError(say('Название должно содержать 1–80 символов.', 'Use a name of 1–80 characters.'))
        if (not isinstance(self.lines, (tuple, list)) or not 1 <= len(self.lines) <= MAX_STEPS
                or any(not isinstance(line, str) for line in self.lines)):
            raise ValueError(say('Добавьте от 1 до 20 команд.', 'Add 1–20 commands.'))
        if type(self.timeout_seconds) is not int or not 5 <= self.timeout_seconds <= 1800:
            raise ValueError(say('Время команды: от 5 до 1800 секунд.', 'Command timeout: 5–1800 seconds.'))
        if type(self.offline) is not bool:
            raise ValueError('Invalid offline setting.')
        lines = []
        for line in self.lines:
            command, args = split_command(line)
            if self.offline and uses_rf(command):
                raise ValueError(say('RF-команды требуют режима устройства.', 'RF commands require device mode.'))
            lines.append(command + (' ' + args if args else ''))
        return QuickAction(self.name.strip(), tuple(lines), self.timeout_seconds, self.offline)

    def to_dict(self):
        return {'name': self.name, 'commands': list(self.lines),
                'timeout_seconds': self.timeout_seconds, 'offline': self.offline}


def load_actions(path):
    """Reject damaged files as a whole so an edit cannot erase hidden entries."""
    path = Path(path)
    if not path.exists():
        return []
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(say('Файл избранного слишком большой.', 'Quick actions file is too large.'))
    document = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(document, dict) or document.get('format') != 'pm3-quick-actions-v1':
        raise ValueError(say('Формат избранного не поддерживается.', 'Unsupported quick actions format.'))
    rows = document.get('actions')
    if not isinstance(rows, list) or len(rows) > MAX_ACTIONS:
        raise ValueError('Invalid quick actions list.')
    result, names = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid quick action.')
        # Loading is deliberately structural: a saved command removed by a later
        # client remains visible/editable, but is revalidated before any run.
        action = QuickAction(row.get('name'), row.get('commands'), row.get('timeout_seconds'), row.get('offline'))
        if (not isinstance(action.name, str) or not action.name.strip() or len(action.name) > 80
                or any(ord(c) < 32 for c in action.name)
                or not isinstance(action.lines, list) or not 1 <= len(action.lines) <= MAX_STEPS
                or any(not isinstance(s, str) or len(s.encode('utf-8')) > 4096
                       or any(ord(c) < 32 for c in s) for s in action.lines)
                or type(action.timeout_seconds) is not int or not 5 <= action.timeout_seconds <= 1800
                or type(action.offline) is not bool):
            raise ValueError('Invalid quick action.')
        key = action.name.strip().casefold()
        if key in names:
            raise ValueError('Duplicate quick action name.')
        names.add(key)
        result.append(QuickAction(action.name.strip(), tuple(action.lines), action.timeout_seconds, action.offline))
    return result


def save_actions(path, actions, *, expected_revision=_UNSPECIFIED):
    path = Path(path)
    if len(actions) > MAX_ACTIONS:
        raise ValueError(say('Можно сохранить до 100 действий.', 'Save up to 100 actions.'))
    # Previously saved unsupported commands are kept; only a newly edited action
    # needs current-client validation, performed by the editor before saving.
    data = json.dumps({'format': 'pm3-quick-actions-v1', 'actions': [a.to_dict() for a in actions]},
                      ensure_ascii=False, indent=2) + '\n'
    if len(data.encode('utf-8')) > MAX_FILE_BYTES:
        raise ValueError('Quick actions file is too large.')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.quick-actions-', suffix='.tmp', delete=False, newline='\n') as out:
            temporary = Path(out.name)
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        # Check with exactly the same reader as a future application launch.
        load_actions(temporary)
        with _save_lock(path):
            if expected_revision is not _UNSPECIFIED and actions_revision(path) != expected_revision:
                raise ValueError(say('Избранное изменено в другом окне. Закройте и откройте его заново.',
                                     'Quick actions changed in another window. Close and reopen the editor.'))
            os.replace(temporary, path)
        return hashlib.sha256(data.encode('utf-8')).hexdigest()
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def action_flow(action):
    """One reviewed transaction; the controller stops on error/Stop, never retries."""
    from .studio_flows import one_command
    action = action.validated()
    steps = tuple(one_command(*split_command(line), action.timeout_seconds * 1000) for line in action.lines)

    def flow(directory):
        completed = 0
        for step in steps:
            result = yield from step(directory)
            code = result.get('exit_code', 0)
            if code:
                raise ValueError(say('Сценарий остановлен: команда завершилась с кодом {0}.',
                                     'Action stopped: command exited with code {0}.').format(code))
            completed += 1
        return {'completed_steps': completed, 'verification': 'client-output-only'}
    return flow
