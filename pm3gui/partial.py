"""Evidence-bound incomplete HF images. Unknown bytes are never read evidence."""
from .i18n import tr
from copy import deepcopy
import re
from pathlib import Path
from .core import DumpData, MAX_DUMP_BYTES, _read_json, parse_hex
from .operations import CLASSIC_GEOMETRIES, _text, _BLOCK_OK, sector_first_block, sector_trailer


def partial_prompt(info):
    unit=tr('страниц') if info['kind']=='mfu' else tr('блоков')
    missing=len(info['missing_blocks'])
    partial=len(info['incomplete_blocks'])-missing
    return (tr('Карта прочитана не полностью. Прочитано {0} из {1} {2}.\nНе прочитано: {3}; частично известны: {4}.\n{5} неизвестных байт будут заполнены нулями.\n\nСохранить неполный дамп в библиотеку с картой пропусков? Полным чтением он отмечен не будет.', f"{len(info['read_blocks'])}", f"{info['expected_bytes'] // info['block_size']}", f'{unit}', f'{missing}', f'{partial}', f"{info['zero_filled_bytes']}"))


def validate_partial_metadata(value, data):
    if not isinstance(value, dict) or value.get('version') != 1 or value.get('expected_bytes') != len(data):
        raise ValueError(tr('Некорректное описание неполного дампа.'))
    known = value.get('known_bytes')
    if not isinstance(known, list) or any(type(i) is not int or not 0 <= i < len(data) for i in known) or known != sorted(set(known)):
        raise ValueError(tr('Некорректная карта считанных байтов.'))
    unit=value.get('block_size')
    if value.get('kind') not in {'mfc','mfu'} or unit!={'mfc':16,'mfu':4}[value['kind']] or len(data)%unit:
        raise ValueError(tr('Не совпал размер блоков карты пропусков.'))
    count=len(data)//unit
    for name in ('read_blocks','missing_blocks','incomplete_blocks'):
        indices=value.get(name)
        if not isinstance(indices,list) or any(type(i) is not int or not 0<=i<count for i in indices) or indices!=sorted(set(indices)):
            raise ValueError(tr('Некорректные адреса карты пропусков.'))
    bitmap=set(known)
    if value['missing_blocks']!=sorted(set(range(count))-set(value['read_blocks'])) or value['incomplete_blocks']!=[
        b for b in range(count) if not set(range(b*unit,(b+1)*unit))<=bitmap]:
        raise ValueError(tr('Карта блоков не совпала с картой байтов.'))
    if value.get('complete') is not False or not re.fullmatch(r'(?:[0-9A-F]{8}|[0-9A-F]{14}|[0-9A-F]{20})',str(value.get('uid',''))):
        raise ValueError(tr('Неполный дамп не содержит подтверждённый UID или ошибочно отмечен полным.'))
    return deepcopy(value)


