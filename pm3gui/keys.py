"""Local MIFARE Classic key candidates, provenance and authentication results.

Flipper dictionary/cache/card formats follow flipperzero-firmware's keys_dict.c,
mf_classic_key_cache.c and mf_classic.c. Imported material is a candidate until
the caller explicitly records positive evidence returned by the device/client.
That evidence can be authentication or a key field read with permitted access;
it must not be inferred from binary-file padding alone.
Flipper .shd files use the same card format, and are imported independently of
their .nfc originals: shadows can contain updated reads or emulation changes.
"""
from __future__ import annotations
from .i18n import tr

from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

MAX_IMPORT_BYTES = 8 * 1024 * 1024
MAX_IMPORT_KEYS = 100000
_HEX12 = re.compile(r"[0-9a-fA-F]{12}\Z")
_HEX_BYTE = re.compile(r"[0-9a-fA-F]{2}\Z")
_GEOMETRY = {"MINI": (5, 20), "1K": (16, 64), "4K": (40, 256)}
_MOSCOW = timezone(timedelta(hours=3), "МСК")


def format_key_timestamp(value: str | None) -> str:
    """Display stored UTC timestamps in the GUI's Moscow (UTC+3) timezone.

    Older database strings without an explicit offset were also written in
    UTC. Missing or malformed dates, including dates without a time, remain
    unknown instead of acquiring a made-up time or the host's local timezone.
    """
    if not isinstance(value, str):
        return "—"
    text = value.strip()
    if text.endswith(" UTC"):
        text = text[:-4] + "+00:00"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?", text):
        return "—"
    try:
        timestamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(_MOSCOW).strftime(tr('%d.%m.%Y %H:%M:%S'))
    except (ValueError, OverflowError):
        return "—"


