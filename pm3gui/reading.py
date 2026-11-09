"""Bounded Classic reads with durable, UID-guarded sector checkpoints."""
from .i18n import tr
import json
import re
from .operations import Operation,CLASSIC_GEOMETRIES,parse_detection,sector_first_block,sector_trailer
from .partial import partial_classic
from .writing import parse_block
from .core import export_dump
from .core import parse_dump
from .writing import block_sector


class ClassicPartialRead:
    def __init__(self,detection,found_keys,directory):
        if detection.family!='mfc' or detection.classic_size not in CLASSIC_GEOMETRIES or not detection.uid:
            raise ValueError(tr('Не подтверждён профиль Classic для неполного чтения.'))
        self.detection=detection;self.found=list(found_keys);self.directory=directory
        self.path=directory/'partial-read.json';self.keys={}
        self.draft=directory/'partial-draft.json'
        sectors=CLASSIC_GEOMETRIES[detection.classic_size][0]
        for key in self.found:
            sector,kind,value=key['sector'],key['type'],key['key_hex']
            if type(sector) is not int or not 0<=sector<sectors or kind not in {'A','B'} or not re.fullmatch(r'[0-9A-Fa-f]{12}',value):
                raise ValueError(tr('Неверный ключ адресного чтения.'))
            self.keys.setdefault(sector,[]).append((kind,value.upper()))
        self.sectors=sorted(self.keys);self.position=0;self.stage='select';self.offset=0;self.key_index=0
        self.todo={s:list(range(sector_first_block(s),sector_trailer(s)+1)) for s in self.sectors}
        self.resumed_from=''
        self.confirmed={};self.buffer={};self.guards=[];self.pending=None;self.done=not self.sectors;self.reason=''
        self._checkpoint()

    @classmethod
    def resume(cls,dump,found_keys,directory,source=''):
        from .operations import Detection
        from .partial import validate_partial_metadata
        info=validate_partial_metadata(dump.metadata.get('partial_dump'),dump.data)
        if info['kind']!='mfc' or info.get('classic_size') not in CLASSIC_GEOMETRIES:raise ValueError(tr('Продолжение доступно для неполного Classic.'))
        if CLASSIC_GEOMETRIES[info['classic_size']][1]!=len(dump.data):raise ValueError(tr('Геометрия снимка не совпала с размером.'))
        reader=cls(Detection('mfc',info['uid'],'MIFARE Classic',info['classic_size'],True),found_keys,directory)
        reader.confirmed={b:dump.data[b*16:(b+1)*16] for b in info['read_blocks']}
        reader.guards=sorted({block_sector(b) for b in reader.confirmed})
        reader.todo={s:[b for b in reader.todo[s] if b not in reader.confirmed or b in info['incomplete_blocks']] for s in reader.sectors}
        reader.sectors=[s for s in reader.sectors if reader.todo[s]]
        reader.stage='resume_select';reader.done=False;reader.resumed_from=source;reader._checkpoint()
        return reader

    def next_operation(self):
        if self.done:return None
        if self.pending is not None:raise ValueError(tr('Предыдущий шаг чтения ещё не завершён.'))
        if self.stage=='resume_select':op=Operation('hf14a_select')
        elif self.stage in {'select','confirm'}:op=Operation('hf14a_select')
        else:
            sector=self.sectors[self.position]
            kind,key=self.keys[sector][self.key_index]
            op=Operation('mfc_read_block',classic_size=self.detection.classic_size,
                         block=self.todo[sector][self.offset],known_type=kind,auth_key=key)
        self.pending=op;return op

    def consume(self,output,code):
        if self.pending is None:raise ValueError(tr('Нет ожидаемого шага чтения.'))
        op=self.pending;self.pending=None
        sector=self.sectors[self.position] if self.position<len(self.sectors) else None
        actual=parse_detection(output)
        if actual.ambiguous_identity or actual.uid and actual.uid!=self.detection.uid:
            self.stop(tr('UID изменился во время чтения. Промежуточные данные сохранены в папке сеанса.'))
            raise ValueError(self.reason)
        if self.stage in {'select','confirm','resume_select'}:
            if code or actual.uid!=self.detection.uid:
                self.stop(tr('Не удалось подтвердить UID сектора {0} (код {1}); неподтверждённые блоки отброшены.', f'{sector}', f'{code}'))
                return
            if self.stage=='resume_select':
                self.stage='select';self.done=not self.sectors;self._checkpoint();return
            if self.stage=='select':self.stage='read';return
            self.confirmed.update(self.buffer);self.buffer={}
            if sector not in self.guards:self.guards.append(sector);self.guards.sort()
            self.position+=1;self.offset=0;self.key_index=0;self.stage='select'
            self.done=self.position==len(self.sectors);self._checkpoint();return
        # Auth denial is a block failure, not a reason to replay an RF attack.
        # Transport/cancel/timeout failures stop immediately; do not reopen COM
        # for every remaining block and do not publish the unguarded sector.
        if code in {-3,-4,-5,-8} or re.search(r'UART CreateFile failed|invalid serial port|command execution time\s*out|resynchronising|communication error',output,re.I):
            self.stop(tr('Обмен остановлен на блоке {0} (код {1}). Подтверждённые сектора сохранены.', f'{op.block}', f'{code}'))
            return
        raw=parse_block(output,op.block,16) if code==0 else None
        if raw is not None:self.buffer[op.block]=raw
        elif self.key_index+1<len(self.keys[sector]):self.key_index+=1;return
        self.key_index=0;self.offset+=1
        if self.offset>=len(self.todo[sector]):self.stage='confirm'

    def stop(self,reason):
        self.done=True;self.reason=reason;self.pending=None;self.buffer={};self._checkpoint()

    def _checkpoint(self):
        self.directory.mkdir(parents=True,exist_ok=True)
        source={'FileType':'mfc v3','Card':{'UID':self.detection.uid},
                'blocks':{str(b):raw.hex().upper() for b,raw in sorted(self.confirmed.items())},
                'ProxmarkStudioRead':{'version':1,'uid':self.detection.uid,'confirmed_sectors':self.guards,
                                     'status':'stopped' if self.reason else 'finished' if self.done else 'reading','reason':self.reason,
                                     'resumed_from':self.resumed_from}}
        temporary=self.path.with_suffix('.tmp');temporary.write_text(json.dumps(source,ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(self.path)
        if self.confirmed:
            temporary=self.draft.with_name('partial-draft.tmp.json')
            export_dump(self._build_dump(),temporary);temporary.replace(self.draft)

    def dump(self):
        if not self.done:raise ValueError(tr('Сначала завершите адресное чтение.'))
        return self._build_dump()

    def _build_dump(self):
        evidence='\n'.join(f'Sector... {s} block... {b-sector_first_block(s)} ( ok )'
                           for s in self.guards for b in self.confirmed if sector_first_block(s)<=b<=sector_trailer(s))
        dump=partial_classic(self.path,self.detection,evidence,self.detection.uid,self.found)
        dump.metadata['read_validation'].update(identity_scope='per_sector',confirmed_sectors=list(self.guards),interruption=self.reason)
        if self.resumed_from:dump.metadata['read_validation'].update(resumed_from=self.resumed_from,snapshot_scope='multiple_reads')
        if dump.metadata['partial_dump']['zero_filled_bytes']==0:
            dump.metadata.pop('partial_dump');dump.metadata['read_validation']['complete']=True
        return dump
