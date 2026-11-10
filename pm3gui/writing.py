"""Reviewable HF write transactions: backup, UID guards, write, read-back.

No port access here. The existing ClientRunner executes one typed operation at
a time; a prepared transaction cannot write until approve() is called.
"""
from .i18n import tr
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
from time import monotonic
from pathlib import Path
from .core import DumpData
from .operations import (Operation, CLASSIC_GEOMETRIES, parse_detection, _text,
                         sector_trailer, sector_first_block,_uid)


def block_sector(block):
    return block//4 if block<128 else 32+(block-128)//16


def access_conditions(raw):
    if len(raw)!=16:
        raise ValueError(tr('Некорректный трейлер сектора.'))
    x,y,z = raw[6:9]
    a,b,c = y>>4,z&15,z>>4
    if (x&15,x>>4,y&15)!=(a^15,b^15,c^15):
        raise ValueError(tr('Повреждены биты доступа сектора; запись не начинается.'))
    return [((a>>i&1)<<2)|((b>>i&1)<<1)|(c>>i&1) for i in range(4)]


def parse_block(output, block, size=16):
    if re.search(r'^\s*\[!+\].*(?:fail|error|time\s*out|timeout|aborted)',_text(output),re.I|re.M):
        return None
    rows=set()
    pattern = r'(?:^|\n)\s*(?:\[[^\]\n]+\]\s*)?(\d{1,3})(?:/0x[0-9a-f]+)?\s*\|\s*((?:[0-9A-F]{2}\s+){'+str(size-1)+r'}[0-9A-F]{2})\s*\|'
    for m in re.finditer(pattern,_text(output),re.I):
        if int(m[1])==block:
            rows.add(bytes.fromhex(m[2]))
    return next(iter(rows)) if len(rows)==1 else None


def write_ack(output, code):
    value=_text(output)
    return code==0 and len(re.findall(r'\bWrite\s*\(\s*ok\s*\)',value,re.I))==1 and not re.search(
        r'\b(?:fail|error|timeout|time out|aborted|tear.?off)\b',value,re.I)


# User memory only. UID/BCC, OTP/CC, lock bytes, config, PWD/PACK and key
# pages are excluded. Other HF types remain accessible in the HF catalogue.
MFU_PROFILES = {
    'NTAG 213':(180,4,39), 'NTAG 215':(540,4,129), 'NTAG 216':(924,4,225),
    'NTAG 203':(168,4,39), 'NTAG 210':(80,4,15), 'NTAG 212':(164,4,35),
    'UL-C':(192,4,39), 'UL EV1 48':(80,4,15), 'UL EV1 128':(164,4,35),
    'Ultralight':(64,4,15),
}


def mfu_profile(output):
    value=_text(output)
    types=set(re.findall(r'\bTYPE:\s*([^\n]+)',value,re.I))
    if len(types)!=1:
        raise ValueError(tr('Клиент не подтвердил единственную модель Ultralight/NTAG.'))
    value=next(iter(types)).replace('MIFARE Ultralight C','UL-C').replace('MIFARE Ultralight EV1','UL EV1')
    value=re.sub(r'(\d+)bytes',r'\1 bytes',value)
    # Longest/more specific labels first; do not mistake UL-C/AES/EV1 for UL.
    for label in sorted(MFU_PROFILES,key=len,reverse=True):
        if label=='Ultralight':
            continue
        pattern = re.escape(label).replace(r'\ ',r'\s*')
        if re.search(pattern+r'(?:\b|[FCT])',value,re.I):
            return label
    if re.search(r'^MIFARE Ultralight\s*(?:$|\(MF0ICU1)',value,re.I):
        return 'Ultralight'
    raise ValueError(tr('Для этой модели нет проверенного диапазона обычной записи. Используйте профильную команду из «Инструменты HF».'))


def source_family(dump):
    if dump.metadata.get('structured_document'):
        raise ValueError('Structured snapshots require their family-specific writer.')
    kind=dump.metadata.get('read_validation',{}).get('kind')
    ftype=dump.metadata.get('source_json',{}).get('FileType')
    if ftype and ftype not in {'raw','mfcard','mfc v2','mfc v3','mfu'}:
        raise ValueError('Use the writer for this card family.')
    if kind=='mfc' or ftype in {'mfcard','mfc v2','mfc v3'}:
        return 'mfc'
    if kind=='mfu' or ftype=='mfu':
        return 'mfu'
    if dump.block_size==4:
        return 'mfu'
    return 'mfc' if len(dump.data) in {v[1] for v in CLASSIC_GEOMETRIES.values()} else 'mfu'


