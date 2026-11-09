"""Typed PM3 card read/recovery operations and conservative result validation.

Commands and output formats are checked against RRG v4.23346 sources. In
particular ``hf mf dump`` may save zero-filled failed blocks and return success;
file size and exit status alone do not prove that a Classic dump is complete.
"""
from __future__ import annotations
from .i18n import tr

from dataclasses import dataclass, field
from pathlib import Path
import math
import re

from .core import DumpData, parse_dump


CLASSIC_GEOMETRIES = {
    "mini": (5, 320), "1k": (16, 1024), "2k": (32, 2048), "4k": (40, 4096),
}
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_UID = re.compile(r"\bUID\s*:\s*((?:[0-9a-fA-F]{2}\s*){4,10})(?![0-9a-fA-F])", re.I)
_KEY_ROW = re.compile(
    r"(?:^|\n)\s*(?:\[[^\]\n]+\]\s*)?(\d{1,3})\s*\|\s*(\d{1,3})\s*\|\s*"
    r"([0-9A-Fa-f]{12}|-{12})\s*\|\s*([0-9A-Za-z])\s*\|\s*"
    r"([0-9A-Fa-f]{12}|-{12})\s*\|\s*([0-9A-Za-z])(?=\s|$)")
_BLOCK_OK = re.compile(r"\bSector\.{3}\s*(\d+)\s+block\.{3}\s*(\d+)\s*\(\s*ok\s*\)", re.I)
_READ_FAILURE = re.compile(
    r"(?:^|\n)[ \t]*\[(?:!+|-)\][^\n]*(?:partial dump|failed to read|timeout reading|"
    r"access rights prevent reading|\(\s*fail\s*\)|aborted via|command execution time out)", re.I)
_RECOVERY_TABLE_HEADER = re.compile(
    r"(?:^|\n)[ \t]*(?:\[[^\]\n]+\][ \t]*)?Sec[ \t]*\|[ \t]*Blk[ \t]*\|[ \t]*key A\b", re.I)
_RECOVERY_METHODS = {
    "1": "checked", "D": "dictionary", "S": "darkside", "U": "user",
    "R": "reused", "N": "nested", "H": "hardnested", "C": "staticnested", "A": "key_a",
}
TARGETED_RECOVERY_KINDS = frozenset({"mfc_nested_target", "mfc_hardnested_target", "mfc_staticnested_all", "mfc_darkside_seed"})
RECOVERY_OPERATION_KINDS = TARGETED_RECOVERY_KINDS | {"mfc_recover"}
ANTENNA_OPERATION_KINDS = frozenset({"antenna_sweep", "antenna_hf", "antenna_lf", "antenna_field_off"})
OPERATION_TIMEOUT_MS = {
    'lf_capture_search_buffer':45000,'lf_search_buffer':30000,
    'spiffs_tree':45000,'spiffs_info':45000,
    'hf_tool':1800000,'lf_tool':1800000,'expert_tool':1800000,'emulation_break':15000,
    'mfc_read_block':30000,'mfc_write_block':30000,'mfu_read_page':30000,'mfu_write_page':30000,'mfu_info':45000,
    "mfc_check_candidate": 30000, "mfc_read_trailer": 30000, "mfc_nonce_probe": 45000,
    "mfc_nested_target": 120000, "mfc_staticnested_all": 120000, "mfc_darkside_seed": 120000,
    "mfc_hardnested_target": 300000, "mfc_recover": 1800000,
    "antenna_sweep": 30000, "antenna_hf": 30000, "antenna_lf": 30000, "antenna_field_off": 15000,
}


def _text(value: str) -> str:
    return _ANSI.sub("", value).replace("\r", "\n")


