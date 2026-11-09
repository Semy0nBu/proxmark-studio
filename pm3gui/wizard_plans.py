"""Prepared family writes: durable backup, explicit review, fresh guards, readback."""
from dataclasses import dataclass
from copy import deepcopy
import hashlib
import json
import re
from .core import DumpData, parse_hex
from .operations import _text, CLASSIC_GEOMETRIES, sector_trailer, _path_token
from .studio_data import say, structured_dump


@dataclass(frozen=True)
class ReviewGate:
    title: str
    uid: str
    scope: str
    rows: tuple
    backup: str
    digest: str


def review_changes(directory, title, uid, before, after, *, unit=16, scope='', labels=None, backup=None):
    before, after = bytes(before), bytes(after)
    if len(before) != len(after) or not before or not 1 <= unit <= 4096:
        raise ValueError('Review requires equal, non-empty memory maps.')
    rows=[]
    for offset in range(0,len(before),unit):
        a,b=before[offset:offset+unit],after[offset:offset+unit]
        if a!=b:rows.append({'address':str(labels[offset//unit] if labels else offset//unit),
                            'before':a.hex(' ').upper(),'after':b.hex(' ').upper()})
    (directory/'before.bin').write_bytes(before)
    (directory/'expected.bin').write_bytes(after)
    plan={'title':title,'uid':uid,'scope':scope,'rows':rows,'before_sha256':hashlib.sha256(before).hexdigest(),
          'expected_sha256':hashlib.sha256(after).hexdigest()}
    digest=hashlib.sha256(json.dumps(plan,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    plan['digest']=digest
    (directory/'write-plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    if rows:
        approved=yield ReviewGate(title,uid,scope,tuple(rows),str(backup or directory/'before.bin'),digest)
        if approved is not True:raise ValueError('Prepared write was not approved.')
        if (directory/'before.bin').read_bytes()!=before or (directory/'expected.bin').read_bytes()!=after:
            raise ValueError('Prepared backup or expected bytes changed. Prepare again.')
    return bool(rows)


def checked_output(output):
    text=_text(output)
    diagnostics='\n'.join(line for line in text.splitlines() if not re.match(
        r'^\s*(?:\[[^\]\n]+\]\s*)?[0-9A-Fa-f]+(?:/0x[0-9A-Fa-f]+)?\s*\|\s*[0-9A-Fa-f]{2}(?:[ \t]|$)',line))
    if re.search(r'\b(?:error|failed|timeout|aborted)\b|mac not equal|MAC.*mismatch|Status flag [12]',diagnostics,re.I):
        raise ValueError(say('Клиент сообщил об ошибке чтения/аутентификации. См. журнал.', 'The client reported a read/authentication error. See the log.'))
    return text


def one_block(output, address, size, *, hexadecimal=False):
    rows=[]
    for m in re.finditer(r'^\s*(?:\[[^\]\n]+\]\s*)?([0-9A-Fa-f]+)(?:/0x[0-9A-Fa-f]+)?\s*\|\s*((?:[0-9A-Fa-f]{2}[ \t]+)*[0-9A-Fa-f]{2})\s*(?:\||$)',checked_output(output),re.M):
        if int(m[1],16 if hexadecimal else 10)==address:
            raw=bytes.fromhex(m[2])
            if len(raw)==size:rows.append(raw)
    if len(rows)!=1:raise ValueError('One complete, unambiguous block was not returned.')
    return rows[0]


def data_block_write(profile, address, payload, *, key='', key_b=False, service='0900', target_uid=''):
    """User blocks only; never overrides MAC, access, system or lock checks."""
    from .studio_flows import Request, identity
    from .writing import block_sector
    payload=bytes(payload)
    if type(address) is not int:raise ValueError('Invalid block number.')
    if profile=='MIFARE Plus':
        if not 1<=address<=255 or address==sector_trailer(block_sector(address)):raise ValueError('Select a MIFARE Plus user-data block, excluding UID and trailers.')
        if len(payload)!=16 or not re.fullmatch('[0-9A-Fa-f]{32}',key):raise ValueError('MIFARE Plus requires 16 data bytes and a 16-byte AES key.')
        select='hf 14a reader';select_args='--skip';stem='hf mfp'
        args=f'--blk {address} --key {key}'+(' --keyb' if key_b else '')
        parser=lambda text:one_block(text,address,16)
    elif profile=='Topaz':
        if not 2<=address<=12 or len(payload)!=8:raise ValueError('The Topaz wizard writes static user blocks 2–12 (8 bytes); UID, CC, lock and OTP areas are excluded.')
        select='hf topaz reader';select_args='';stem='hf topaz';args=f'--blk {address}'
        parser=lambda text:one_block(text,address,8)
    elif profile=='FeliCa':
        if not 0<=address<=13 or len(payload)!=16 or not re.fullmatch('[0-9A-Fa-f]{4}',service):raise ValueError('FeliCa: 16 data bytes, user block 0–13 and a two-byte on-wire service code are required.')
        select='hf felica reader';select_args='';stem='hf felica';args=''
        # The current rdbl printer labels this row with Number of Blocks (01),
        # not the block address from BLE. This single-block request is immutable.
        parser=lambda text:one_block(text,1,16,hexadecimal=True)
    else:raise ValueError('Unsupported block-write profile.')
    def flow(directory):
        output,_=yield Request(select,select_args);uid=identity(output)
        if target_uid and uid!=target_uid.upper():raise ValueError('Destination identity does not match.')
        read_args=args if profile!='FeliCa' else f'--idm {uid} --sn 01 --scl {service} --bn 01 --ble 80{address:02X}'
        output,_=yield Request(stem+' rdbl',read_args);before=parser(output)
        output,_=yield Request(select,select_args)
        if identity(output)!=uid:raise ValueError('Card changed during backup.')
        changed=yield from review_changes(directory,profile,uid,before,payload,unit=len(payload),labels=[f'SC {service} / {address}' if profile=='FeliCa' else address],
            scope=say('Один пользовательский блок; UID, ключи и настройки сохраняются.', 'One user-data block; UID, keys and configuration are preserved.'))
        if changed:
            output,_=yield Request(select,select_args)
            if identity(output)!=uid:raise ValueError('Card changed after review.')
            output,_=yield Request(stem+' rdbl',read_args)
            if parser(output)!=before:raise ValueError('Target bytes changed after review. Prepare again.')
            output,_=yield Request(select,select_args)
            if identity(output)!=uid:raise ValueError('Card changed before writing.')
            output,_=yield Request(stem+' wrbl',read_args+' -d '+payload.hex());checked_output(output)
            output,_=yield Request(select,select_args)
            if identity(output)!=uid:raise ValueError('Card changed after writing.')
            output,_=yield Request(stem+' rdbl',read_args)
            if parser(output)!=payload:raise ValueError('Written block does not match readback.')
            output,_=yield Request(select,select_args)
            if identity(output)!=uid:raise ValueError('Readback identity was not confirmed.')
        (directory/'after.bin').write_bytes(payload)
        doc={'uid':uid,'block':address,'block_data':payload.hex().upper(),'service':service if profile=='FeliCa' else None,'read_scope':'one-user-block'}
        return {'uid':uid,'protocol':profile,'dump':structured_dump(doc,profile,uid,'selected-region'),
                'backup':str(directory/'before.bin'),'verification':'single-user-block-and-identity-readback','changed_units':int(changed)}
    return flow


def desfire_source_files(dump):
    """Actual RRG Applications.<AID>.Files.<FID> schema; missing Data is unknown."""
    document=dump.metadata.get('structured_document') or dump.metadata.get('source_json') or {}
    if isinstance(document.get('file_data'),str):
        raw=parse_hex(document['file_data'])
        return [{'aid':document['aid'],'fid':document['fid'],'data':raw}] if raw else []
    apps=document.get('Applications')
    if not isinstance(apps,dict):return []
    result=[]
    for aid,app in apps.items():
        if not isinstance(aid,str) or not re.fullmatch('[0-9A-Fa-f]{6}',aid) or not isinstance(app,dict):continue
        files=app.get('Files',{})
        if not isinstance(files,dict):continue
        for fid,file in files.items():
            if not isinstance(fid,str) or not re.fullmatch('[0-9A-Fa-f]{2}',fid) or int(fid,16)>31 or not isinstance(file,dict):continue
            if str(file.get('TypeRaw')).upper() not in {'00','01'} or file.get('Read') is not True or not isinstance(file.get('Data'),str):continue
            raw=parse_hex(file['Data'])
            if raw and len(raw)==file.get('FileSize') and len(raw)<=65536:
                result.append({'aid':aid.upper(),'fid':fid.upper(),'data':raw})
    return result


def desfire_settings(output, fid):
    text=checked_output(output)
    rows=re.findall(r'\bfile\s+'+fid+r'\s+settings\[(\d+)\]:\s*([0-9A-Fa-f ]+)\s*$',text,re.I|re.M)
    if len(rows)!=1:raise ValueError('DESFire did not return one raw file-settings record.')
    raw=bytes.fromhex(rows[0][1])
    if int(rows[0][0])!=len(raw) or len(raw)<7 or raw[0] not in {0,1}:
        raise ValueError('This wizard supports existing Standard Data and Backup Data files only.')
    size=int.from_bytes(raw[4:7],'little')
    if not 1<=size<=65536:raise ValueError('The verified file-backup limit is 64 KiB.')
    return raw,size


def desfire_data(output, fid, size):
    text=checked_output(output)
    headers=re.findall(r'\bRead (\d+) bytes from file 0x([0-9A-Fa-f]{2}) offset (\d+)',text)
    if headers!=[(str(size),fid.lower(), '0')] and [(n,f.upper(),o) for n,f,o in headers]!=[(str(size),fid.upper(),'0')]:
        raise ValueError('DESFire file identity/length was not confirmed.')
    rows=re.findall(r'^\s*(?:\[[^\]\n]+\]\s*)?(\d+)/0x([0-9A-Fa-f]+)\s*\|\s*([0-9A-Fa-f ]+)\|',text,re.M)
    data=bytearray()
    for decimal,hexadecimal,value in rows:
        part=bytes.fromhex(value)
        if int(decimal)!=len(data) or int(hexadecimal,16)!=len(data) or len(part)!=min(16,size-len(data)):
            raise ValueError('DESFire file data has missing, duplicate or out-of-order bytes.')
        data.extend(part)
    if len(data)!=size:raise ValueError('Complete DESFire file backup was not obtained.')
    return bytes(data)


def desfire_file_write(aid, fid, offset, payload, *, key, algo='AES', keyno=0, channel='ev1', cmode='plain', no_auth=False, target_uid=''):
    from .studio_flows import Request, identity
    payload=bytes(payload)
    if not re.fullmatch('[0-9A-Fa-f]{6}',aid) or not re.fullmatch('[0-9A-Fa-f]{2}',fid) or int(fid,16)>31:raise ValueError('DESFire requires AID (3 bytes) and file ID 00–1F.')
    if type(offset) is not int or not 0<=offset<=0xffffff or not 1<=len(payload)<=65536:raise ValueError('Use a non-negative file offset and 1–65536 data bytes.')
    lengths={'DES':16,'2TDEA':32,'3TDEA':48,'AES':32}
    if algo not in lengths or not re.fullmatch('[0-9A-Fa-f]{'+str(lengths[algo])+'}',key):raise ValueError('Key length does not match the selected DESFire algorithm.')
    if channel not in {'d40','ev1','ev2','lrp'} or cmode not in {'plain','mac','encrypt'} or type(keyno) is not int or not 0<=keyno<=13:raise ValueError('Invalid DESFire authentication settings.')
    if no_auth and cmode!='plain':raise ValueError('No-auth mode requires plain communication.')
    aid,fid=aid.upper(),fid.upper()
    common=f'--aid {aid} --fid {fid} --algo {algo} --keyno {keyno} --key {key} --kdf none --ccset native --schann {channel} --cmode {cmode}'+(' --no-auth' if no_auth else '')
    settings_args=common+' --verbose'
    def flow(directory):
        output,_=yield Request('hf 14a reader','--skip');uid=identity(output)
        if target_uid and uid!=target_uid.upper():raise ValueError('Destination identity does not match.')
        output,_=yield Request('hf mfdes getfilesettings',settings_args);settings,size=desfire_settings(output,fid)
        if offset+len(payload)>size:raise ValueError('Payload extends beyond the existing file.')
        read_args=common+f' --type data --offset 000000 --length {size:06X}'
        output,_=yield Request('hf mfdes read',read_args);before=desfire_data(output,fid,size)
        output,_=yield Request('hf 14a reader','--skip')
        if identity(output)!=uid:raise ValueError('Card changed during file backup.')
        expected=before[:offset]+payload+before[offset+len(payload):]
        (directory/'file-settings.bin').write_bytes(settings)
        changed=yield from review_changes(directory,'DESFire',uid,before,expected,unit=16,
            labels=[f'{aid}/{fid} @ {i}' for i in range(0,size,16)],scope=say('Данные выбранного файла; проверяется весь файл и неизменность настроек.', 'Selected file data; the entire file and unchanged settings are checked.'))
        if changed:
            output,_=yield Request('hf 14a reader','--skip')
            if identity(output)!=uid:raise ValueError('Card changed after review.')
            output,_=yield Request('hf mfdes getfilesettings',settings_args)
            if desfire_settings(output,fid)[0]!=settings:raise ValueError('File settings changed after review.')
            output,_=yield Request('hf mfdes read',read_args)
            if desfire_data(output,fid,size)!=before:raise ValueError('File bytes changed after review.')
            output,_=yield Request('hf 14a reader','--skip')
            if identity(output)!=uid:raise ValueError('Card changed before writing.')
            for index in range(0,len(payload),512):
                output,_=yield Request('hf 14a reader','--skip')
                if identity(output)!=uid:raise ValueError('Card changed before a file-data chunk.')
                chunk=payload[index:index+512]
                output,_=yield Request('hf mfdes write',common+f' --type data --offset {offset+index:06X} --data {chunk.hex()}'+(' --commit' if settings[0]==1 else ''));checked_output(output)
            output,_=yield Request('hf mfdes read',read_args);after=desfire_data(output,fid,size)
            if after!=expected:raise ValueError('Full file readback differs, including bytes outside the edited region.')
            output,_=yield Request('hf mfdes getfilesettings',settings_args)
            if desfire_settings(output,fid)[0]!=settings:raise ValueError('File settings changed after writing.')
            output,_=yield Request('hf 14a reader','--skip')
            if identity(output)!=uid:raise ValueError('Readback card identity was not confirmed.')
        (directory/'after.bin').write_bytes(expected)
        doc={'uid':uid,'aid':aid,'fid':fid,'file_data':expected.hex().upper(),'file_settings':settings.hex(),'read_scope':'one-complete-data-file'}
        return {'uid':uid,'protocol':'DESFire','dump':structured_dump(doc,'DESFire',uid,'selected-file'),
                'backup':str(directory/'before.bin'),'verification':'entire-selected-file-settings-and-uid-readback','changed_units':sum(a!=b for a,b in zip(before,expected))}
    return flow


def magic_user_restore(source, generation='Gen1a', password='00000000', *, target_uid=''):
    from .studio_flows import Request, identity
    from .studio_data import import_profile_dump
    from .operations import parse_detection
    from .writing import source_family
    source=deepcopy(source)
    size=next((name for name,(_,n) in CLASSIC_GEOMETRIES.items() if n==len(source.data)),None)
    if not size or source_family(source)!='mfc' or source.metadata.get('partial_dump') or source.metadata.get('structured_document') or source.block_size!=16:
        raise ValueError('Choose a complete Classic memory snapshot.')
    if generation not in {'Gen1a','Gen4 GTU'} or not re.fullmatch('[0-9A-Fa-f]{8}',password):raise ValueError('Select a Magic generation and a four-byte GTU password.')
    save='hf mf csave' if generation=='Gen1a' else 'hf mf gsave'
    write='hf mf csetblk' if generation=='Gen1a' else 'hf mf gsetblk'
    extra='' if generation=='Gen1a' else ' --pwd '+password
    def snapshot(directory, name):
        prefix=directory/name
        yield Request(save,f'--{size} -f '+_path_token(prefix)+extra)
        paths=[prefix.with_suffix(s) for s in ('.json','.bin') if prefix.with_suffix(s).is_file()]
        if not paths:raise ValueError('Magic backup file was not saved.')
        dump=import_profile_dump(paths[0],'Magic '+generation)
        if len(dump.data)!=len(source.data) or dump.metadata.get('partial_dump'):raise ValueError('Complete Magic memory was not backed up.')
        return dump
    def flow(directory):
        output,_=yield Request('hf 14a info');uid=identity(output)
        if target_uid and uid!=target_uid.upper():raise ValueError('Destination identity does not match.')
        expected=r'\bgen\s*1a\b' if generation=='Gen1a' else r'\bgen\s*4\b|\bGTU\b'
        if not re.search(expected,output,re.I) or parse_detection(output).classic_size!=size:raise ValueError('Magic generation and geometry were not positively identified.')
        before=yield from snapshot(directory,'magic-before')
        output,_=yield Request('hf 14a reader','--skip')
        if identity(output)!=uid:raise ValueError('Magic card changed during backup.')
        expected=bytearray(before.data)
        trailers={sector_trailer(s) for s in range(CLASSIC_GEOMETRIES[size][0])}
        blocks=[b for b in range(1,len(source.data)//16) if b not in trailers]
        for b in blocks:expected[b*16:(b+1)*16]=source.data[b*16:(b+1)*16]
        changed=yield from review_changes(directory,'Magic '+generation,uid,before.data,expected,unit=16,
            scope=say('Только пользовательские блоки. UID/BCC, ключи и условия доступа назначения сохраняются.', 'User blocks only. Destination UID/BCC, keys and access conditions are preserved.'))
        if changed:
            output,_=yield Request('hf 14a reader','--skip')
            if identity(output)!=uid:raise ValueError('Magic card changed after review.')
            current=yield from snapshot(directory,'magic-guard')
            if current.data!=before.data:raise ValueError('Magic bytes changed after review.')
            for b in blocks:
                raw=bytes(expected[b*16:(b+1)*16])
                if raw==before.data[b*16:(b+1)*16]:continue
                output,_=yield Request('hf 14a reader','--skip')
                if identity(output)!=uid:raise ValueError('Magic card changed before a block write.')
                output,_=yield Request(write,f'--blk {b} -d '+raw.hex()+extra);checked_output(output)
            after=yield from snapshot(directory,'magic-after')
            output,_=yield Request('hf 14a reader','--skip')
            if identity(output)!=uid or after.data!=bytes(expected):raise ValueError('Magic full-memory readback or identity did not match.')
        else:after=before
        return {'uid':uid,'protocol':'Magic '+generation,'dump':after,'backup':str(directory/'before.bin'),
                'verification':'full-memory-user-blocks-and-preserved-service-fields-readback'}
    return flow
