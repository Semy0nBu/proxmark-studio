"""Lossless local card library and deliberately conservative dump conversion.

JSON ``data`` contains memory blocks only. Protocol headers remain in metadata;
it must not silently be presented as a PM3 binary dump with those headers.
"""

from __future__ import annotations
from .i18n import tr

import base64
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4
from .card_band import card_band


MAX_DUMP_BYTES = 16 * 1024 * 1024
_HEX = re.compile(r"[0-9a-fA-F \t\r\n\v\f]*\Z")
_INDEX = re.compile(r"0|[1-9][0-9]*\Z")
_JSON_FORMATS = {"bin", "mfd", "eml", "json"}
_BLOCK_SIZES = {
    "mfcard": 16, "mfc v2": 16, "mfc v3": 16,
    "mfu": 4, "fudan": 4, "hitag": 4, "hitag1": 4, "hitag2": 4,
    "hitags": 4, "hitagu": 4, "t55x7": 4, "t5555": 4,
    "iclass": 8, "EM4205/EM4305": 4, "EM4469/EM4569": 4,
    "EM4X50": 4, "legic v2": 16, "topaz": 8, "14b v2": 4,
    "lto": 32, "cryptorf": 8, "ndef": 16,
}
# Verified flat loadFileJSONex branches in RRG v4.23346 fileutils.c.
# Unknown or header-bearing protocols must remain JSON: having a ``blocks``
# object alone does not prove that their PM3 BIN representation is flat.
_FLAT_JSON_BINARY_TYPES = {
    "raw", "mfcard", "mfc v2", "fudan", "hitag", "hitag1", "hitag2",
    "hitags", "hitagu", "iclass", "t55x7", "EM4205/EM4305",
    "EM4469/EM4569", "EM4X50", "legic v2", "14b v2", "lto", "cryptorf", "ndef",
}


@dataclass
class DumpData:
    data: bytes
    format: str
    metadata: dict
    block_size: int


def parse_hex(text: str) -> bytes:
    """Parse bare hex and ASCII whitespace; never discard unexpected text."""
    if not isinstance(text, str) or _HEX.fullmatch(text) is None:
        raise ValueError(tr('HEX должен содержать только цифры 0–9, A–F и пробелы.'))
    compact = re.sub(r"[ \t\r\n\v\f]", "", text)
    if len(compact) % 2:
        raise ValueError(tr('В HEX должно быть чётное число цифр (две на байт).'))
    return bytes.fromhex(compact)


def decode_bytes(data: bytes, encoding: str) -> str:
    return bytes(data).decode(encoding, errors="strict")


def encode_text(text: str, encoding: str) -> bytes:
    return text.encode(encoding, errors="strict")


