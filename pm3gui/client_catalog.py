"""Single-command boundary for expert tools, including host-side commands."""
import json
import re
from pathlib import Path
from .hf_catalog import COMMANDS as HF, render_hf_command
from .lf_catalog import COMMANDS as LF

CATALOG = json.loads(Path(__file__).with_name('client_commands.json').read_text(encoding='utf-8'))
COMMANDS = {**HF, **LF, **{row['command']: row for row in CATALOG['commands']}}
_LONGEST = sorted(COMMANDS, key=len, reverse=True)


def bundled_client_matches(executable, root=None):
    """The shipped forms are usable with the exact tested bundled binary."""
    from .paths import application_root
    from .catalog_snapshot import executable_sha256
    try:
        folder = Path(root) if root is not None else application_root()
        manifest = json.loads((folder/'release.json').read_text(encoding='utf-8'))
        if not isinstance(manifest,dict):return False
        expected = manifest.get('native_client_sha256')
        return bool(isinstance(expected,str) and re.fullmatch('[0-9a-f]{64}',expected)
                    and executable_sha256(executable) == expected)
    except (OSError,ValueError,TypeError):
        return False


def confirmed_command(snapshot, executable, command, arguments=''):
    """Confirm shipped forms against one selected binary's advertised syntax.

    Kept explicit: normal native workflows do not depend on refreshing a catalog.
    This boundary is for the user-selected expert tools and catalog UI only.
    """
    from .catalog_snapshot import require_confirmed_command
    return require_confirmed_command(snapshot, executable, command, arguments, reference=COMMANDS)


def split_command(line):
    if not isinstance(line, str) or not line.strip():
        raise ValueError('Choose a client command.')
    line = line.strip()
    for command in _LONGEST:
        if line == command or line.startswith(command + ' '):
            arguments = line[len(command):].strip()
            render_command(command, arguments)
            return command, arguments
    raise ValueError('Unknown command. Choose a command supported by this client.')


def render_command(command, arguments=''):
    if command not in COMMANDS:
        raise ValueError('Unknown client command.')
    validated = render_hf_command('hf search', arguments)
    return command + validated[len('hf search'):]


def uses_rf(command):
    return (command.startswith(('hf ', 'lf ', 'nfc type', 'nfc mf ', 'nfc barcode '))
            or command in {'hw detectreader', 'hw decay', 'hw standalone', 'script run'})


def changes_data(command, arguments=''):
    """Conservative preview classification, never an authentication result."""
    words = command.split()
    return (words[-1] in {'write', 'wrbl', 'restore', 'clone', 'wipe', 'format', 'remove',
                         'rename', 'copy', 'upload', 'load', 'setkey', 'csetuid', 'gen3uid',
                         'csetblk', 'cload', 'esetblk', 'eclr', 'setlfdivisor', 'sethfthresh'}
            or bool(re.search(r'(?:write|format|setuid|setpwd|protect|finalize|lock)', words[-1]))
            or command in {'script run','hw standalone'} or words[-1] == 'raw'
            or command == 'hf mf value' and bool(re.search(r'--(?:inc|dec|set|transfer|res)\b', arguments)))
