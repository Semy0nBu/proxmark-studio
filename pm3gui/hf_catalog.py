"""HF command facts/help from the matching RRG client, without hardware access."""
from .i18n import tr
import json
import re
from pathlib import Path

CATALOG=json.loads(Path(__file__).with_name('hf_commands.json').read_text(encoding='utf-8'))
COMMANDS={row['command']:row for row in CATALOG['commands']}


def render_hf_command(command, arguments=''):
    if not isinstance(command,str) or command not in COMMANDS:
        raise ValueError(tr('Выберите HF-команду из каталога клиента.'))
    if not isinstance(arguments,str) or len(arguments.encode('utf-8'))>3000:
        raise ValueError(tr('Слишком длинные параметры команды.'))
    if any(ord(ch)<32 for ch in arguments) or any(ch in arguments for ch in ';|&`'):
        raise ValueError(tr('Допустима одна команда: разделители, управляющие символы и цепочки запрещены.'))
    # The client has its own CLI lexer. Preserve quoted Windows paths verbatim;
    # no shell is involved, and quote fragments cannot introduce a second run.
    if arguments.count('"')%2:
        raise ValueError(tr('Закройте двойные кавычки параметра.'))
    return command+(' '+arguments.strip() if arguments.strip() else '')


def is_recovery_tool(command):
    return bool(re.fullmatch(r'hf mf (?:autopwn|nested|hardnested|staticnested|darkside)',command))