def partial_classic(path: Path, detection, output, confirmed_uid, found_keys=()):
    """Keep only explicitly successful block reads, bound to pre/post RF UID."""
    if detection.family != 'mfc' or detection.classic_size not in CLASSIC_GEOMETRIES or confirmed_uid != detection.uid:
        raise ValueError(tr('UID и профиль неполного дампа не подтверждены.'))
    if not path.is_file() or path.stat().st_size > MAX_DUMP_BYTES:
        raise ValueError(tr('Файл неполного дампа не получен.'))
    root = _read_json(path.read_bytes())
    if not isinstance(root.get('Card'),dict):
        raise ValueError(tr('Некорректные метаданные карты.'))
    if root['FileType'] not in {'mfcard', 'mfc v2', 'mfc v3'} or not isinstance(root.get('blocks'), dict):
        raise ValueError(tr('Ожидались блоки MIFARE Classic.'))
    sectors, size = CLASSIC_GEOMETRIES[detection.classic_size]
    raw_blocks = {}
    for index, value in root['blocks'].items():
        if not re.fullmatch(r'0|[1-9][0-9]*', index) or not 0 <= int(index) < size//16:
            raise ValueError(tr('Адрес блока выходит за пределы Classic.'))
        block = parse_hex(value)
        if len(block) != 16:
            raise ValueError(tr('Блок Classic должен содержать 16 байт.'))
        raw_blocks[int(index)] = block
    declared = str(root.get('Card', {}).get('UID', '')).replace(' ', '').upper()
    expected_uid = detection.uid.upper()
    # RRG's mfc v2 header may apply the four-byte heuristic to a seven-byte UID.
    block0 = raw_blocks.get(0, b'')
    truncated_header = (root['FileType']=='mfc v2' and len(expected_uid)==14 and len(declared)==8
                        and expected_uid.startswith(declared) and block0[:7].hex().upper()==expected_uid
                        and len(block0)==16 and block0[0]^block0[1]^block0[2]^block0[3]==block0[4]
                        and (block0[6]&0xc0)==0
                        and str(root.get('Card',{}).get('SAK','')).upper()==block0[5:6].hex().upper()
                        and str(root.get('Card',{}).get('ATQA','')).upper()==block0[6:8].hex().upper())
    if declared != expected_uid and not truncated_header:
        raise ValueError(tr('UID файла неполного дампа не совпадает с картой.'))
    observed = set()
    for sec, local in _BLOCK_OK.findall(_text(output)):
        sec, local = int(sec), int(local)
        if 0 <= sec < sectors and 0 <= local < (4 if sec < 32 else 16):
            observed.add(sector_first_block(sec)+local)
    observed &= raw_blocks.keys()
    if not observed:
        raise ValueError(tr('Нет ни одного подтверждённо считанного блока; сохранять пустой образ нельзя.'))
    image, known = bytearray(size), set()
    for index in observed:
        image[index*16:(index+1)*16] = raw_blocks[index]
        known.update(range(index*16, (index+1)*16))
    keys = {(entry['sector'],entry.get('type',entry.get('key_type'))):entry['key_hex'] for entry in found_keys}
    for sector in range(sectors):
        block = sector_trailer(sector)
        if block not in observed:
            continue
        base = block*16
        # A is always masked. B is replaced by the CLI key file, so its bytes
        # need separately verified key evidence, even when the block read passed.
        for kind, offset in [('A',0),('B',10)]:
            span = range(base+offset,base+offset+6)
            known.difference_update(span)
            image[base+offset:base+offset+6] = bytes(6)
            key = keys.get((sector,kind))
            if isinstance(key,str) and re.fullmatch(r'[0-9A-Fa-f]{12}',key):
                image[base+offset:base+offset+6] = bytes.fromhex(key)
                known.update(span)
    missing = [i for i in range(size//16) if i not in observed]
    incomplete = [i for i in range(size//16) if not set(range(i*16,(i+1)*16)) <= known]
    info = {'version':1,'uid':expected_uid,'kind':'mfc','classic_size':detection.classic_size,
            'expected_bytes':size,'block_size':16,'read_blocks':sorted(observed),
            'missing_blocks':missing,'incomplete_blocks':incomplete,'known_bytes':sorted(known),
            'zero_filled_bytes':size-len(known),'complete':False}
    # Rebuild the canonical PM3 memory object; retain protocol/header metadata.
    source = deepcopy(root)
    source['blocks'] = {str(i):bytes(image[i*16:(i+1)*16]).hex().upper() for i in range(size//16)}
    source.pop('SectorKeys',None)
    return DumpData(bytes(image),'json',{'source_json':source,'partial_dump':info,
        'read_validation':{'kind':'mfc','complete':False,'verified_blocks':len(observed),
                           'expected_bytes':size,'uid':expected_uid,'known_key_slots':list(found_keys),
                           'all_key_slots_known':len(keys)==sectors*2}},16)


def partial_key_file(path, classic_size, entries):
    sectors = CLASSIC_GEOMETRIES[classic_size][0]
    slots = {(e['sector'],e.get('type',e.get('key_type'))):e['key_hex'] for e in entries}
    data = b''.join(bytes.fromhex(slots.get((sector,kind),'000000000000'))
                    for kind in ('A','B') for sector in range(sectors))
    if len(data)!=sectors*12:
        raise ValueError(tr('Некорректный файл доступных ключей.'))
    path.write_bytes(data)
    return path


def partial_ultralight(path, detection, output, confirmed_uid):
    if detection.family!='mfu' or detection.uid!=confirmed_uid or not path.is_file() or path.stat().st_size>MAX_DUMP_BYTES:
        raise ValueError(tr('Неполное чтение Ultralight/NTAG не подтверждено.'))
    root = _read_json(path.read_bytes())
    match = re.search(r'Partial dump created\.\s*\((\d+) of (\d+) blocks\)',_text(output),re.I)
    if not match or root['FileType']!='mfu' or not re.search(r'Reading tag memory',output,re.I):
        raise ValueError(tr('Нет подтверждённого числа считанных страниц.'))
    read, total = map(int,match.groups())
    if not 3<=read<total<=256 or str(root.get('Card',{}).get('UID','')).replace(' ','').upper()!=detection.uid:
        raise ValueError(tr('Не совпали UID или размеры неполного чтения.'))
    blocks = root.get('blocks',{})
    if set(blocks)!=set(map(str,range(read))):
        raise ValueError(tr('Страницы неполного файла неоднозначны.'))
    values = [parse_hex(blocks[str(i)]) for i in range(read)]
    if any(len(b)!=4 for b in values):
        raise ValueError(tr('Страница должна содержать 4 байта.'))
    raw = b''.join(values)
    binary = path.with_suffix('.bin')
    if not binary.is_file() or binary.stat().st_size!=56+len(raw) or binary.read_bytes()[56:]!=raw or binary.read_bytes()[11]+1!=read:
        raise ValueError(tr('Не совпали парные BIN/JSON неполного чтения.'))
    info = {'version':1,'uid':detection.uid,'kind':'mfu','expected_bytes':total*4,'block_size':4,
            'known_bytes':list(range(len(raw))),'read_blocks':list(range(read)),
            'missing_blocks':list(range(read,total)),'incomplete_blocks':list(range(read,total)),
            'zero_filled_bytes':(total-read)*4,'complete':False}
    data = raw+bytes((total-read)*4)
    root['blocks']={str(i):data[i*4:(i+1)*4].hex().upper() for i in range(total)}
    return DumpData(data,'json',{'source_json':root,'partial_dump':info,
        'read_validation':{'kind':'mfu','complete':False,'uid':detection.uid,'verified_pages':read,'expected_bytes':len(data)}},4)
