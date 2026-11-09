"""LF command facts/help from the locally matched client; no device probing."""
from .i18n import tr
import json
from pathlib import Path
from .hf_catalog import render_hf_command

CATALOG=json.loads(Path(__file__).with_name('lf_commands.json').read_text(encoding='utf-8'))
COMMANDS={row['command']:row for row in CATALOG['commands']}


def render_lf_command(command,arguments=''):
    if not isinstance(command,str) or command not in COMMANDS:
        raise ValueError(tr('Выберите LF-команду из каталога клиента.'))
    # Share the argument boundary with HF without admitting LF to its catalogue.
    validated=render_hf_command('hf search',arguments)
    return command+validated[len('hf search'):]
