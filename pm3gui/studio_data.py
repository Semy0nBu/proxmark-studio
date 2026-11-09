"""Offline data models for the laboratory. None of these functions opens a port."""
from __future__ import annotations
from dataclasses import dataclass
from copy import deepcopy
from pathlib import Path
import cmath
import hashlib
import json
import math
import re
import struct
import wave

from .core import DumpData, parse_dump, parse_hex
from .operations import CLASSIC_GEOMETRIES, sector_trailer, _text
from .writing import access_conditions


def say(ru, en):
    from .i18n import language
    return en if language() == 'en' else ru


def block_selection(text, count):
    """Parse an explicit set, rejecting bad and out-of-bounds ranges."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError(say('Выберите блоки.', 'Select blocks.'))
    result = set()
    for token in re.split(r'[,;\s]+', text.strip()):
        match = re.fullmatch(r'(\d+)(?:-(\d+))?', token)
        if not match:
            raise ValueError(say('Блоки: 1, 4, 8-11.', 'Blocks: 1, 4, 8-11.'))
        first, last = int(match[1]), int(match[2] or match[1])
        if not 0 <= first <= last < count:
            raise ValueError(say('Блок выходит за пределы карты.', 'Block is outside this card.'))
        result.update(range(first, last + 1))
    return tuple(sorted(result))


def encode_access(conditions, gpb=0x69):
    if len(conditions) != 4 or any(type(c) is not int or not 0 <= c <= 7 for c in conditions):
        raise ValueError('Four access conditions in the range 0–7 are required.')
    if type(gpb) is not int or not 0 <= gpb <= 255:
        raise ValueError('GPB must be a byte.')
    c1 = sum(((c >> 2) & 1) << i for i, c in enumerate(conditions))
    c2 = sum(((c >> 1) & 1) << i for i, c in enumerate(conditions))
    c3 = sum((c & 1) << i for i, c in enumerate(conditions))
    return bytes([((c2 ^ 15) << 4) | (c1 ^ 15), (c1 << 4) | (c3 ^ 15), (c3 << 4) | c2, gpb])


# NXP MF1S50YYX_V1, tables 7/8. Order is C1,C2,C3, then data operations.
DATA_RIGHTS = {
    0: ('AB', 'AB', 'AB', 'AB'), 1: ('AB', '-', '-', 'AB'),
    2: ('AB', '-', '-', '-'), 3: ('B', 'B', '-', '-'),
    4: ('AB', 'B', '-', '-'), 5: ('B', '-', '-', '-'),
    6: ('AB', 'B', 'B', 'AB'), 7: ('-', '-', '-', '-'),
}
# Key A write, ACL read, ACL write, Key B read, Key B write.
TRAILER_RIGHTS = {
    0: ('A', 'A', '-', 'A', 'A'), 1: ('A', 'A', 'A', 'A', 'A'),
    2: ('-', 'A', '-', 'A', '-'), 3: ('B', 'AB', 'B', '-', 'B'),
    4: ('B', 'AB', '-', '-', 'B'), 5: ('-', 'AB', 'B', '-', '-'),
    6: ('-', 'AB', '-', '-', '-'), 7: ('-', 'AB', '-', '-', '-'),
}


def access_report(conditions):
    if len(conditions) != 4 or any(c not in DATA_RIGHTS for c in conditions):
        raise ValueError('Invalid access conditions.')
    readable_b = TRAILER_RIGHTS[conditions[3]][3] != '-'
    rows = []
    for c in conditions[:3]:
        rights = DATA_RIGHTS[c]
        rows.append(tuple((r.replace('B', '') or '-') if readable_b else r for r in rights))
    return {'data': rows, 'trailer': TRAILER_RIGHTS[conditions[3]], 'key_b_authenticates': not readable_b}


def decode_value_block(raw):
    if len(raw) != 16:
        raise ValueError('A Classic value block must contain 16 bytes.')
    if (raw[:4] != raw[8:12] or bytes(b ^ 255 for b in raw[:4]) != raw[4:8]
            or not (raw[12] == raw[14] == (raw[13] ^ 255) == (raw[15] ^ 255))):
        raise ValueError(say('Это некорректный value-блок.', 'Invalid value block redundancy.'))
    return int.from_bytes(raw[:4], 'little', signed=True), raw[12]


def key_entries(dump, include_zeros=False):
    size = next((size for size, (_, length) in CLASSIC_GEOMETRIES.items() if length == len(dump.data)), None)
    if size is None or dump.block_size != 16:
        raise ValueError('Select a Classic Mini/1K/2K/4K dump.')
    known = set(dump.metadata.get('partial_dump', {}).get('known_bytes', range(len(dump.data))))
    rows = []
    for sector in range(CLASSIC_GEOMETRIES[size][0]):
        for kind, delta in [('A', 0), ('B', 10)]:
            offset = sector_trailer(sector) * 16 + delta
            key = dump.data[offset:offset + 6]
            if all(i in known for i in range(offset, offset + 6)) and (include_zeros or any(key)):
                rows.append({'sector': sector, 'type': kind, 'key_hex': key.hex().upper()})
    return rows


def parse_binary_keys(raw, sectors, include_zeros=False):
    if sectors not in {5, 16, 32, 40} or len(raw) != sectors * 12:
        raise ValueError('key.bin size must match the selected Classic geometry (A keys, then B keys).')
    return [{'sector': s, 'type': k, 'key_hex': raw[offset:offset + 6].hex().upper()}
            for k, base in [('A', 0), ('B', sectors * 6)] for s in range(sectors)
            if (offset := base + s * 6) >= 0 and (include_zeros or any(raw[offset:offset + 6]))]


def binary_keys(entries, sectors):
    if sectors not in {5, 16, 32, 40}:
        raise ValueError('Invalid Classic geometry.')
    raw = bytearray(sectors * 12)
    slots = {}
    for entry in entries:
        s, k, h = entry['sector'], entry.get('type', entry.get('key_type')), entry['key_hex']
        if type(s) is not int or not 0 <= s < sectors or k not in {'A', 'B'} or not re.fullmatch('[0-9A-Fa-f]{12}', h):
            raise ValueError('Invalid sector key.')
        if (s, k) in slots and slots[s, k] != h.upper():
            raise ValueError('Conflicting keys for one sector. Select one value before export.')
        slots[s, k] = h.upper()
        offset = (s + (sectors if k == 'B' else 0)) * 6
        raw[offset:offset + 6] = bytes.fromhex(h)
    return bytes(raw), [{'sector': s, 'type': k} for s, k in sorted(slots)]


def merge_keys(dump, entries):
    result = deepcopy(dump)
    data = bytearray(result.data)
    geometry = next((s for s, n in CLASSIC_GEOMETRIES.values() if n == len(data)), None)
    if dump.block_size != 16 or geometry is None:
        raise ValueError('Select a Classic image.')
    for entry in entries:
        sector, key_type = entry['sector'], entry.get('type', entry.get('key_type'))
        if not 0 <= sector < geometry or key_type not in {'A', 'B'}:
            raise ValueError('Invalid sector key.')
        raw = parse_hex(entry['key_hex'])
        if len(raw) != 6:
            raise ValueError('A Classic key contains six bytes.')
        off = sector_trailer(sector) * 16 + (0 if key_type == 'A' else 10)
        data[off:off + 6] = raw
    result.data = bytes(data)
    return result


def compare_dumps(a, b):
    ak = set(a.metadata.get('partial_dump', {}).get('known_bytes', range(len(a.data))))
    bk = set(b.metadata.get('partial_dump', {}).get('known_bytes', range(len(b.data))))
    return [{'offset': i, 'a': a.data[i] if i < len(a.data) else None,
             'b': b.data[i] if i < len(b.data) else None, 'a_known': i in ak, 'b_known': i in bk}
            for i in range(max(len(a.data), len(b.data)))
            if (a.data[i:i + 1] != b.data[i:i + 1]) or ((i in ak) != (i in bk))]


URI_PREFIXES = ['', 'http://www.', 'https://www.', 'http://', 'https://', 'tel:', 'mailto:',
                'ftp://anonymous:anonymous@', 'ftp://ftp.', 'ftps://', 'sftp://', 'smb://',
                'nfs://', 'ftp://', 'dav://', 'news:', 'telnet://', 'imap:', 'rtsp://',
                'urn:', 'pop:', 'sip:', 'sips:', 'tftp:', 'btspp://', 'btl2cap://',
                'btgoep://', 'tcpobex://', 'irdaobex://', 'file://', 'urn:epc:id:',
                'urn:epc:tag:', 'urn:epc:pat:', 'urn:epc:raw:', 'urn:epc:', 'urn:nfc:']


def encode_ndef(records, tlv=False):
    if not records or len(records) > 100:
        raise ValueError('NDEF requires 1–100 records.')
    output = bytearray()
    for i, record in enumerate(records):
        kind, value = record['kind'], str(record['value'])
        if kind == 'uri':
            prefix = max((p for p in URI_PREFIXES if value.startswith(p)), key=len)
            tnf, typ, payload = 1, b'U', bytes([URI_PREFIXES.index(prefix)]) + value[len(prefix):].encode('utf-8')
        elif kind == 'text':
            lang = record.get('language', 'en').encode('ascii')
            if len(lang) > 63:
                raise ValueError('NDEF language code is too long.')
            tnf, typ, payload = 1, b'T', bytes([len(lang)]) + lang + value.encode('utf-8')
        elif kind == 'aar':
            tnf, typ, payload = 4, b'android.com:pkg', value.encode('utf-8')
        else:
            tnf, typ, payload = int(record.get('tnf', 2)), record['type'].encode('utf-8'), bytes.fromhex(value)
        if not 0 <= tnf <= 5 or len(typ) > 255 or len(payload) > 1024 * 1024:
            raise ValueError('Invalid NDEF type or payload length.')
        identifier = bytes.fromhex(record.get('id', ''))
        if len(identifier) > 255 or tnf == 0 and (typ or payload or identifier) or tnf == 5 and typ:
            raise ValueError('Invalid NDEF empty/unknown record or identifier.')
        short = len(payload) < 256
        flags = tnf | (0x80 if i == 0 else 0) | (0x40 if i == len(records) - 1 else 0) | (0x10 if short else 0) | (8 if identifier else 0)
        output.extend(bytes([flags, len(typ)]) + len(payload).to_bytes(1 if short else 4, 'big') + (bytes([len(identifier)]) if identifier else b'') + typ + identifier + payload)
    if tlv:
        if len(output) > 65535:
            raise ValueError('NDEF Type 2 TLV is too large.')
        return b'\x03' + (bytes([len(output)]) if len(output) < 255 else b'\xff' + len(output).to_bytes(2, 'big')) + output + b'\xfe'
    return bytes(output)


def decode_ndef(raw):
    raw = bytes(raw)
    # Optional Type 2 TLV envelope. Never confuse a record header with TLV.
    if raw[:1] in {b'\x00', b'\x03'}:
        p = 0
        while p < len(raw) and raw[p] == 0:
            p += 1
        if p < len(raw) and raw[p] == 3:
            p += 1
            if p >= len(raw):raise ValueError('Truncated NDEF TLV.')
            n = raw[p];p += 1
            if n == 255:
                if p + 2 > len(raw):raise ValueError('Truncated NDEF TLV length.')
                n = int.from_bytes(raw[p:p + 2], 'big');p += 2
            if p + n > len(raw):raise ValueError('Truncated NDEF TLV data.')
            raw = raw[p:p + n]
    records, p = [], 0
    while p < len(raw):
        if len(records) >= 100 or p + 2 > len(raw):raise ValueError('Invalid NDEF record count/header.')
        header, type_len = raw[p:p + 2];p += 2
        if header & 7 in {6, 7}:raise ValueError('Unsupported/reserved NDEF TNF.')
        if header & 0x20:raise ValueError('Chunked NDEF records are not supported.')
        if bool(header & 0x80) != (not records):raise ValueError('Invalid NDEF MB flag.')
        nbytes = 1 if header & 0x10 else 4
        if p + nbytes > len(raw):raise ValueError('Truncated NDEF payload length.')
        n = int.from_bytes(raw[p:p + nbytes], 'big');p += nbytes
        ident_len = 0
        if header & 8:
            if p >= len(raw):raise ValueError('Truncated NDEF ID length.')
            ident_len = raw[p];p += 1
        if p + type_len + ident_len + n > len(raw):raise ValueError('Truncated NDEF record.')
        typ = raw[p:p + type_len];p += type_len
        ident = raw[p:p + ident_len];p += ident_len
        payload = raw[p:p + n];p += n
        if header & 7 == 0 and (typ or ident or payload) or header & 7 == 5 and typ:
            raise ValueError('Invalid empty/unknown NDEF record.')
        record = {'tnf': header & 7, 'type': typ.decode('utf-8', errors='replace'), 'id': ident.hex(),
                  'kind': 'raw', 'value': payload.hex().upper()}
        if header & 7 == 1 and typ == b'U' and payload:
            if payload[0] >= len(URI_PREFIXES):raise ValueError('Unknown NDEF URI prefix.')
            record.update(kind='uri', value=URI_PREFIXES[payload[0]] + payload[1:].decode('utf-8'))
        elif header & 7 == 1 and typ == b'T' and payload:
            lang_len = payload[0] & 63
            if len(payload) < 1 + lang_len:raise ValueError('Truncated NDEF text language.')
            record.update(kind='text', language=payload[1:1 + lang_len].decode('ascii'),
                          value=payload[1 + lang_len:].decode('utf-16' if payload[0] & 128 else 'utf-8'))
        elif header & 7 == 4 and typ == b'android.com:pkg':
            record.update(kind='aar', value=payload.decode('utf-8'))
        records.append(record)
        if header & 0x40:
            if p != len(raw):raise ValueError('Trailing data after final NDEF record.')
            return records
    raise ValueError('NDEF message has no final record.')


@dataclass(frozen=True)
class TraceFrame:
    start: str
    end: str
    direction: str
    data: str
    crc: str
    annotation: str


def parse_trace(output):
    frames = []
    for line in _text(output).splitlines():
        clean = re.sub(r'^\s*\[[=+!]\]\s*', '', line)
        # Iceman trace list: start | end | source | data | CRC | annotation.
        columns = [c.strip() for c in clean.split('|')]
        if len(columns) >= 4 and re.search(r'\b(?:Rdr|Tag|Reader|Card)\b', columns[2], re.I):
            data = re.findall(r'(?<![\w])([0-9A-Fa-f]{2})(?:!?)(?![\w])', columns[3])
            if data:
                frames.append(TraceFrame(columns[0], columns[1], columns[2], ' '.join(data).upper(),
                                         columns[4] if len(columns) > 4 else '', ' | '.join(columns[5:])))
    return frames


def load_samples(path, limit=2_000_000):
    path = Path(path)
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError('Signal file exceeds 64 MiB.')
    if path.suffix.lower() == '.wav':
        with wave.open(str(path), 'rb') as f:
            channels, width, rate = f.getnchannels(), f.getsampwidth(), f.getframerate()
            if width not in (1, 2) or not 1 <= channels <= 8 or f.getnframes() > limit:
                raise ValueError('Use an 8/16-bit PCM WAV with at most two million frames.')
            raw = f.readframes(f.getnframes())
        values = list(raw) if width == 1 else list(struct.unpack('<' + 'h' * (len(raw) // 2), raw))
        return [x - 128 if width == 1 else x for x in values[::channels]], rate
    values = []
    for line in path.read_text(encoding='ascii').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):continue
        value = float(line)
        if not math.isfinite(value):raise ValueError('Non-finite signal sample.')
        values.append(value)
        if len(values) > limit:raise ValueError('Signal contains too many samples.')
    if not values:raise ValueError('Signal file contains no samples.')
    return values, None


def fft_spectrum(samples, sample_rate, max_points=8192):
    if not samples or not math.isfinite(sample_rate) or sample_rate <= 0:
        raise ValueError('Samples and a positive sample rate are required.')
    n = 1 << (min(len(samples), max_points).bit_length() - 1)
    if n < 4:raise ValueError('At least four samples are required.')
    mean = sum(samples[:n]) / n
    a = [complex((samples[i] - mean) * (.5 - .5 * math.cos(2 * math.pi * i / (n - 1)))) for i in range(n)]
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:j ^= bit;bit >>= 1
        j ^= bit
        if i < j:a[i], a[j] = a[j], a[i]
    width = 2
    while width <= n:
        factor = cmath.exp(-2j * math.pi / width)
        for first in range(0, n, width):
            w = 1
            for i in range(width // 2):
                even, odd = a[first + i], w * a[first + i + width // 2]
                a[first + i], a[first + i + width // 2] = even + odd, even - odd
                w *= factor
        width *= 2
    return [(i * sample_rate / n, 20 * math.log10(max(abs(a[i]) / n, 1e-12))) for i in range(n // 2)]


def lf_identity(profile, output):
    text = _text(output)
    if re.search(r'no .*?(?:found|detected)|no data found|failed to demodulate|CRC\s*(?:fail|bad|error)|parity\s*(?:error|fail)', text, re.I):
        raise ValueError('No confirmed LF identifier.')
    patterns = {
        'EM410x': r'EM\s*410x\s+ID\s*:?\s*([0-9A-F]{10})\b',
        'HID Prox': r'(?:HID\s+Prox\s+(?:TAG\s+)?ID|TAG\s+ID|raw)\s*:\s*([0-9A-F]+)\b',
        'Indala': r'Indala[^\n]*?\b(?:Raw|UID|ID)\s*:\s*([0-9A-F]+)\b',
        'AWID': r'AWID[^\n]*?\b(?:Raw|ID)\s*:\s*([0-9A-F]+)\b',
        'ioProx': r'IO\s*Prox[^\n]*?\bRaw\s*:\s*([0-9A-F]+)\b',
        'FDX-B': r'Animal ID[\s.:]+([0-9]{3,4}-[0-9]{1,12})\b',
    }
    ids = set(re.findall(patterns[profile], text, re.I))
    if len(ids) != 1:raise ValueError('The response does not contain one unambiguous LF identifier.')
    identifier = ids.pop().upper()
    fields = {'identifier': identifier, 'protocol': profile}
    for name, pattern in [('facility_code', r'\bFC\s*:\s*(\d+)'), ('card_number', r'\b(?:Card|CN)\s*:\s*(\d+)'),
                          ('country', r'Country\s*Code[\s.:]+(\d+)')]:
        values = set(re.findall(pattern, text, re.I))
        if len(values) == 1:fields[name] = int(values.pop())
    if profile == 'ioProx':
        m = re.search(r'XSF\((\d+)\)([0-9A-F]{2}):(\d+)', text, re.I)
        if m:fields.update(version=int(m[1]),facility_code=int(m[2],16),card_number=int(m[3]))
    if profile == 'FDX-B':
        country, number = identifier.split('-');fields.update(country=int(country),national_code=int(number))
        for key,pattern in [('animal',r'Animal bit set\?[^\n]*?(True|False)'),('data_block',r'Data block\?[^\n]*?(True|False)'),('rudi',r'RUDI bit\?[^\n]*?(True|False)')]:
            match=re.search(pattern,text,re.I)
            if match:fields[key]=match[1].lower()=='true'
        for key,pattern in [('reserved',r'Reserved/RFU[. ]+(\d+)'),('user_info',r'User Info\?[. ]+(\d+)'),('replacement',r'Replacement No\?[. ]+(\d+)')]:
            match=re.search(pattern,text,re.I)
            if match:fields[key]=int(match[1])
        match=re.search(r'Data block\?[^\n]*?\(\s*0x([0-9A-F]+)\s*\)',text,re.I)
        if match:fields['extended']=int(match[1],16)
    bits = re.search(r'(?:Fmt|len[: ]|Format Len:)\s*(\d+)',text,re.I)
    if bits:fields['bit_length'] = int(bits[1])
    return fields


def structured_dump(document, profile, uid='', kind='structured-card'):
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':')).encode('utf-8')
    if len(raw) > 16 * 1024 * 1024:raise ValueError('Structured snapshot exceeds 16 MiB.')
    lf = kind == 'lf-identifier' or kind == 'detection-record' and isinstance(document,dict) and document.get('rf_band') == 'lf'
    return DumpData(raw, 'bin', {'structured_document': document, 'protocol': profile, 'uid': uid, 'rf_band': 'lf' if lf else 'hf',
                                'snapshot_kind': kind, 'source_sha256': hashlib.sha256(raw).hexdigest()}, 1)


def is_felica_document(document):
    """The current RRG FeliCa dump uses a systems tree, without FileType."""
    if not isinstance(document, dict):return False
    systems=document.get('systems')
    if not isinstance(systems,list) or not systems or len(systems)>256:return False
    for system in systems:
        if not isinstance(system,dict):return False
        if any(not isinstance(system.get(k),str) or not re.fullmatch('[0-9A-Fa-f]{'+str(n)+'}',system[k]) for k,n in [('code',4),('idm',16),('pmm',16)]):return False
        nodes=system.get('nodes')
        if not isinstance(nodes,list):return False
        for node in nodes:
            if not isinstance(node,dict) or not isinstance(node.get('type'),str) or not re.fullmatch('[0-9A-Fa-f]{4}',str(node.get('code',''))):return False
            data=node.get('data',{})
            if not isinstance(data,dict) or any(not re.fullmatch('[0-9A-Fa-f]{4}',str(k)) or not isinstance(v,str) or not re.fullmatch('[0-9A-Fa-f]{32}',v) for k,v in data.items()):return False
    return True


def import_profile_dump(path, profile):
    path = Path(path)
    if path.stat().st_size > 16 * 1024 * 1024:raise ValueError('Dump exceeds 16 MiB.')
    try:
        result = parse_dump(path)
    except ValueError:
        if path.suffix.lower() != '.json':raise
        document = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(document, dict) or not isinstance(document.get('FileType'), str):raise
        if document.get('FileType') == 'legic v2' and isinstance(document.get('blocks'), dict):
            blocks=document['blocks'];indices=sorted(int(k) for k in blocks)
            if indices != list(range(len(indices))):raise ValueError('LEGIC dump has missing blocks.')
            values=[parse_hex(blocks[str(i)]) for i in indices]
            if not values or any(len(v)!=16 for v in values[:-1]) or not 1 <= len(values[-1]) <= 16:raise ValueError('Invalid LEGIC block layout.')
            raw=b''.join(values)
            if len(raw) not in {22,256,1024}:raise ValueError('Unsupported LEGIC memory size.')
            result=DumpData(raw,'json',{'source_json':document,'source_json_text':path.read_text(encoding='utf-8'),
                                      'source_sha256':hashlib.sha256(raw).hexdigest()},16)
        else:result = structured_dump(document, profile)
    result.metadata['protocol'] = profile
    return result