def _checked(dump: DumpData) -> DumpData:
    if isinstance(dump, DumpData) and isinstance(dump.metadata, dict) and 'structured_document' in dump.metadata:
        expected = json.dumps(dump.metadata['structured_document'], ensure_ascii=False, sort_keys=True,
                              allow_nan=False, separators=(',', ':')).encode('utf-8')
        if dump.data != expected:
            raise ValueError('Structured snapshots must be edited through their record editor, not raw HEX.')
    if not isinstance(dump, DumpData) or not isinstance(dump.data, bytes):
        raise ValueError(tr('Дамп должен содержать bytes.'))
    if not dump.data or len(dump.data) > MAX_DUMP_BYTES:
        raise ValueError(tr('Размер дампа должен быть от 1 байта до 16 МиБ.'))
    if dump.format not in _JSON_FORMATS:
        raise ValueError(tr('Неизвестный формат дампа.'))
    if not isinstance(dump.metadata, dict):
        raise ValueError(tr('Метаданные дампа должны быть объектом.'))
    if type(dump.block_size) is not int or not 1 <= dump.block_size <= 65536:
        raise ValueError(tr('Недопустимый размер блока.'))
    try:
        json.dumps(dump.metadata, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(tr('Метаданные должны сериализоваться в JSON.')) from exc
    return dump


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(tr('Повторяющийся ключ JSON: {0}', f'{key}'))
        result[key] = value
    return result


def _read_json(raw: bytes) -> dict:
    try:
        root = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(tr('Не удалось прочитать JSON дампа в UTF-8.')) from exc
    if not isinstance(root, dict) or not isinstance(root.get("FileType"), str):
        from .studio_data import is_felica_document
        if not isinstance(root, dict) or 'FileType' in root or not is_felica_document(root):
            raise ValueError(tr('В PM3 JSON требуется строковое поле FileType.'))
    return root


def parse_dump(path: Path) -> DumpData:
    path = Path(path)
    suffix = path.suffix.lower().lstrip(".")
    if suffix not in _JSON_FORMATS:
        raise ValueError(tr('Поддерживаются .bin, .mfd, .eml и .json.'))
    if path.stat().st_size > MAX_DUMP_BYTES:
        raise ValueError(tr('Файл превышает ограничение 16 МиБ.'))
    raw = path.read_bytes()
    metadata = {"source_name": path.name}
    if suffix in {"bin", "mfd"}:
        # Length alone cannot identify a protocol or a protocol-specific header.
        return _checked(_restore_partial(DumpData(raw, suffix, metadata, 16), path))
    if suffix == "eml":
        try:
            # Upstream skips full '#' comment lines before parsing HEX. Keep
            # comments byte-for-byte even if their prose is not ASCII.
            lines = [line.strip().decode("ascii") for line in raw.splitlines()
                     if line.strip() and not line.lstrip().startswith(b"#")]
        except UnicodeDecodeError as exc:
            raise ValueError(tr('EML должен содержать только ASCII HEX.')) from exc
        blocks = [parse_hex(line) for line in lines]
        if not blocks or not blocks[0] or any(len(b) != len(blocks[0]) for b in blocks):
            raise ValueError(tr('EML должен содержать непустые HEX строки одинаковой длины.'))
        metadata["source_eml_base64"] = base64.b64encode(raw).decode("ascii")
        metadata["source_sha256"] = hashlib.sha256(b"".join(blocks)).hexdigest()
        return _checked(_restore_partial(DumpData(b"".join(blocks), suffix, metadata, len(blocks[0])), path))
    root = _read_json(raw)
    if 'FileType' not in root:
        from .studio_data import structured_dump
        return _checked(structured_dump(root,'FeliCa',root['systems'][0]['idm']))
    if root['FileType'] == 'ProxmarkStudio.Structured v1':
        from .studio_data import structured_dump
        if 'Document' not in root or not isinstance(root.get('Profile'), str):
            raise ValueError('Invalid structured snapshot envelope.')
        return _checked(structured_dump(root['Document'], root['Profile'], str(root.get('UID', '')),
                                        str(root.get('SnapshotKind', 'structured-card'))))
    metadata["source_json"] = root
    # Keep an optional UTF-8 BOM too, so an unchanged export is byte-for-byte.
    metadata["source_json_text"] = raw.decode("utf-8")
    if root["FileType"] == "raw":
        if not isinstance(root.get("raw"), str):
            raise ValueError(tr('PM3 raw JSON требует HEX строку в поле raw.'))
        data = parse_hex(root["raw"])
        block_size = 16
    else:
        blocks = root.get("blocks")
        if not isinstance(blocks, dict) or not blocks:
            raise ValueError(tr('Этот JSON не содержит поддерживаемую непустую карту blocks.'))
        if any(not isinstance(key, str) or _INDEX.fullmatch(key) is None for key in blocks):
            raise ValueError(tr('Ключи blocks должны быть десятичными номерами без ведущих нулей.'))
        indices = sorted(int(key) for key in blocks)
        if indices != list(range(len(indices))):
            raise ValueError(tr('Дамп содержит пропуски блоков. Они не будут заменены нулями.'))
        values = [parse_hex(blocks[str(i)]) for i in indices]
        block_size = len(values[0])
        legic_22 = root['FileType']=='legic v2' and [len(b) for b in values]==[16,6]
        if not block_size or any(len(block) != block_size for block in values) and not legic_22:
            raise ValueError(tr('Все блоки JSON должны иметь одинаковый непустой размер.'))
        expected = _BLOCK_SIZES.get(root["FileType"])
        if expected is not None and expected != block_size:
            raise ValueError(tr('Для {0} ожидается {1} байт в блоке.', f"{root['FileType']}", f'{expected}'))
        data = b"".join(values)
    metadata["source_sha256"] = hashlib.sha256(data).hexdigest()
    result = DumpData(data, suffix, metadata, block_size)
    embedded = root.get('ProxmarkStudio')
    if isinstance(embedded, dict) and embedded.get('partial_dump') is not None:
        _attach_partial(result, embedded)
    return _checked(result)


def _attach_partial(dump, envelope):
    from .partial import validate_partial_metadata
    if envelope.get('sha256') != hashlib.sha256(dump.data).hexdigest():
        raise ValueError(tr('Карта пропусков относится к другим байтам дампа.'))
    dump.metadata['partial_dump'] = validate_partial_metadata(envelope['partial_dump'], dump.data)
    dump.block_size=dump.metadata['partial_dump']['block_size']


def _restore_partial(dump, path):
    sidecar = path.with_name(path.name + '.pm3meta.json')
    if sidecar.is_file():
        if sidecar.stat().st_size > MAX_DUMP_BYTES:
            raise ValueError(tr('Файл карты пропусков слишком велик.'))
        value = json.loads(sidecar.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError(tr('Некорректный файл карты пропусков.'))
        if value.get('partial_dump') is not None:
            _attach_partial(dump,value)
    return dump


def export_dump(dump: DumpData, path: Path) -> None:
    _checked(dump)
    path = Path(path)
    if 'structured_document' in dump.metadata:
        if path.suffix.lower() != '.json':
            raise ValueError('Export a structured snapshot as JSON; it is not a flat chip-memory dump.')
        value = {'FileType': 'ProxmarkStudio.Structured v1', 'Profile': dump.metadata.get('protocol', ''),
                 'UID': dump.metadata.get('uid', ''), 'SnapshotKind': dump.metadata.get('snapshot_kind', 'structured-card'),
                 'Document': dump.metadata['structured_document']}
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
        return
    suffix = path.suffix.lower().lstrip(".")
    if suffix not in _JSON_FORMATS:
        raise ValueError(tr('Поддерживаются .bin, .mfd, .eml и .json.'))
    source = dump.metadata.get("source_json")
    partial = dump.metadata.get('partial_dump')
    envelope = {'version':1,'sha256':hashlib.sha256(dump.data).hexdigest(),'partial_dump':partial}
    if partial is not None:
        from .partial import validate_partial_metadata
        validate_partial_metadata(partial,dump.data)
        source = deepcopy(source) if isinstance(source,dict) else {'Created':'pm3gui','FileType':'raw'}
        source['ProxmarkStudio'] = envelope
    unchanged = dump.metadata.get("source_sha256") == hashlib.sha256(dump.data).hexdigest()
    filetype = source.get("FileType", "") if isinstance(source, dict) else ""
    if suffix in {"bin", "mfd"}:
        if isinstance(source, dict) and filetype not in _FLAT_JSON_BINARY_TYPES:
            if filetype in {"mfu", "mfc v3", "topaz"} or filetype.startswith("15693"):
                raise ValueError(tr('{0}: JSON содержит память отдельно от служебного заголовка. Экспортируйте JSON; преобразование в PM3 BIN пока не поддерживается.', f'{filetype}'))
            raise ValueError(tr('{0}: структура PM3 BIN для этого FileType не проверена. Экспортируйте JSON; преобразование в PM3 BIN пока не поддерживается.', f'{filetype}'))
        raw = dump.data
    elif suffix == "eml":
        if unchanged and dump.format == "eml" and isinstance(dump.metadata.get("source_eml_base64"), str):
            raw = base64.b64decode(dump.metadata["source_eml_base64"], validate=True)
        elif unchanged and dump.format == "eml" and isinstance(dump.metadata.get("source_eml"), str):
            # Compatibility with revisions created before byte-preserved comments.
            raw = dump.metadata["source_eml"].encode("ascii")
        else:
            if len(dump.data) % dump.block_size:
                raise ValueError(tr('Для EML длина данных должна быть кратна размеру блока.'))
            raw = ("\n".join(dump.data[i:i + dump.block_size].hex().upper()
                             for i in range(0, len(dump.data), dump.block_size)) + "\n").encode("ascii")
    else:
        if unchanged and partial is None and isinstance(source, dict) and isinstance(dump.metadata.get("source_json_text"), str):
            raw = dump.metadata["source_json_text"].encode("utf-8")
        else:
            root = deepcopy(source) if isinstance(source, dict) else {"Created": "pm3gui", "FileType": "raw"}
            if root.get("FileType") == "raw":
                root["raw"] = dump.data.hex().upper()
            else:
                legic_22 = root.get('FileType')=='legic v2' and dump.block_size==16 and len(dump.data)==22
                if len(dump.data) % dump.block_size and not legic_22:
                    raise ValueError(tr('Для JSON blocks длина данных должна быть кратна размеру блока.'))
                root["blocks"] = {str(i // dump.block_size): dump.data[i:i + dump.block_size].hex().upper()
                                  for i in range(0, len(dump.data), dump.block_size)}
                # SectorKeys and descriptive access-condition strings are
                # derived from old memory and cannot remain trustworthy.
                if not unchanged and "SectorKeys" in root:
                    root.pop("SectorKeys")
            raw = (json.dumps(root, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    path.write_bytes(raw)
    if suffix != 'json':
        sidecar = path.with_name(path.name + '.pm3meta.json')
        if partial is not None or sidecar.exists():
            sidecar.write_text(json.dumps(envelope,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Library:
    """SQLite-backed library. Saved revisions are immutable through this API."""

    def __init__(self, db_path):
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS cards (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, uid TEXT NOT NULL,
                protocol TEXT NOT NULL, notes TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS revisions (
                id TEXT PRIMARY KEY, card_id TEXT NOT NULL REFERENCES cards(id),
                created_at TEXT NOT NULL, note TEXT NOT NULL, data BLOB NOT NULL,
                format TEXT NOT NULL, metadata TEXT NOT NULL,
                block_size INTEGER NOT NULL, sha256 TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS revisions_card ON revisions(card_id, created_at DESC);
            PRAGMA user_version=1;
        """)

    def close(self):
        self.connection.close()

    @staticmethod
    def _name(name):
        if not isinstance(name, str) or not name.strip():
            raise ValueError(tr('Введите название карты.'))
        return name.strip()

    def list_cards(self, query=""):
        query = str(query).casefold().strip()
        rows = self.connection.execute("""
            SELECT c.*,
              (SELECT length(data) FROM revisions r WHERE r.card_id=c.id
               ORDER BY created_at DESC, rowid DESC LIMIT 1) AS size,
              (SELECT count(*) FROM revisions r WHERE r.card_id=c.id) AS revision_count
              ,(SELECT json_extract(metadata,'$.partial_dump.version') FROM revisions r WHERE r.card_id=c.id
                ORDER BY created_at DESC,rowid DESC LIMIT 1) AS partial_version
              ,(SELECT json_object('rf_band',json_extract(metadata,'$.rf_band'),
                  'read_validation',json_object('kind',json_extract(metadata,'$.read_validation.kind')),
                  'source_json',json_object('FileType',json_extract(metadata,'$.source_json.FileType')),
                  'protocol',json_extract(metadata,'$.protocol')) FROM revisions r WHERE r.card_id=c.id
                ORDER BY created_at DESC,rowid DESC LIMIT 1) AS band_metadata
            FROM cards c ORDER BY c.updated_at DESC, c.rowid DESC
        """)
        result=[]
        for row in rows:
            card=dict(row);card['rf_band']=card_band(card,json.loads(card.pop('band_metadata') or '{}'))
            if not query or query in ' '.join(str(card[field]) for field in ('name','uid','protocol','notes')).casefold():result.append(card)
        return result

    def get_card(self, card_id):
        row = self.connection.execute("""
            SELECT c.*,
              (SELECT length(data) FROM revisions r WHERE r.card_id=c.id
               ORDER BY created_at DESC, rowid DESC LIMIT 1) AS size,
              (SELECT count(*) FROM revisions r WHERE r.card_id=c.id) AS revision_count
            FROM cards c WHERE c.id=?
        """, (card_id,)).fetchone()
        if row is None:
            raise KeyError(card_id)
        return dict(row)

    def _insert_revision(self, card_id, dump, note, timestamp):
        _checked(dump)
        revision_id = str(uuid4())
        self.connection.execute("""
            INSERT INTO revisions(id,card_id,created_at,note,data,format,metadata,block_size,sha256)
            VALUES(?,?,?,?,?,?,?,?,?)
        """, (revision_id, card_id, timestamp, str(note), sqlite3.Binary(dump.data), dump.format,
              json.dumps(dump.metadata, ensure_ascii=False, allow_nan=False), dump.block_size,
              hashlib.sha256(dump.data).hexdigest()))
        return revision_id

    def create_card(self, name, dump, *, uid="", protocol="", notes=""):
        name = self._name(name)
        _checked(dump)
        card_id = str(uuid4())
        timestamp = _now()
        with self.connection:
            self.connection.execute("INSERT INTO cards VALUES(?,?,?,?,?,?,?)",
                                    (card_id, name, str(uid), str(protocol), str(notes), timestamp, timestamp))
            self._insert_revision(card_id, dump, tr('Импорт'), timestamp)
        return card_id

    def update_card(self, card_id, *, name, uid, protocol, notes):
        self.get_card(card_id)
        with self.connection:
            self.connection.execute("UPDATE cards SET name=?,uid=?,protocol=?,notes=?,updated_at=? WHERE id=?",
                                    (self._name(name), str(uid), str(protocol), str(notes), _now(), card_id))

    def save_revision(self, card_id, dump, note=""):
        self.get_card(card_id)
        _checked(dump)
        timestamp = _now()
        with self.connection:
            revision_id = self._insert_revision(card_id, dump, note, timestamp)
            self.connection.execute("UPDATE cards SET updated_at=? WHERE id=?", (timestamp, card_id))
        return revision_id

    def list_revisions(self, card_id):
        self.get_card(card_id)
        return [dict(row) for row in self.connection.execute("""
            SELECT id, created_at, note, length(data) AS size, sha256 FROM revisions
            WHERE card_id=? ORDER BY created_at DESC, rowid DESC
        """, (card_id,))]

    def get_dump(self, card_id, revision_id=None):
        self.get_card(card_id)
        if revision_id is None:
            row = self.connection.execute("""
                SELECT * FROM revisions WHERE card_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1
            """, (card_id,)).fetchone()
        else:
            row = self.connection.execute("SELECT * FROM revisions WHERE card_id=? AND id=?",
                                          (card_id, revision_id)).fetchone()
        if row is None:
            raise KeyError(revision_id or card_id)
        data = bytes(row["data"])
        if hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise ValueError(tr('Контрольная сумма ревизии не совпадает: данные повреждены.'))
        return DumpData(data, row["format"], json.loads(row["metadata"]), row["block_size"])