def _uid(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError(tr('UID должен быть строкой HEX.'))
    value = re.sub(r"\s", "", value).upper()
    if not re.fullmatch(r"(?:[0-9A-F]{8}|[0-9A-F]{14}|[0-9A-F]{20})", value):
        raise ValueError(tr('UID должен содержать 4, 7 или 10 байт HEX.'))
    return value


@dataclass(frozen=True)
class Detection:
    family: str = "unknown"
    uid: str = ""
    protocol: str = field(default_factory=lambda:tr('Не определён'))
    classic_size: str = ""
    supported: bool = False
    reason: str = ""
    ambiguous_identity: bool = False


def parse_detection(output: str) -> Detection:
    """Parse ONE operation's output, never the accumulated GUI journal."""
    text = _text(output)
    uids = {_uid(match.group(1)) for match in _UID.finditer(text)}
    if len(uids) > 1 or re.search(r"\bmultiple (?:cards|tags)\b|more than one (?:card|tag)", text, re.I):
        return Detection(reason=tr('Обнаружено несколько UID. Оставьте одну карту на антенне.'), ambiguous_identity=True)
    uid = next(iter(uids), "")
    em_ids = set(re.findall(r"\bEM\s*410x\s+ID\s*:?\s*([0-9A-Fa-f]{10})(?![0-9A-Fa-f])", text, re.I))
    if not uid and len(em_ids) == 1:
        return Detection("em410x", next(iter(em_ids)).upper(), tr('EM410x · только идентификатор'), supported=True)
    if len(em_ids) > 1:
        return Detection(reason=tr('Обнаружено несколько LF-идентификаторов.'), ambiguous_identity=True)
    if not uid:
        if re.search(r"no (?:known .*?tags|.*?tags|.*?cards).*?found|no (?:card|tag) response", text, re.I):
            return Detection(reason=tr('Карта не обнаружена. Проверьте положение на антенне.'))
        return Detection(reason=tr('Ответ не содержит однозначного UID; тип карты не подтверждён.'))
    if re.search(r"\b(?:NTAG\s*\d{3}|MIFARE\s+Ultralight)\b", text, re.I):
        match = re.search(r"NTAG\s*(\d{3})", text, re.I)
        protocol = f"NTAG {match.group(1)}" if match else "MIFARE Ultralight / NTAG"
        return Detection("mfu", uid, protocol, supported=True,
                         reason=tr('Защищённые страницы могут потребовать известный пароль или ключ.'))
    sizes = set()
    for value in re.findall(r"MIFARE\s+Classic\s+(Mini|[124]\s*K)\b", text, re.I):
        sizes.add(re.sub(r"\s", "", value).lower())
    if len(sizes) == 1:
        size = next(iter(sizes))
        return Detection("mfc", uid, f"MIFARE Classic {size.upper()}", size, True)
    if len(sizes) > 1:
        return Detection("mfc", uid, "MIFARE Classic-compatible", reason=tr('Размер памяти неоднозначен; выберите профиль после проверки информации о карте.'))
    sak = re.search(r"\bSAK\s*:\s*([0-9a-fA-F]{2})\b", text)
    if sak and sak.group(1).upper() in {"08", "09", "18"}:
        size = {"08": "1k", "09": "mini", "18": "4k"}[sak.group(1).upper()]
        return Detection("mfc", uid, "MIFARE Classic-compatible", size, True,
                         tr('Профиль {0} выбран по SAK {1}; проверьте информацию о карте.', f'{size.upper()}', f'{sak.group(1).upper()}'))
    return Detection("unknown", uid, tr('ISO14443-A / неподдерживаемый профиль'), reason=tr('UID получен, но семейство чтения не подтверждено.'))


parse_output = parse_detection


def _path_token(value: Path, *, existing: bool = False) -> str:
    if not isinstance(value, Path):
        raise ValueError(tr('Путь операции должен быть pathlib.Path.'))
    path = value.expanduser().resolve()
    text = path.as_posix()
    if any(ord(char) < 32 for char in text) or any(char in text for char in '\";|&`'):
        raise ValueError(tr('Путь содержит недопустимые символы команды.'))
    if len(text.encode("utf-8")) > 700:
        raise ValueError(tr('Путь слишком длинный для клиента Proxmark3.'))
    if existing and not path.is_file():
        raise ValueError(tr('Файл не найден: {0}', f'{path.name}'))
    return f'"{text}"'


def _prefix(value: Path | None) -> str:
    if value is None or not isinstance(value, Path):
        raise ValueError(tr('Укажите путь сохранения дампа.'))
    path = value.expanduser().resolve()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", path.name) or not path.parent.is_dir():
        raise ValueError(tr("Имя дампа должно состоять из букв, цифр, '_' или '-' без расширения; папка должна существовать."))
    if any(path.with_suffix(suffix).exists() for suffix in ("", ".json", ".bin", ".eml", ".pm3")):
        raise ValueError(tr('Файлы результата уже существуют. Создайте новую папку операции.'))
    return _path_token(path)


@dataclass(frozen=True)
class Operation:
    kind: str
    output_prefix: Path | None = None
    classic_size: str = "1k"
    dictionary_path: Path | None = None
    key_file: Path | None = None
    auth_key: str = ""
    known_sector: int | None = None
    known_type: str = "A"
    output_suffix: str = "gui-recovery"
    target_sector: int | None = None
    target_type: str = "A"
    sample_count: int = 5
    lf_divisor: int = 95
    block: int | None = None
    data_hex: str = ''
    allow_trailer: bool = False
    secure_channel: bool = False
    hf_command: str = ''
    hf_args: str = ''
    lf_command: str = ''
    lf_args: str = ''
    client_command: str = ''
    client_args: str = ''

    def render(self) -> str:
        if self.kind in {'lf_capture_search_buffer', 'lf_search_buffer'}:
            if self != Operation(self.kind):
                raise ValueError('The bounded LF search accepts no parameters.')
            # Below RDV4 BigBuf capacity: avoid the USB-only realtime path.
            # Decode is a separate operation and may only follow a proven
            # capture, so a failed transfer cannot reuse a stale GraphBuffer.
            return ('lf read -v -s 30000' if self.kind == 'lf_capture_search_buffer'
                    else 'lf search -1')
        if self.kind in {'spiffs_tree', 'spiffs_info'}:
            if self != Operation(self.kind):
                raise ValueError('The fenced SPIFFS query accepts no parameters.')
            # Native tree/info send a command and immediately return without ACK.
            # Firmware handles it synchronously; a subsequent fresh OS reply
            # fences its debug listing in the same captured client operation.
            return 'mem spiffs '+self.kind.removeprefix('spiffs_')+'; hw version'
        if self.kind == 'expert_tool':
            from dataclasses import replace
            from .client_catalog import render_command
            if replace(self, kind='_default', client_command='', client_args='') != Operation('_default'):
                raise ValueError('Expert commands cannot contain parameters of another operation.')
            return render_command(self.client_command, self.client_args)
        if self.client_command or self.client_args:
            raise ValueError('Client parameters require an expert operation.')
        if self.kind=='emulation_break':
            if self!=Operation('emulation_break'):raise ValueError(tr('Остановка эмуляции не принимает параметры.'))
            return 'hw break'
        if self.kind in {'hf_tool','lf_tool'}:
            if (self.output_prefix is not None or self.dictionary_path is not None or self.key_file is not None
                or self.auth_key or self.known_sector is not None or self.known_type!='A' or self.output_suffix!='gui-recovery'
                or self.target_sector is not None or self.target_type!='A' or self.sample_count!=5 or self.lf_divisor!=95
                or self.classic_size!='1k' or self.block is not None or self.data_hex or self.allow_trailer or self.secure_channel):
                raise ValueError(tr('Профильная команда не принимает параметры других операций.'))
            if self.kind=='lf_tool':
                if self.hf_command or self.hf_args:raise ValueError(tr('LF-команда не принимает параметры HF.'))
                from .lf_catalog import render_lf_command
                return render_lf_command(self.lf_command,self.lf_args)
            if self.lf_command or self.lf_args:raise ValueError(tr('HF-команда не принимает параметры LF.'))
            from .hf_catalog import render_hf_command
            return render_hf_command(self.hf_command,self.hf_args)
        if self.hf_command or self.hf_args or self.lf_command or self.lf_args:
            raise ValueError(tr('Профильные параметры допустимы только в Инструментах HF/LF.'))
        if self.kind in {'mfc_read_block','mfc_write_block','mfu_read_page','mfu_write_page','mfu_info'}:
            if any(v is not None for v in (self.output_prefix,self.dictionary_path,self.key_file,self.known_sector,self.target_sector)) or self.target_type!='A' or self.output_suffix!='gui-recovery' or self.sample_count!=5 or self.lf_divisor!=95:
                raise ValueError(tr('Недопустимые параметры адресного чтения/записи.'))
            if type(self.allow_trailer) is not bool or type(self.secure_channel) is not bool:
                raise ValueError(tr('Флаги операции должны быть bool.'))
            if self.kind.startswith('mfc_'):
                if self.secure_channel or self.classic_size not in CLASSIC_GEOMETRIES or type(self.block) is not int or not 0<=self.block<CLASSIC_GEOMETRIES[self.classic_size][1]//16:
                    raise ValueError(tr('Адрес выходит за пределы Classic.'))
                key, kind = _classic_key(self.auth_key), _key_type(self.known_type)
                if self.kind=='mfc_read_block':
                    if self.data_hex or self.allow_trailer:
                        raise ValueError(tr('Чтение не принимает данные записи.'))
                    return f'hf mf rdbl --blk {self.block} -{kind.lower()} -k {key}'
                if self.block==0 or (self.block in {sector_trailer(s) for s in range(CLASSIC_GEOMETRIES[self.classic_size][0])} and not self.allow_trailer):
                    raise ValueError(tr('Запись UID запрещена; ключи требуют отдельного выбора.'))
                if not isinstance(self.data_hex,str) or not re.fullmatch(r'[0-9A-Fa-f]{32}',self.data_hex):
                    raise ValueError(tr('Запись Classic требует 16 байт HEX.'))
                return f'hf mf wrbl --blk {self.block} -{kind.lower()} -k {key} -d {self.data_hex.upper()}'
            if self.allow_trailer or self.known_type!='A' or self.classic_size!='1k':
                raise ValueError(tr('Параметры Classic неприменимы к NTAG/Ultralight.'))
            if self.auth_key and not re.fullmatch(r'(?:[0-9A-Fa-f]{8}|[0-9A-Fa-f]{32})',self.auth_key):
                raise ValueError(tr('PWD — 8 HEX, ключ UL-C/AES — 32 HEX.'))
            if self.secure_channel and len(self.auth_key)!=32:
                raise ValueError(tr('Защищённый канал требует 16-байтный ключ.'))
            auth = (' -k '+self.auth_key.upper()) if self.auth_key else ''
            auth += ' --schann' if self.secure_channel else ''
            if self.kind=='mfu_info':
                if self.block is not None or self.data_hex:
                    raise ValueError(tr('Информация не принимает адрес или данные.'))
                return 'hf mfu info'+auth
            if type(self.block) is not int or not 0<=self.block<=255:
                raise ValueError(tr('Неверный адрес страницы.'))
            if self.kind=='mfu_read_page':
                if self.data_hex:
                    raise ValueError(tr('Чтение не принимает данные записи.'))
                return f'hf mfu rdbl -b {self.block}'+auth
            if self.block<4 or not isinstance(self.data_hex,str) or not re.fullmatch(r'[0-9A-Fa-f]{8}',self.data_hex):
                raise ValueError(tr('Запись требует пользовательскую страницу и 4 байта HEX.'))
            return f'hf mfu wrbl -b {self.block} -d {self.data_hex.upper()}'+auth
        if self.block is not None or self.data_hex or self.allow_trailer or self.secure_channel:
            raise ValueError(tr('Адресные параметры допустимы только для адресного чтения/записи.'))
        if self.kind in ANTENNA_OPERATION_KINDS:
            if (self.classic_size != "1k" or self.dictionary_path is not None or self.key_file is not None
                    or self.auth_key or self.known_sector is not None or self.known_type != "A"
                    or self.output_suffix != "gui-recovery" or self.target_sector is not None or self.target_type != "A"):
                raise ValueError(tr('Измерение антенн не принимает параметры карт или ключей.'))
            if self.kind in {"antenna_sweep", "antenna_field_off"}:
                if self.sample_count != 5 or type(self.sample_count) is not int or self.lf_divisor != 95 or type(self.lf_divisor) is not int:
                    raise ValueError(tr('Общий sweep и выключение поля не принимают число отсчётов или LF divisor.'))
                if self.kind == "antenna_sweep":
                    # GraphBuffer belongs to this CLI process. Both commands
                    # are fixed catalog actions; only the quoted path varies.
                    return "hw tune; data save -f " + _prefix(self.output_prefix)
                if self.output_prefix is not None:
                    raise ValueError(tr('Выключение поля не сохраняет график.'))
                return "hw fpgaoff"
            if self.output_prefix is not None:
                raise ValueError(tr('Отдельное измерение HF/LF не сохраняет LF sweep.'))
            if type(self.sample_count) is not int or not 1 <= self.sample_count <= 20:
                raise ValueError(tr('Число отсчётов должно быть от 1 до 20; бесконечный режим запрещён.'))
            if type(self.lf_divisor) is not int or not 19 <= self.lf_divisor <= 255:
                raise ValueError(tr('LF divisor должен быть целым числом от 19 до 255.'))
            if self.kind == "antenna_hf":
                if self.lf_divisor != 95:
                    raise ValueError(tr('HF измеряется на 13.56 MHz и не принимает LF divisor.'))
                return f"hf tune --iter {self.sample_count} --value --verbose"
            return f"lf tune --iter {self.sample_count} --divisor {self.lf_divisor} --value --verbose"
        if self.sample_count != 5 or type(self.sample_count) is not int or self.lf_divisor != 95 or type(self.lf_divisor) is not int:
            raise ValueError(tr('Параметры антенн допустимы только для измерения антенн.'))
        targeted = TARGETED_RECOVERY_KINDS | {"mfc_check_candidate", "mfc_read_trailer", "mfc_nonce_probe"}
        if self.kind in targeted:
            if any((self.output_prefix, self.dictionary_path, self.key_file)) or self.output_suffix != "gui-recovery":
                raise ValueError(tr('Целевая операция не принимает файлы дампа, словарь или суффикс.'))
            if self.classic_size not in CLASSIC_GEOMETRIES:
                raise ValueError(tr('Недопустимый профиль Classic.'))
            if self.kind == "mfc_nonce_probe":
                if self.auth_key or self.known_sector is not None or self.target_sector is not None or self.known_type != "A" or self.target_type != "A":
                    raise ValueError(tr('Общий тест PRNG не принимает ключи или номера секторов.'))
                return "hf 14a info"
            if self.kind == "mfc_darkside_seed":
                if self.auth_key or self.known_sector is not None or self.known_type != "A" or self.target_sector != 0 or self.target_type != "A":
                    raise ValueError(tr('Начальный darkside допускает только сектор 0 / A без исходного ключа.'))
                return "hf mf darkside --blk 0"
            source = _profile_sector(self.known_sector, self.classic_size)
            key = _classic_key(self.auth_key)
            source_type = _key_type(self.known_type)
            if self.kind == "mfc_check_candidate":
                if self.target_sector is not None or self.target_type != "A":
                    raise ValueError(tr('Проверяемая пара задаётся known_sector/known_type.'))
                return f"hf mf chk --{self.classic_size} --tblk {sector_first_block(source)} -{source_type.lower()} -k {key} --no-default"
            if self.kind == "mfc_read_trailer":
                if source_type != "A" or self.target_sector is not None or self.target_type != "A":
                    raise ValueError(tr('Чтение доступного Key B выполняется известным Key A своего сектора.'))
                return f"hf mf rdbl --blk {sector_trailer(source)} -a -k {key}"
            if self.kind == "mfc_staticnested_all":
                if self.target_sector is not None or self.target_type != "A":
                    raise ValueError(tr('У staticnested этой версии нет отдельной цели; допускается один ограниченный запуск по карте.'))
                return f"hf mf staticnested --{self.classic_size} --blk {sector_first_block(source)} -{source_type.lower()} -k {key}"
            target = _profile_sector(self.target_sector, self.classic_size)
            target_type = _key_type(self.target_type)
            if (source, source_type) == (target, target_type):
                raise ValueError(tr('Нельзя восстанавливать уже известную исходную пару сектор/ключ.'))
            command = "nested" if self.kind == "mfc_nested_target" else "hardnested"
            geometry = f" --{self.classic_size}" if command == "nested" else ""
            return f"hf mf {command}{geometry} --blk {sector_first_block(source)} -{source_type.lower()} -k {key} --tblk {sector_first_block(target)} --t{target_type.lower()}"
        if self.target_sector is not None or self.target_type != "A":
            raise ValueError(tr('Целевая пара допустима только для целевого восстановления.'))
        if self.kind == "mfc_recover":
            if self.output_prefix is not None or self.key_file is not None or self.classic_size not in CLASSIC_GEOMETRIES:
                raise ValueError(tr('Недопустимые параметры восстановления ключей Classic.'))
            suffix = _recovery_suffix(self.output_suffix)
            command = f"hf mf autopwn --{self.classic_size}"
            if self.auth_key:
                if not isinstance(self.auth_key, str) or not re.fullmatch(r"[0-9A-Fa-f]{12}", self.auth_key):
                    raise ValueError(tr('Известный ключ Classic должен содержать 12 HEX.'))
                if type(self.known_sector) is not int or not 0 <= self.known_sector < CLASSIC_GEOMETRIES[self.classic_size][0]:
                    raise ValueError(tr('Укажите допустимый сектор известного ключа.'))
                if self.known_type not in {"A", "B", "a", "b"}:
                    raise ValueError(tr('Тип известного ключа должен быть A или B.'))
                command += f" -s {self.known_sector} -{self.known_type.lower()} -k {self.auth_key.upper()}"
            elif self.known_sector is not None or self.known_type not in {"A", "a"}:
                raise ValueError(tr('Сектор и тип начального ключа требуют известный ключ.'))
            if self.dictionary_path is not None:
                command += " -f " + _path_token(self.dictionary_path, existing=True)
            return command + " --suffix " + suffix
        if self.known_sector is not None or self.known_type != "A" or self.output_suffix != "gui-recovery":
            raise ValueError(tr('Параметры восстановления допустимы только для mfc_recover.'))
        if self.kind in {"mfc_info", "hf14a_select"}:
            if any((self.output_prefix, self.dictionary_path, self.key_file, self.auth_key)):
                raise ValueError(tr('Информация о карте не принимает параметры чтения.'))
            # hf mf info also probes proprietary backdoors. A read workflow only
            # needs a standard anticollision/select to re-check UID and SAK.
            return "hf 14a reader --skip"
        if self.kind == "lf_em410x_read":
            if any((self.output_prefix, self.dictionary_path, self.key_file, self.auth_key)):
                raise ValueError(tr('Чтение LF ID не принимает параметры дампа.'))
            return "lf em 410x reader"
        if self.kind in {"mfc_check_keys", "mfc_dump"}:
            if self.classic_size not in CLASSIC_GEOMETRIES or self.auth_key:
                raise ValueError(tr('Недопустимый профиль MIFARE Classic.'))
            if self.kind == "mfc_check_keys":
                if self.output_prefix is not None or self.key_file is not None:
                    raise ValueError(tr('Проверка ключей сохраняет UID-key.bin в новой папке операции.'))
                command = f"hf mf fchk --{self.classic_size} --dump"
                if self.dictionary_path is not None:
                    command += " -f " + _path_token(self.dictionary_path, existing=True)
                return command
            if self.dictionary_path is not None or self.key_file is None:
                raise ValueError(tr('Для чтения Classic укажите подготовленный файл ключей.'))
            token = _path_token(self.key_file, existing=True)
            expected = CLASSIC_GEOMETRIES[self.classic_size][0] * 12
            if self.key_file.expanduser().resolve().stat().st_size != expected:
                raise ValueError(tr('Файл ключей для {0} должен содержать {1} байт (все A, затем все B).', f'{self.classic_size}', f'{expected}'))
            return f"hf mf dump --{self.classic_size} -f {_prefix(self.output_prefix)} -k {token}"
        if self.kind == "mfu_dump":
            if self.dictionary_path is not None or self.key_file is not None:
                raise ValueError(tr('Для Ultralight/NTAG используется известный пароль или ключ, а не файл Classic.'))
            command = "hf mfu dump -f " + _prefix(self.output_prefix)
            if self.auth_key:
                if not isinstance(self.auth_key, str) or not re.fullmatch(r"(?:[0-9A-Fa-f]{8}|[0-9A-Fa-f]{32})", self.auth_key):
                    raise ValueError(tr('Пароль NTAG — 8 HEX, ключ UL-C/UL-AES — 32 HEX.'))
                command += " -k " + self.auth_key.upper()
            return command
        raise ValueError(tr('Неизвестная операция чтения.'))

    @property
    def recommended_timeout_ms(self) -> int:
        return OPERATION_TIMEOUT_MS.get(self.kind, 120000)


def sector_trailer(sector: int) -> int:
    if type(sector) is not int or not 0 <= sector < 40:
        raise ValueError(tr('Недопустимый сектор Classic.'))
    return sector * 4 + 3 if sector < 32 else 128 + (sector - 32) * 16 + 15


def sector_first_block(sector: int) -> int:
    if type(sector) is not int or not 0 <= sector < 40:
        raise ValueError(tr('Недопустимый сектор Classic.'))
    return sector * 4 if sector < 32 else 128 + (sector - 32) * 16


def _profile_sector(value: int | None, size: str) -> int:
    if type(value) is not int or not 0 <= value < CLASSIC_GEOMETRIES[size][0]:
        raise ValueError(tr('Сектор не соответствует выбранной геометрии Classic.'))
    return value


def _classic_key(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9A-Fa-f]{12}", value):
        raise ValueError(tr('Ключ Classic должен содержать 12 HEX.'))
    return value.upper()


def _key_type(value: str) -> str:
    if not isinstance(value, str) or value not in {"A", "B", "a", "b"}:
        raise ValueError(tr('Тип ключа должен быть A или B.'))
    return value.upper()


def parse_candidate_verification(output: str, sector: int, key_type: str,
                                 key_hex: str, classic_size: str = "1k") -> bool:
    """Require positive chk evidence for precisely the requested slot and key.

    chk with A may also read B. Those incidental B values must not authenticate
    a different requested slot; callers issue chk with the requested A/B flag.
    """
    if classic_size not in CLASSIC_GEOMETRIES:
        raise ValueError(tr('Недопустимый профиль Classic.'))
    sector = _profile_sector(sector, classic_size)
    kind, key = _key_type(key_type), _classic_key(key_hex)
    return any(entry == {"sector": sector, "type": kind, "key_hex": key}
               for entry in parse_found_keys(output, classic_size))


def parse_target_key(output: str, operation: Operation) -> str | None:
    """Extract a recovery candidate, never proof of successful RF authentication.

    Single-target nested may return success despite a printed failure in this
    client version. Only the explicit positive result is usable. Hardnested's
    positive result omits target identity, so it is bound to this typed run and
    must subsequently pass mfc_check_candidate before entering the key library.
    """
    if type(operation) is not Operation or operation.kind not in {
            "mfc_nested_target", "mfc_hardnested_target", "mfc_darkside_seed"}:
        raise ValueError(tr('Ожидалась операция восстановления отдельной пары сектор/ключ.'))
    operation.render()
    text = _text(output)
    candidates = set()
    if operation.kind == "mfc_nested_target":
        pattern = r"\btarget block\s+(\d+)\s+key type\s+([AB])\s+--\s+found valid key\s*\[\s*([0-9A-F]{12})\s*\]"
        for match in re.finditer(pattern, text, re.I):
            if (int(match.group(1)), match.group(2).upper()) == (
                    sector_first_block(operation.target_sector), operation.target_type.upper()):
                candidates.add(match.group(3).upper())
    elif operation.kind == "mfc_hardnested_target":
        candidates.update(re.findall(r"\bBrute force completed\.\s*Key found:\s*([0-9A-F]{12})(?![0-9A-F])", text, re.I))
    else:
        # darkside has no explicit type A flag; the restricted operation uses
        # its source-verified default, block 0 / A, without -b or -c options.
        candidates.update(re.findall(r"(?:^|\n)\s*\[\+\]\s*Found valid key\s*\[\s*([0-9A-F]{12})\s*\]", text, re.I))
    candidates = {key.upper() for key in candidates}
    return next(iter(candidates)) if len(candidates) == 1 else None


def parse_trailer_block(output: str, sector: int) -> bytes | None:
    """Read exactly one unambiguous rdbl row for the requested trailer."""
    trailer = sector_trailer(sector)
    rows = set()
    pattern = r"(?:^|\n)\s*(?:\[[^\]\n]+\]\s*)?(\d{1,3})\s*\|\s*((?:[0-9A-F]{2}\s+){15}[0-9A-F]{2})\s*\|"
    for match in re.finditer(pattern, _text(output), re.I):
        if int(match.group(1)) == trailer:
            rows.add(bytes.fromhex(match.group(2)))
    return next(iter(rows)) if len(rows) == 1 else None


def readable_key_b_from_trailer(output: str, sector: int) -> str | None:
    """Return visible B data only when valid trailer ACL permits A to read it.

    This is plaintext evidence, not B-authentication proof. Under those ACLs B
    is data and may not authenticate; the scheduler must not infer Key A or
    treat the returned value as a usable B source without separate RF checking.
    """
    raw = parse_trailer_block(output, sector)
    if raw is None:
        return None
    b6, b7, b8 = raw[6:9]
    d1, d2, d3 = b7 >> 4, b8 & 15, b8 >> 4
    if (b6 & 15, b6 >> 4, b7 & 15) != (d1 ^ 15, d2 ^ 15, d3 ^ 15):
        return None
    condition = (((d1 >> 3) & 1) << 2) | (((d2 >> 3) & 1) << 1) | ((d3 >> 3) & 1)
    return raw[10:16].hex().upper() if condition in {0, 1, 2} else None


def parse_nonce_kind(output: str) -> str:
    """Classify only explicit standard information output, with no guessing."""
    text = _text(output)
    if re.search(r"\bStatic enc nonce\.*\s+yes\b", text, re.I):
        return "static_encrypted"
    if re.search(r"\bStatic nonce\.*\s+yes\b", text, re.I):
        return "static"
    matches = set(value.lower() for value in re.findall(r"\bPrng detection\.*\s+(weak|hard)\b", text, re.I))
    return next(iter(matches)) if len(matches) == 1 else "unknown"


def parse_lf_sweep(path: Path) -> list[dict]:
    """Decode hw tune's 256 GraphBuffer integers, preserving ADC quantization.

    Only indices 19..255 were measured. The remaining entries are placeholders.
    The graph contains byte-scaled ADC values, not exact voltage readings.
    """
    path = Path(path)
    if path.suffix.lower() != ".pm3" or not path.is_file() or path.stat().st_size > 4096:
        raise ValueError(tr('Нет корректного файла LF sweep .pm3.'))
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except UnicodeError as exc:
        raise ValueError(tr('LF sweep должен содержать только ASCII числа.')) from exc
    if len(lines) != 256 or any(not re.fullmatch(r"-?(?:0|[1-9]\d{0,2})", line) for line in lines):
        raise ValueError(tr('LF sweep должен содержать ровно 256 целых отсчётов.'))
    values = [int(line) for line in lines]
    if any(not -128 <= value <= 127 for value in values) or any(value != -128 for value in values[:19]):
        raise ValueError(tr('LF sweep не соответствует шкале GraphBuffer данного клиента.'))
    if all(value == -128 for value in values[19:]):
        raise ValueError(tr('Файл LF sweep не содержит измеренных ненулевых значений.'))
    return [{"divisor": divisor, "frequency_khz": 12000.0 / (divisor + 1),
             "voltage_v": (values[divisor] + 128) * 0.512, "raw_value": values[divisor]}
            for divisor in range(255, 18, -1)]


def parse_antenna_measurement(output: str, operation: Operation, returncode: int = 0) -> dict:
    """Validate exactly one finite antenna operation's output and optional curve.

    Values absent from hw tune's output remain None (it suppresses <=1 V).
    Both 'Peak voltage' lines inside Q sections are dimensionless estimates.
    """
    if type(operation) is not Operation or operation.kind not in ANTENNA_OPERATION_KINDS - {"antenna_field_off"}:
        raise ValueError(tr('Ожидалась операция измерения антенн.'))
    if type(returncode) is not int or returncode:
        raise ValueError(tr('Измерение антенн прервано или завершилось ошибкой клиента.'))
    text = _text(output)
    if re.search(r"No response from Proxmark3|Antenna tuning failed|timeout while waiting|unexpected reply|"
                 r"file not found or locked|cannot communicate|UART.*(?:error|fail)|aborted via|Traceback", text, re.I):
        raise ValueError(tr('Клиент сообщил об ошибке измерения; результат не подтверждён.'))
    if re.search(r"\b(?:Average|Peak voltage|Frequency bandwidth)\.*\s+[-+]?(?:nan|inf(?:inity)?)\b", text, re.I):
        raise ValueError(tr('Клиент не получил конечное числовое измерение антенны.'))
    result = dict.fromkeys(("lf125_v", "lf134_v", "lf_peak_khz", "lf_peak_v", "hf_v",
                            "lf_q_bandwidth", "lf_q_voltage", "hf_q_voltage", "lf_status", "hf_status"))
    result.update(kind=operation.kind, band={"antenna_sweep": "both", "antenna_hf": "hf", "antenna_lf": "lf"}[operation.kind],
                  status="measured", warnings=re.findall(r"(?:^|\n)\s*\[(?:!+|-)\]\s*([^\n]+)", text))

    def number_once(pattern: str, scope: str, *, required: bool = True) -> float | None:
        matches = re.findall(pattern, scope, re.I)
        if not matches and not required:
            return None
        if len(matches) != 1:
            raise ValueError(tr('Ответ содержит неполные или повторные измерения антенны.'))
        value = float(matches[0])
        if not math.isfinite(value) or value < 0:
            raise ValueError(tr('В измерении антенны недопустимое числовое значение.'))
        return value

    if operation.kind != "antenna_sweep":
        operation.render()
        header = r"\bMeasuring HF antenna\b" if operation.kind == "antenna_hf" else r"\bMeasuring LF antenna at\s+([0-9.]+)\s+kHz\b"
        headers = re.findall(header, text, re.I)
        if len(headers) != 1 or len(re.findall(r"\bDone!", text)) != 1:
            raise ValueError(tr('Нет полного результата выбранного измерения антенны.'))
        frequency = 13560.0 if operation.kind == "antenna_hf" else 12000.0 / (operation.lf_divisor + 1)
        if operation.kind == "antenna_lf" and abs(float(headers[0]) - frequency) > 0.0051:
            raise ValueError(tr('Измеренная LF частота не соответствует выбранному divisor.'))
        minimum = number_once(r"\bMin\.{3,}\s*(\d+)\s+mV\b", text)
        maximum = number_once(r"\bMax\.{3,}\s*(\d+)\s+mV\b", text)
        average = number_once(r"\bAverage\.{3,}\s*(\d+(?:\.\d+)?)\s+mV\b", text)
        readings = [int(value) for value in re.findall(r"\[=\]\s*(\d+)\s+mV\s*/\s*\d+\s+V\s*/\s*\d+\s+Vmax\b", text)]
        if not readings or readings[0] != 0 or len(readings) != operation.sample_count + 1:
            raise ValueError(tr('Клиент не подтвердил запрошенное число отсчётов антенны.'))
        readings = readings[1:]
        if (any(value > 140800 for value in readings) or minimum != min(readings) or maximum != max(readings)
                or not minimum <= average <= maximum or abs(average - sum(readings) / len(readings)) > 0.00051):
            raise ValueError(tr('Отсчёты и итоговая статистика антенны не согласуются.'))
        result.update(frequency_khz=frequency, sample_count=len(readings), samples_v=[v / 1000.0 for v in readings],
                      min_v=minimum / 1000.0, max_v=maximum / 1000.0, average_v=average / 1000.0, latest_v=readings[-1] / 1000.0)
        if operation.kind == "antenna_hf":
            result["hf_v"] = result["average_v"]
        elif operation.lf_divisor in {88, 95}:
            result["lf134_v" if operation.lf_divisor == 88 else "lf125_v"] = result["average_v"]
        return result

    if not isinstance(operation.output_prefix, Path):
        raise ValueError(tr('Для общего измерения нужен путь LF sweep.'))
    lf_headers = list(re.finditer(r"-{3,}\s*LF Antenna\s*-{3,}", text, re.I))
    hf_headers = list(re.finditer(r"-{3,}\s*HF Antenna\s*-{3,}", text, re.I))
    if len(lf_headers) != 1 or len(hf_headers) != 1 or lf_headers[0].start() >= hf_headers[0].start():
        raise ValueError(tr('Общий тест не содержит однозначных разделов LF и HF.'))
    if "Q factor must be measured without tag on the antenna" not in text:
        raise ValueError(tr('Общий тест антенн не завершён.'))
    lf_text = text[lf_headers[0].end():hf_headers[0].start()]
    hf_text = text[hf_headers[0].end():]
    for band, scope in [("lf", lf_text), ("hf", hf_text)]:
        statuses = re.findall(rf"\b{band} antenna\.*\s*\(?\s*(ok|marginal|unusable)\b\s*\)?", scope, re.I)
        if len(statuses) != 1:
            raise ValueError(tr('Нет однозначной оценки LF/HF антенны.'))
        result[band + "_status"] = statuses[0].lower()
    volts = re.findall(r"\b(\d+(?:\.\d+)?)\s+kHz\s*\.{3,}\s*(\d+(?:\.\d+)?)\s+V\b", lf_text, re.I)
    seen = set()
    for frequency_text, voltage_text in volts:
        frequency, voltage = float(frequency_text), float(voltage_text)
        if not 46.87 <= frequency <= 600 or not 0 <= voltage <= 140.8 or frequency in seen:
            raise ValueError(tr('В LF ответе неверные частоты или напряжения.'))
        seen.add(frequency)
        if abs(frequency - 125) <= 0.0051:
            result["lf125_v"] = voltage
        elif abs(frequency - 12000 / 89) <= 0.0051:
            result["lf134_v"] = voltage
    optimum = re.findall(r"\b(\d+(?:\.\d+)?)\s+kHz optimal\.{3,}\s*(\d+(?:\.\d+)?)\s+V\b", lf_text, re.I)
    if len(optimum) > 1 or not optimum and result["lf_status"] != "unusable":
        raise ValueError(tr('LF ответ не содержит однозначного резонансного максимума.'))
    if optimum:
        result["lf_peak_khz"], result["lf_peak_v"] = map(float, optimum[0])
        if not 46.87 <= result["lf_peak_khz"] <= 600 or not 0 <= result["lf_peak_v"] <= 140.8:
            raise ValueError(tr('Недопустимый LF резонанс или напряжение.'))
    result["hf_v"] = number_once(r"\b13\.56\s+MHz\.{3,}\s*(\d+(?:\.\d+)?)\s+V\b", hf_text,
                                  required=result["hf_status"] != "unusable")
    if result["hf_v"] is not None and result["hf_v"] > 140.8:
        raise ValueError(tr('Недопустимое HF напряжение.'))
    qpattern = r"\bPeak voltage\.{3,}[ \t]*(\d+(?:\.\d+)?)[ \t]*(?:\n|$)"
    result["lf_q_bandwidth"] = number_once(r"\bFrequency bandwidth\.{3,}[ \t]*(\d+(?:\.\d+)?)[ \t]*(?:\n|$)", lf_text, required=False)
    result["lf_q_voltage"] = number_once(qpattern, lf_text, required=bool(optimum))
    result["hf_q_voltage"] = number_once(qpattern, hf_text, required=result["hf_status"] != "unusable")
    graph_path = operation.output_prefix.with_suffix(".pm3").resolve()
    zero_graph = bool(re.search(r"All values are zero\. Not showing LF tuning graph", text, re.I))
    result.update(lf_curve=[], curve_quantum_v=0.512, graph_unavailable_reason=None)
    if zero_graph:
        if graph_path.exists() or optimum or result["lf_status"] != "unusable":
            raise ValueError(tr('Нулевой LF sweep противоречит файлу или измерениям.'))
        result["graph_unavailable_reason"] = "all_lf_values_zero"
        return result
    saved = re.findall(r"Saved\s+256\s+bytes to PM3 file\s+`([^`\n]+)`", text, re.I)
    if len(saved) != 1 or Path(saved[0]).resolve() != graph_path:
        raise ValueError(tr('Клиент не подтвердил сохранение именно этого LF sweep.'))
    result["lf_curve"] = parse_lf_sweep(graph_path)
    if optimum:
        divisor = round(12000 / result["lf_peak_khz"] - 1)
        by_divisor = {entry["divisor"]: entry for entry in result["lf_curve"]}
        if divisor not in by_divisor or abs(by_divisor[divisor]["frequency_khz"] - result["lf_peak_khz"]) > 0.0051:
            raise ValueError(tr('LF резонанс не соответствует шагам sweep.'))
        maximum_raw = max(entry["raw_value"] for entry in result["lf_curve"])
        if by_divisor[divisor]["raw_value"] != maximum_raw:
            raise ValueError(tr('LF максимум и сохранённый sweep не совпадают.'))
        graph_v = by_divisor[divisor]["voltage_v"]
        if result["lf_peak_v"] < 131.072 and not graph_v - 0.0051 <= result["lf_peak_v"] < graph_v + 0.5171:
            raise ValueError(tr('LF напряжение максимума не совпадает со шкалой sweep.'))
    return result


def key_file_for_session(directory: Path, uid: str) -> Path:
    return Path(directory).resolve() / f"hf-mf-{_uid(uid)}-key.bin"


def _recovery_suffix(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", value):
        raise ValueError(tr("Суффикс восстановления должен состоять из 1–48 букв, цифр, '_' или '-'."))
    return value


def recovery_key_file_for_session(directory: Path, uid: str, suffix: str = "gui-recovery") -> Path:
    return Path(directory).resolve() / f"hf-mf-{_uid(uid)}-key-{_recovery_suffix(suffix)}.bin"


def parse_found_keys(output: str, classic_size: str = "1k") -> list[dict]:
    """Read positive fchk table evidence; never infer found keys from FF placeholders."""
    if classic_size not in CLASSIC_GEOMETRIES:
        raise ValueError(tr('Недопустимый профиль Classic.'))
    count = CLASSIC_GEOMETRIES[classic_size][0]
    found = {}
    for match in _KEY_ROW.finditer(_text(output)):
        sector, block = int(match.group(1)), int(match.group(2))
        if sector >= count or block != sector_trailer(sector):
            raise ValueError(tr('Таблица ключей не соответствует выбранной геометрии.'))
        for kind, key, result in [("A", match.group(3), match.group(4)), ("B", match.group(5), match.group(6))]:
            # fchk records authenticated/read keys with numeric flag 1. Other
            # table legends belong to recovery commands we do not run here.
            if result == "1" and re.fullmatch(r"[0-9A-Fa-f]{12}", key):
                value = key.upper()
                previous = found.get((sector, kind))
                if previous is not None and previous != value:
                    raise ValueError(tr('В результате есть разные ключи одной пары сектор/тип.'))
                found[(sector, kind)] = value
    return [{"sector": sector, "type": kind, "key_hex": key}
            for (sector, kind), key in sorted(found.items())]


def validate_key_file(path: Path, output: str, classic_size: str = "1k") -> list[dict]:
    """Cross-check found-key evidence with the A-array/B-array binary artifact."""
    entries = parse_found_keys(output, classic_size)
    return _validate_key_artifact(path, entries, classic_size)


def parse_recovered_keys(output: str, classic_size: str = "1k") -> list[dict]:
    """Parse only autopwn's last table, including its documented recovery legends.

    Earlier progress tables can withdraw candidate keys. They must never be
    unioned with the final table, especially when FF is also an unknown-key fill.
    """
    if classic_size not in CLASSIC_GEOMETRIES:
        raise ValueError(tr('Недопустимый профиль Classic.'))
    text = _text(output)
    headers = list(_RECOVERY_TABLE_HEADER.finditer(text))
    if not headers:
        return []
    tail = text[headers[-1].end():]
    count = CLASSIC_GEOMETRIES[classic_size][0]
    found = {}
    sectors_seen = set()
    for match in _KEY_ROW.finditer(tail):
        sector, block = int(match.group(1)), int(match.group(2))
        if sector >= count or block != sector_trailer(sector) or sector in sectors_seen:
            raise ValueError(tr('Последняя таблица восстановления не соответствует геометрии карты.'))
        sectors_seen.add(sector)
        for kind, key, result in [("A", match.group(3), match.group(4)), ("B", match.group(5), match.group(6))]:
            if result in _RECOVERY_METHODS and re.fullmatch(r"[0-9A-Fa-f]{12}", key):
                found[(sector, kind)] = (key.upper(), _RECOVERY_METHODS[result])
    return [{"sector": sector, "type": kind, "key_hex": key, "method": method}
            for (sector, kind), (key, method) in sorted(found.items())]


def validate_recovery_key_file(path: Path, output: str, classic_size: str = "1k") -> list[dict]:
    entries = parse_recovered_keys(output, classic_size)
    return _validate_key_artifact(path, entries, classic_size)


def _validate_key_artifact(path: Path, entries: list[dict], classic_size: str) -> list[dict]:
    count = CLASSIC_GEOMETRIES[classic_size][0]
    path = Path(path)
    if path.stat().st_size != count * 12:
        raise ValueError(tr('Размер файла ключей не соответствует карте.'))
    raw = path.read_bytes()
    for entry in entries:
        offset = entry["sector"] * 6 + (count * 6 if entry["type"] == "B" else 0)
        if raw[offset:offset + 6].hex().upper() != entry["key_hex"]:
            raise ValueError(tr('Файл ключей не совпадает с подтверждённой таблицей клиента.'))
    if not entries:
        raise ValueError(tr('Клиент не подтвердил ни одного ключа.'))
    return entries


def validate_read_dump(path: Path, detection: Detection, output: str, *, returncode: int = 0,
                       confirmed_uid: str | None = None) -> DumpData:
    """Require a matching UID, structural data and explicit successful read evidence."""
    if returncode != 0:
        raise ValueError(tr('Клиент не завершил чтение успешно.'))
    text = _text(output)
    if _READ_FAILURE.search(text):
        raise ValueError(tr('Чтение неполное или содержит ошибки. Непрочитанные области не будут сохранены как нули.'))
    if "[Вывод сокращён" in text:
        raise ValueError(tr('Журнал обрезан; полноту чтения нельзя подтвердить.'))
    dump = parse_dump(path)
    source = dump.metadata.get("source_json", {})
    card = source.get("Card", {})
    if not isinstance(card, dict):
        raise ValueError(tr('Дамп не содержит корректные метаданные Card.'))
    uid = card.get("UID", "")
    expected_uid = _uid(detection.uid)
    if confirmed_uid is not None and _uid(confirmed_uid) != expected_uid:
        raise ValueError(tr('UID изменился после чтения. Дамп не будет импортирован.'))
    uid_source = "json_header_and_post_read_rf_select" if confirmed_uid is not None else "json_header"
    header_mismatch = None
    if not uid or _uid(uid) != expected_uid:
        declared = _uid(uid) if uid else ""
        block0 = dump.data[:16]
        # RRG v4.23346 fileutils.c:4918-4931 reconstructs Card from block 0,
        # not from the RF selection saved by mfc_read_tag. A 7-byte-UID clone
        # whose block 0 also passes the old four-byte BCC/ATQA heuristic gets
        # a truncated Card.UID. Accept only this exact shape, and only with a
        # separate post-read RF select confirming the full initial UID.
        four_byte_layout = (
            len(block0) == 16
            and (block0[0] ^ block0[1] ^ block0[2] ^ block0[3]) == block0[4]
            and (block0[6] & 0xC0) == 0
            and str(card.get("SAK", "")).upper() == block0[5:6].hex().upper()
            and str(card.get("ATQA", "")).upper() == block0[6:8].hex().upper()
        )
        verified_truncation = (
            detection.family == "mfc" and source.get("FileType") == "mfc v2"
            and len(expected_uid) == 14 and len(declared) == 8
            and expected_uid.startswith(declared)
            and block0[:7].hex().upper() == expected_uid
            and confirmed_uid is not None and four_byte_layout
        )
        if not verified_truncation:
            raise ValueError(tr('UID дампа не совпадает с определённой картой. Повторите определение.'))
        uid_source = "rf_select_before_and_after_read"
        header_mismatch = {
            "kind": "rrg_mfc_v2_four_byte_block0_heuristic",
            "declared_uid": declared, "confirmed_rf_uid": expected_uid,
            "original_json_header_preserved": True,
            "evidence": "block0 starts with the full RF UID and also matches RRG's four-byte BCC/ATQA heuristic",
        }
    if detection.family == "mfc":
        if detection.classic_size not in CLASSIC_GEOMETRIES:
            raise ValueError(tr('Размер Classic не определён.'))
        sectors, size = CLASSIC_GEOMETRIES[detection.classic_size]
        if source.get("FileType") not in {"mfcard", "mfc v2", "mfc v3"} or dump.block_size != 16 or len(dump.data) != size:
            raise ValueError(tr('Формат или размер дампа не соответствует профилю Classic.'))
        expected = {(sector, block) for sector in range(sectors) for block in range(4 if sector < 32 else 16)}
        observed = {(int(sector), int(block)) for sector, block in _BLOCK_OK.findall(text)}
        if observed != expected:
            raise ValueError(tr('Подтверждено чтение {0} из {1} блоков. Неполный дамп не импортируется.', f'{len(observed & expected)}', f'{len(expected)}'))
        validation = {"kind": "mfc", "verified_blocks": len(expected), "expected_bytes": size}
    elif detection.family == "mfu":
        if source.get("FileType") != "mfu" or dump.block_size != 4:
            raise ValueError(tr('Ожидался JSON Ultralight/NTAG с четырёхбайтными страницами.'))
        if not re.search(r"reading tag memory", text, re.I) or not re.search(r"saved.*json", text, re.I):
            raise ValueError(tr('Нет подтверждения завершённого чтения и сохранения JSON.'))
        reported_type = re.search(r"\bTYPE:\s*([^\n]+)", text, re.I)
        type_text = detection.protocol + ("\n" + reported_type.group(1) if reported_type else "")
        if reported_type and re.search(r"\bunknown\b", reported_type.group(1), re.I):
            raise ValueError(tr('Клиент не определил модель Ultralight/NTAG; полноту памяти нельзя подтвердить.'))
        model = re.search(r"NTAG\s*(213|215|216)(?:\b|[FC]|TT)", type_text, re.I)
        if model and len(dump.data) != {"213": 180, "215": 540, "216": 924}[model.group(1)]:
            raise ValueError(tr('Количество страниц не соответствует определённой модели NTAG.'))
        # A PM3 MFU BIN contains a 56-byte header; page 0 starts after it.
        # Cross-check the paired artifact when present, never import its header
        # as card memory or trust a claimed page count without comparing bytes.
        binary_path = Path(path).with_suffix(".bin")
        if binary_path.exists():
            if binary_path.stat().st_size != len(dump.data) + 56:
                raise ValueError(tr('Размер MFU BIN не соответствует JSON и 56-байтному заголовку.'))
            binary = binary_path.read_bytes()
            if binary[11] + 1 != len(dump.data) // 4 or binary[56:] != dump.data:
                raise ValueError(tr('MFU BIN/JSON содержат разные страницы или неверный счётчик страниц.'))
        validation = {"kind": "mfu", "verified_pages": len(dump.data) // 4,
                      "capacity_check": "model_size" if model else "client_no_partial_warning",
                      "secret_fields_may_be_unreadable": True}
    else:
        raise ValueError(tr('Для этого семейства чтение памяти ещё не поддерживается.'))
    validation["uid"] = expected_uid
    validation["uid_source"] = uid_source
    if header_mismatch is not None:
        validation["header_mismatch"] = header_mismatch
    dump.metadata["read_validation"] = validation
    return dump


def identity_dump(detection: Detection) -> DumpData:
    if detection.family != "em410x" or not re.fullmatch(r"[0-9A-Fa-f]{10}", detection.uid):
        raise ValueError(tr('Ожидался подтверждённый EM410x ID.'))
    return DumpData(bytes.fromhex(detection.uid), "bin",
                    {"record_kind": "identifier", "protocol": "EM410x", "uid": detection.uid,
                     "note": tr('Только идентификатор; не является полным дампом памяти чипа.')}, 5)