@dataclass(frozen=True)
class WriteOptions:
    family: str
    key_a: str='FFFFFFFFFFFF'
    key_b: str='FFFFFFFFFFFF'
    mfu_key: str=''
    secure_channel: bool=False
    include_keys: bool=False
    include_unknown: bool=False
    first: int=0
    last: int=255
    target_uid: str=''
    blocks: tuple[int, ...]=()

    def __post_init__(self):
        if not isinstance(self.blocks, (tuple, list)) or any(type(b) is not int or not 0 <= b <= 255 for b in self.blocks):
            raise ValueError('Selected blocks must be integer addresses from 0 to 255.')
        if len(set(self.blocks)) != len(self.blocks):
            raise ValueError('Duplicate selected blocks.')
        object.__setattr__(self, 'blocks', tuple(sorted(self.blocks)))


class HFWritePlan:
    def __init__(self, dump, options, directory, keys_for_uid=lambda uid:[]):
        if not isinstance(dump,DumpData) or options.family not in {'mfc','mfu'} or not dump.data:
            raise ValueError(tr('Выберите дамп Classic или Ultralight/NTAG.'))
        if type(options.first) is not int or type(options.last) is not int or not 0<=options.first<=options.last<=255:
            raise ValueError(tr('Неверный диапазон блоков/страниц.'))
        for key in ((options.key_a,options.key_b) if options.family=='mfc' else ()):
            if not re.fullmatch(r'[0-9A-Fa-f]{12}',key):
                raise ValueError(tr('Ключи Classic должны содержать 12 HEX.'))
        if options.mfu_key and not re.fullmatch(r'(?:[0-9A-Fa-f]{8}|[0-9A-Fa-f]{32})',options.mfu_key):
            raise ValueError(tr('Пароль/ключ Ultralight/NTAG имеет неверную длину.'))
        if options.family=='mfu' and options.include_keys:
            raise ValueError(tr('A/B относятся только к Classic.'))
        self.source=DumpData(bytes(dump.data),dump.format,deepcopy(dump.metadata),dump.block_size)
        self.options=options
        self.expected_uid=_uid(options.target_uid) if options.target_uid else ''
        self.directory=Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True)
        self.keys_for_uid=keys_for_uid
        self.phase='identify'
        self.pending=None
        self.target=None
        self.ready=False
        self.approved=False
        self.done=False
        self.error=''
        self.entries=[]
        self.backup={}
        self.trailers={}
        self.auth={}
        self.verified=[]
        self.may_have_written=[]
        self.skipped=[]
        self.expected_keys=[]
        self.index=0
        self.deadline=monotonic()+1200
        from .card_snapshot import known_byte_mask
        mask = known_byte_mask(self.source, protocol='MIFARE Classic' if options.family == 'mfc' else 'Ultralight/NTAG')
        self._known = {i for i, known in enumerate(mask) if known}

    def _issue(self, op):
        self.pending=op
        return op

    def next_operation(self):
        if monotonic()>self.deadline:
            raise ValueError(tr('Превышено время транзакции записи (20 минут).'))
        if self.pending is not None:
            raise ValueError(tr('Предыдущий шаг записи ещё не завершён.'))
        if self.done or self.ready and not self.approved:
            return None
        if self.phase=='identify': return self._issue('hf search')
        if self.phase=='mfu_info': return self._issue(Operation('mfu_info',auth_key=self.options.mfu_key,secure_channel=self.options.secure_channel))
        if self.phase=='trailer':
            kind,key=self.candidates[self.candidate_index]
            return self._issue(Operation('mfc_read_block',block=sector_trailer(self.sector),classic_size=self.target.classic_size,auth_key=key,known_type=kind))
        if self.phase=='backup':
            block=self.units[self.unit_index]
            return self._issue(self._read(block,self.read_auth[block]))
        if self.phase in {'preflight_identity','guard','final_identity','keys_identity','verify_identity'}:
            return self._issue(Operation('hf14a_select'))
        entry=self.entries[self.index]
        if self.phase=='write':
            self.may_have_written.append(entry['block'])
            self.save_report('writing')
            if self.options.family=='mfc':
                return self._issue(Operation('mfc_write_block',classic_size=self.target.classic_size,block=entry['block'],
                    auth_key=entry['key'],known_type=entry['type'],data_hex=entry['new'],allow_trailer=entry['trailer']))
            return self._issue(Operation('mfu_write_page',block=entry['block'],data_hex=entry['new'],auth_key=self.options.mfu_key,secure_channel=self.options.secure_channel))
        if self.phase in {'verify','verify_b'}:
            pair=(entry['type'],entry['key'])
            if entry['trailer']:
                pair=('A',entry['new'][:12]) if self.phase=='verify' else ('B',entry['new'][20:32])
            return self._issue(self._read(entry['block'],pair))
        raise ValueError(tr('Неизвестный этап записи.'))

    def _read(self, block, pair):
        if self.options.family=='mfc':
            return Operation('mfc_read_block',classic_size=self.target.classic_size,block=block,known_type=pair[0],auth_key=pair[1])
        return Operation('mfu_read_page',block=block,auth_key=self.options.mfu_key,secure_channel=self.options.secure_channel)

    def consume(self, output, code):
        if self.pending is None:
            raise ValueError(tr('Нет ожидающего шага записи.'))
        operation,self.pending=self.pending,None
        if self.phase=='identify':
            self.target=parse_detection(output)
            if code or self.target.family!=self.options.family or not self.target.uid or self.target.ambiguous_identity:
                raise ValueError(tr('На антенне нет одной карты выбранного HF-семейства.'))
            if self.expected_uid and self.target.uid!=self.expected_uid:
                raise ValueError(tr('UID карты не совпадает со сценарием. Запись не начата.'))
            if self.options.family=='mfc':
                if self.target.classic_size not in CLASSIC_GEOMETRIES or len(self.source.data)!=CLASSIC_GEOMETRIES[self.target.classic_size][1]:
                    raise ValueError(tr('Размер дампа не совпадает с Classic на антенне.'))
                self._select_units(16,range(len(self.source.data)//16))
                self.sectors=sorted({block_sector(b) for b in self.units}|set(self.key_sectors))
                self.sector_index=0
                self.context=self.keys_for_uid(self.target.uid)
                self._begin_sector()
            else:
                self.phase='mfu_info'
            return
        if self.phase=='mfu_info':
            if code: raise ValueError(tr('Модель Ultralight/NTAG не подтверждена.'))
            self.model=mfu_profile(output)
            length,first,last=MFU_PROFILES[self.model]
            if len(self.source.data)!=length:
                raise ValueError(tr('Для {0} ожидается {1} байт памяти без BIN-заголовка.', f'{self.model}', f'{length}'))
            self._select_units(4,range(first,last+1))
            self.read_auth={b:('','') for b in self.units}
            self.write_auth=dict(self.read_auth)
            self.unit_index=0
            self.phase='backup'
            return
        if self.phase=='trailer':
            raw=parse_block(output,operation.block)
            if code in {-3,-4,-5} or re.search(r'command execution time\s*out|UART .*failed|invalid serial port',output,re.I):
                raise ValueError(tr('Связь прервалась при подготовке записи.'))
            if code==0 and raw is not None:
                conditions=access_conditions(raw)
                self.trailers[self.sector]=raw
                self.auth[self.sector][operation.known_type]=operation.auth_key
                # A-readable B is plaintext data, not a B authentication key.
                if conditions[3] in {0,1,2}:
                    self.auth[self.sector]['B_data']=raw[10:16].hex().upper()
            self.candidate_index+=1
            while self.candidate_index<len(self.candidates):
                kind,_=self.candidates[self.candidate_index]
                if kind in self.auth[self.sector] or kind=='B' and 'B_data' in self.auth[self.sector]:
                    self.candidate_index+=1
                else: break
            if self.candidate_index==len(self.candidates):
                if self.sector not in self.trailers:
                    raise ValueError(tr('Нет подходящего ключа для сектора {0}; запись не начата.', f'{self.sector}'))
                self.sector_index+=1
                self._begin_sector()
            return
        if self.phase=='backup':
            block=self.units[self.unit_index]
            raw=parse_block(output,block,self.unit_size)
            if code or raw is None:
                raise ValueError(tr('Не удалось сохранить исходный блок/страницу {0}; запись не начата.', f'{block}'))
            self.backup[block]=raw.hex().upper()
            proposed=self.source.data[block*self.unit_size:(block+1)*self.unit_size]
            if raw!=proposed:
                kind,key=self.write_auth[block]
                self.entries.append(dict(block=block,old=raw.hex().upper(),new=proposed.hex().upper(),type=kind,key=key,trailer=False))
            self.unit_index+=1
            if self.unit_index==len(self.units): self._prepare_review()
            return
        if self.phase in {'preflight_identity','guard','final_identity','keys_identity','verify_identity'}:
            if code or parse_detection(output).uid!=self.target.uid:
                raise ValueError(tr('UID изменился или карта снята. Операция остановлена.'))
            if self.phase=='preflight_identity':
                self.ready=True
                self.save_report('review')
            elif self.phase=='guard': self.phase='write'
            elif self.phase in {'keys_identity','verify_identity'}: self._entry_verified()
            else:
                self.done=True
                self.save_report('verified')
            return
        if self.phase=='write':
            if not write_ack(output,code):
                raise ValueError(tr('Клиент не подтвердил запись блока {0}. Повтор автоматически не выполняется.', f"{self.entries[self.index]['block']}"))
            self.phase='verify'
            return
        entry=self.entries[self.index]
        raw=parse_block(output,entry['block'],self.unit_size)
        if code or raw is None:
            raise ValueError(tr('Не удалось проверить записанный блок {0}.', f"{entry['block']}"))
        expected=bytes.fromhex(entry['new'])
        if entry['trailer']:
            if raw[6:10]!=expected[6:10]: raise ValueError(tr('После смены ключей изменились условия доступа.'))
            if access_conditions(raw)[3] in {0,1,2}:
                if raw[10:16]!=expected[10:16]: raise ValueError(tr('Новый Key B не совпал.'))
            elif self.phase=='verify':
                self.phase='verify_b'
                return
            self.phase='keys_identity'
        else:
            if raw!=expected: raise ValueError(tr('Прочитанные байты блока {0} не совпали с записанными.', f"{entry['block']}"))
            self.phase='verify_identity'

    def _select_units(self, size, allowed):
        self.unit_size=size
        self.units=[]
        self.key_sectors=[]
        trailers={sector_trailer(s):s for s in range(CLASSIC_GEOMETRIES[self.target.classic_size][0])} if size==16 else {}
        for block in allowed:
            if self.options.blocks and block not in self.options.blocks: continue
            if not self.options.first<=block<=self.options.last: continue
            if block==0: continue
            known=set(range(block*size,(block+1)*size))<=self._known
            if block in trailers:
                if self.options.include_keys and known: self.key_sectors.append(trailers[block])
                elif self.options.include_keys: self.skipped.append(block)
                continue
            if known or self.options.include_unknown: self.units.append(block)
            else: self.skipped.append(block)
        if not self.units and not self.key_sectors:
            raise ValueError(tr('В выбранном диапазоне нет считанных пользовательских блоков для записи.'))

    def _begin_sector(self):
        if self.sector_index==len(self.sectors):
            self._resolve_classic_access()
            self.unit_index=0
            if self.units: self.phase='backup'
            else: self._prepare_review()
            return
        self.sector=self.sectors[self.sector_index]
        self.auth[self.sector]={}
        self.candidates=[]
        for kind,fallback in [('A',self.options.key_a),('B',self.options.key_b)]:
            keys=[e['key_hex'].upper() for e in self.context if e['sector']==self.sector and e.get('type',e.get('key_type'))==kind]
            for key in dict.fromkeys(keys[:8]+[fallback.upper()]): self.candidates.append((kind,key))
        self.candidate_index=0
        self.phase='trailer'

    def _resolve_classic_access(self):
        self.read_auth,self.write_auth={},{}
        for block in self.units:
            sector=block_sector(block)
            local=block-sector_first_block(sector)
            group=local if sector<32 else local//5
            condition=access_conditions(self.trailers[sector])[group]
            writers={0:'AB',3:'B',4:'B',6:'B'}.get(condition,'')
            readers={0:'AB',1:'AB',2:'AB',3:'B',4:'AB',5:'B',6:'AB'}.get(condition,'')
            for options,target in [(writers,self.write_auth),(readers,self.read_auth)]:
                pair=next(((k,self.auth[sector][k]) for k in options if k in self.auth[sector]),None)
                if pair is None: raise ValueError(tr('Нет права чтения/записи блока {0}; подготовка остановлена.', f'{block}'))
                target[block]=pair

    def _prepare_review(self):
        for sector in self.key_sectors:
            raw=self.trailers[sector]
            cond=access_conditions(raw)[3]
            if cond not in {1,3}:
                raise ValueError(tr('Сектор {0}: смена A/B поддержана для изменяемых трейлеров 001/011. Условия доступа сохраняются.', f'{sector}'))
            auth=self.auth[sector]
            old_a,old_b=auth.get('A'),auth.get('B_data',auth.get('B'))
            if not old_a or not old_b: raise ValueError(tr('Для резервной копии сектора {0} нужны текущие A/B.', f'{sector}'))
            block=sector_trailer(sector)
            old=bytes.fromhex(old_a)+raw[6:10]+bytes.fromhex(old_b)
            source=self.source.data[block*16:(block+1)*16]
            new=source[:6]+raw[6:10]+source[10:16]
            self.backup[block]=old.hex().upper()
            if old!=new:
                kind='A' if cond==1 else 'B'
                self.entries.append(dict(block=block,old=old.hex().upper(),new=new.hex().upper(),type=kind,key=auth[kind],trailer=True))
        self.save_report('prepared')
        self.phase='preflight_identity'

    def approve(self):
        if not self.ready or self.approved or self.pending is not None or self.done or self.error:
            raise ValueError(tr('Сначала подготовьте и проверьте план записи.'))
        from .card_snapshot import known_byte_mask
        mask = known_byte_mask(self.source, protocol='MIFARE Classic' if self.options.family == 'mfc' else 'Ultralight/NTAG')
        for entry in self.entries:
            if entry['trailer'] and not all(mask[i] and i in self._known for i in range(entry['block']*16, (entry['block']+1)*16)):
                raise ValueError(tr('Нельзя записывать неизвестные ключи A/B. Подготовьте новый план.'))
        self.approved=True
        self.phase='guard' if self.entries else 'final_identity'
        self.save_report('approved')

    def _entry_verified(self):
        entry=self.entries[self.index]
        self.verified.append(entry['block'])
        if entry['trailer']:
            sector=block_sector(entry['block'])
            self.expected_keys.extend([{'sector':sector,'type':k,'key_hex':entry['new'][a:b]} for k,a,b in [('A',0,12),('B',20,32)]])
        self.save_report('verifying')
        self.index+=1
        self.phase='guard' if self.index<len(self.entries) else 'final_identity'

    def save_report(self, status):
        report={'version':1,'status':status,'uid':self.target.uid if self.target else '',
                'expected_uid':self.expected_uid,
                'family':self.options.family,'source_sha256':hashlib.sha256(self.source.data).hexdigest(),
                'backup_scope':'selected_data_blocks_and_known_sector_keys','backup':{str(k):v for k,v in self.backup.items()},
                'entries':self.entries,'skipped_unknown_blocks':self.skipped,'verified_blocks':self.verified,
                'may_have_written':self.may_have_written,'error':self.error,
                'preserved':['UID/manufacturer','access conditions','lock/OTP/config/password pages']}
        path=self.directory/'write-report.json'
        temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        temporary.replace(path)

    def fail(self,message):
        self.error=str(message)
        self.done=True
        self.save_report('failed' if self.may_have_written else 'not_written')

    def review(self):
        uid=self.target.uid if self.target else '—'
        return (tr('Карта назначения: {0}\nИзменится блоков/страниц: {1}; пропущено неизвестных: {2}.\nUID, условия доступа и служебные страницы сохраняются.\nРезервная копия изменяемых данных и отчёт: ', f'{uid}', f'{len(self.entries)}', f'{len(self.skipped)}')+str(self.directory/'write-report.json'))