def normalize_key(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError(tr('Ключ MIFARE Classic должен быть строкой из 12 HEX-цифр.'))
    compact = re.sub(r"[ \t\r\n]", "", value)
    if not _HEX12.fullmatch(compact):
        raise ValueError(tr('Ключ MIFARE Classic должен содержать ровно 12 HEX-цифр (6 байт).'))
    return compact.upper()


def _dictionary_records(text: str) -> list[str]:
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise ValueError(tr('Словарь превышает ограничение 8 МиБ.'))
    result = []
    for number, original in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        line = original.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            result.append(normalize_key(line))
        except ValueError as exc:
            raise ValueError(tr('Строка {0}: ожидается один 6-байтный HEX-ключ.', f'{number}')) from exc
        if len(result) > MAX_IMPORT_KEYS:
            raise ValueError(tr('За один импорт допускается не более 100000 ключей.'))
    return result


def parse_dictionary(text: str) -> list[str]:
    """Return unique normalized key candidates, preserving first-seen order."""
    return list(dict.fromkeys(_dictionary_records(text)))


def _fields(text: str) -> dict[str, str]:
    if len(text.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise ValueError(tr('Файл превышает ограничение 8 МиБ.'))
    fields = {}
    for number, raw in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(tr('Строка {0}: некорректное поле Flipper NFC.', f'{number}'))
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if not key or key in fields:
            raise ValueError(tr('Строка {0}: пустое или повторяющееся поле Flipper NFC.', f'{number}'))
        fields[key] = value
    return fields


def _geometry(fields: dict) -> tuple[int, int]:
    kind = fields.get("Mifare Classic type", "").upper()
    if kind not in _GEOMETRY:
        raise ValueError(tr('Поддерживаются карты Flipper MIFARE Classic MINI, 1K и 4K.'))
    return _GEOMETRY[kind]


def _uid(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError(tr('UID должен быть HEX-строкой.'))
    result = re.sub(r"[ \t]", "", value).upper()
    if len(result) not in (8, 14, 20) or re.fullmatch(r"[0-9A-F]+", result) is None:
        raise ValueError(tr('UID должен содержать 4, 7 или 10 байт HEX.'))
    return result


def _card_entries(text: str) -> tuple[list[dict], int, str]:
    fields = _fields(text)
    if fields.get("Filetype") != "Flipper NFC device":
        raise ValueError(tr('Ожидается Filetype: Flipper NFC device.'))
    # File version 2 is still supported by Flipper's legacy loader; its ATQA
    # ordering differs, but the independently versioned Classic key fields do
    # not. Keep requiring Classic data format 2 below to reject old fillers.
    if fields.get("Version") not in {"2", "3", "4"}:
        raise ValueError(tr('Поддерживаются версии 2, 3 и 4 файла Flipper NFC device.'))
    if fields.get("Device type", "").casefold() != "mifare classic":
        raise ValueError(tr('Этот NFC-файл не является MIFARE Classic.'))
    if fields.get("Data format version") != "2":
        raise ValueError(tr('Для ключей нужен Data format version: 2. Старые NFC-файлы не отличают неизвестные ключи от заполнителей.'))
    sectors, blocks = _geometry(fields)
    uid = _uid(fields["UID"]) if "UID" in fields else ""
    trailers = {4 * sector + 3 if sector < 32 else 128 + 16 * (sector - 32) + 15: sector
                for sector in range(sectors)}
    entries, unknown = [], 0
    found = set()
    for field, value in fields.items():
        if not field.startswith("Block "):
            continue
        suffix = field[6:]
        if re.fullmatch(r"0|[1-9][0-9]*", suffix) is None or int(suffix) >= blocks:
            raise ValueError(tr('Номер блока Flipper NFC не соответствует типу карты.'))
        block = int(suffix)
        tokens = value.split()
        if len(tokens) != 16 or any(token != "??" and _HEX_BYTE.fullmatch(token) is None for token in tokens):
            raise ValueError(tr('Блок {0}: ожидается 16 HEX-байт или маркеров ??.', f'{block}'))
        if block not in trailers:
            continue
        found.add(block)
        for key_type, portion in [("A", tokens[:6]), ("B", tokens[10:])]:
            if "??" in portion:
                unknown += 1
            else:
                entries.append({"sector": trailers[block], "type": key_type,
                                "key_hex": normalize_key("".join(portion))})
    unknown += 2 * (sectors - len(found))
    return entries, unknown, uid


def parse_flipper_nfc_keys(text: str) -> list[str]:
    entries, _, _ = _card_entries(text)
    return list(dict.fromkeys(entry["key_hex"] for entry in entries))


def _cache_entries(text: str) -> tuple[list[dict], int]:
    fields = _fields(text)
    if fields.get("Filetype") != "Flipper NFC keys" or fields.get("Version") != "1":
        raise ValueError(tr('Поддерживается Flipper NFC keys версии 1.'))
    sectors, _ = _geometry(fields)
    entries = []
    expected_fields = set()
    for key_type in ("A", "B"):
        value = fields.get(f"Key {key_type} map", "")
        if re.fullmatch(r"[0-9a-fA-F]{16}", value) is None:
            raise ValueError(tr('Карта найденных ключей Flipper должна содержать 16 HEX-цифр.'))
        mask = int(value, 16)
        if mask >> sectors:
            raise ValueError(tr('Карта ключей Flipper выходит за пределы секторов карты.'))
        for sector in range(sectors):
            if not mask & (1 << sector):
                continue
            field = f"Key {key_type} sector {sector}"
            expected_fields.add(field)
            if field not in fields:
                raise ValueError(tr('В кэше Flipper отсутствует ключ, отмеченный в карте ключей.'))
            entries.append({"sector": sector, "type": key_type, "key_hex": normalize_key(fields[field])})
    actual_fields = {field for field in fields if field.startswith(("Key A sector ", "Key B sector "))}
    if actual_fields != expected_fields:
        raise ValueError(tr('Поля ключей Flipper не согласованы с картами найденных ключей.'))
    return entries, sectors * 2 - len(entries)


def parse_flipper_key_cache(text: str) -> list[str]:
    entries, _ = _cache_entries(text)
    return list(dict.fromkeys(entry["key_hex"] for entry in entries))


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class KeyLibrary:
    """Own tables can coexist with the card Library in the same SQLite file."""
    def __init__(self, db_path):
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS mf_keys(
                id TEXT PRIMARY KEY, key_hex TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL,
                verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0,1))
            );
            CREATE TABLE IF NOT EXISTS mf_key_origins(
                key_id TEXT NOT NULL REFERENCES mf_keys(id) ON DELETE CASCADE,
                label TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(key_id,label,source)
            );
            CREATE TABLE IF NOT EXISTS mf_card_keys(
                id TEXT PRIMARY KEY, uid TEXT NOT NULL, sector INTEGER NOT NULL,
                key_type TEXT NOT NULL, key_id TEXT NOT NULL REFERENCES mf_keys(id) ON DELETE CASCADE,
                source TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, verified_at TEXT,
                UNIQUE(uid,sector,key_type,key_id)
            );
            CREATE INDEX IF NOT EXISTS mf_card_keys_uid ON mf_card_keys(uid,sector,key_type);
        """)

    def close(self):
        self.connection.close()

    def list_keys(self, query=""):
        rows = self.connection.execute("SELECT * FROM mf_keys ORDER BY created_at DESC,rowid DESC").fetchall()
        origins = {}
        for row in self.connection.execute("SELECT * FROM mf_key_origins ORDER BY created_at"):
            origins.setdefault(row["key_id"], []).append(dict(row))
        query = str(query).strip().casefold()
        result = []
        for row in rows:
            item = dict(row)
            item["verified"] = bool(item["verified"])
            item["origins"] = origins.get(item["id"], [])
            item["provenance_count"] = len(item["origins"])
            search = " ".join([item["key_hex"], item["label"], item["source"],
                               *[o["label"] + " " + o["source"] for o in item["origins"]]])
            if not query or query in search.casefold():
                result.append(item)
        return result

    def _add(self, values, label, source, verified):
        added = duplicates = 0
        for value in values:
            row = self.connection.execute("SELECT id FROM mf_keys WHERE key_hex=?", (value,)).fetchone()
            if row is None:
                key_id = str(uuid4())
                self.connection.execute("INSERT INTO mf_keys VALUES(?,?,?,?,?,?)",
                                        (key_id, value, label, source, _now(), int(verified)))
                added += 1
            else:
                key_id = row["id"]
                duplicates += 1
                if verified:
                    self.connection.execute("UPDATE mf_keys SET verified=1 WHERE id=?", (key_id,))
            self.connection.execute("INSERT OR IGNORE INTO mf_key_origins VALUES(?,?,?,?)",
                                    (key_id, label, source, _now()))
        total = self.connection.execute("SELECT count(*) FROM mf_keys").fetchone()[0]
        return {"added": added, "duplicates": duplicates, "total": total}

    def add_keys(self, keys, label="", source="", *, verified=False):
        if isinstance(keys, (str, bytes)):
            raise ValueError(tr('Передайте список ключей; для текста используйте parse_dictionary.'))
        values = []
        for key in keys:
            values.append(normalize_key(key))
            if len(values) > MAX_IMPORT_KEYS:
                raise ValueError(tr('За один импорт допускается не более 100000 ключей.'))
        with self.connection:
            return self._add(values, str(label), str(source), bool(verified))

    def _store_context(self, uid, entries, source, verified):
        for entry in entries:
            key_id = self.connection.execute("SELECT id FROM mf_keys WHERE key_hex=?", (entry["key_hex"],)).fetchone()[0]
            timestamp = _now()
            self.connection.execute("""
                INSERT INTO mf_card_keys VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(uid,sector,key_type,key_id) DO UPDATE SET
                    verified=MAX(mf_card_keys.verified,excluded.verified),
                    source=CASE WHEN excluded.verified=1 THEN excluded.source ELSE mf_card_keys.source END,
                    verified_at=CASE WHEN excluded.verified=1 THEN excluded.verified_at ELSE mf_card_keys.verified_at END
            """, (str(uuid4()), uid, entry["sector"], entry["type"], key_id, source,
                  int(verified), timestamp, timestamp if verified else None))

    def record_verified_keys(self, uid, entries, *, source=None):
        """Record client-confirmed card keys, which need not all be usable for authentication."""
        if source is None:source=tr('Proxmark3: подтверждено клиентом')
        uid = _uid(uid)
        checked = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(tr('Результат проверки ключа должен содержать сектор, тип и HEX-ключ.'))
            sector = entry.get("sector")
            key_type = str(entry.get("type", entry.get("key_type", ""))).upper()
            if type(sector) is not int or not 0 <= sector < 40 or key_type not in {"A", "B"}:
                raise ValueError(tr('Допустимы секторы 0–39 и тип ключа A или B.'))
            value = normalize_key(entry.get("key_hex"))
            slot = (sector, key_type)
            if slot in checked and checked[slot]["key_hex"] != value:
                raise ValueError(tr('В одном результате указаны разные ключи для одного сектора и типа.'))
            checked[slot] = {"sector": sector, "type": key_type, "key_hex": value}
        records = list(checked.values())
        label = f"UID {uid}"
        with self.connection:
            report = self._add([entry["key_hex"] for entry in records], label, str(source), True)
            self._store_context(uid, records, str(source), True)
        return {**report, "recorded": len(records), "uid": uid}

    def list_card_keys(self, uid):
        uid = _uid(uid)
        rows = self.connection.execute("""
            SELECT c.id,c.uid,c.sector,c.key_type,c.key_type AS type,k.key_hex,
                   c.source,c.verified,c.created_at,c.verified_at
            FROM mf_card_keys c JOIN mf_keys k ON k.id=c.key_id
            WHERE c.uid=? ORDER BY c.sector,c.key_type,c.verified_at DESC,c.created_at DESC
        """, (uid,)).fetchall()
        return [{**dict(row), "verified": bool(row["verified"])} for row in rows]

    def record_candidate_keys(self, uid, entries, *, source='Imported sector keys'):
        """Imported dump/key.bin bytes are candidates, never card authentication."""
        uid = _uid(uid)
        checked = {}
        for entry in entries:
            sector, kind = entry.get('sector'), entry.get('type', entry.get('key_type'))
            if type(sector) is not int or not 0 <= sector < 40 or kind not in {'A', 'B'}:
                raise ValueError('Invalid sector or key type.')
            key = normalize_key(entry.get('key_hex'))
            if (sector, kind) in checked and checked[sector, kind]['key_hex'] != key:
                raise ValueError('Conflicting imported sector keys.')
            checked[sector, kind] = {'sector': sector, 'type': kind, 'key_hex': key}
        values = list(checked.values())
        with self.connection:
            report = self._add([e['key_hex'] for e in values], 'UID ' + uid, str(source), False)
            self._store_context(uid, values, str(source), False)
        return {**report, 'uid': uid, 'imported': len(values), 'verified': False}

    def import_file(self, path):
        path = Path(path)
        shadow = path.suffix.lower() == ".shd"
        if path.suffix.lower() not in {".nfc", ".shd", ".keys", ".dic", ".dict", ".txt"}:
            raise ValueError(tr('Поддерживаются .nfc, .shd, .keys, .dic, .dict и .txt. Бинарные key.bin не содержат признаков найденных ключей.'))
        if path.stat().st_size > MAX_IMPORT_BYTES:
            raise ValueError(tr('Файл превышает ограничение 8 МиБ.'))
        try:
            text = path.read_bytes().decode("utf-8-sig")
        except UnicodeError as exc:
            raise ValueError(tr('Текстовый словарь или файл Flipper должен быть в UTF-8/ASCII.')) from exc
        first = next((line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")), "")
        if shadow and (not first.startswith("Filetype:") or first.split(":", 1)[1].strip() != "Flipper NFC device"):
            raise ValueError(tr('Shadow .shd должен содержать Filetype: Flipper NFC device и данные MIFARE Classic.'))
        entries, unknown, uid = [], 0, ""
        if first.startswith("Filetype:"):
            filetype = first.split(":", 1)[1].strip()
            if filetype == "Flipper NFC device":
                entries, unknown, uid = _card_entries(text)
                fmt = "flipper-nfc"
            elif filetype == "Flipper NFC keys":
                entries, unknown = _cache_entries(text)
                try:
                    uid = _uid(path.stem)
                except ValueError:
                    pass
                fmt = "flipper-cache"
            else:
                raise ValueError(tr('Неизвестный Filetype файла Flipper.'))
            values = [entry["key_hex"] for entry in entries]
        else:
            values = _dictionary_records(text)
            fmt = "dictionary"
        source = tr('Импорт: {0}', f'{path.name}')
        with self.connection:
            report = self._add(values, path.stem, source, False)
            if uid:
                self._store_context(uid, entries, source, False)
        return {**report, "format": fmt, "shadow": shadow, "skipped_unknown": unknown,
                "imported": len(values), "uid": uid}

    def export_dictionary(self, path, flipper=True):
        keys = self.connection.execute("SELECT key_hex FROM mf_keys ORDER BY key_hex").fetchall()
        header = "# MIFARE Classic user keys\n" if flipper else ""
        text = header + "".join(row["key_hex"] + "\n" for row in keys)
        # Flipper consumes ASCII with LF and no BOM, including for its .nfc user dictionary.
        Path(path).write_bytes(text.encode("ascii"))

    def delete_keys(self, ids):
        if isinstance(ids, (str, bytes)):
            raise ValueError(tr('Передайте список идентификаторов ключей.'))
        ids = list(ids)
        if any(not isinstance(value, str) for value in ids):
            raise ValueError(tr('Идентификаторы ключей должны быть строками.'))
        ids = list(dict.fromkeys(ids))
        with self.connection:
            deleted = 0
            for key_id in ids:
                deleted += self.connection.execute("DELETE FROM mf_keys WHERE id=?", (key_id,)).rowcount
        return deleted
