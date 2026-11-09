"""Reviewable EM410x -> unprotected T5577 transaction, no port access."""
from .i18n import tr
import json,re
from .operations import Operation,parse_detection,_text


def lf(command,args=''):return Operation('lf_tool',lf_command=command,lf_args=args)


def em410x_blocks(identifier,clock=64):
    if not re.fullmatch(r'[0-9A-Fa-f]{10}',identifier) or clock not in (32,64):raise ValueError(tr('Нужен EM410x ID из 10 HEX, RF/32 или RF/64.'))
    bits='1'*9;columns=[0]*4
    for nibble in identifier:
        row=f'{int(nibble,16):04b}';bits+=row+str(sum(map(int,row))%2)
        columns=[a^int(b) for a,b in zip(columns,row)]
    bits+=''.join(map(str,columns))+'0'
    return {0:0x00008040 | (0x00140000 if clock==64 else 0x00080000),1:int(bits[:32],2),2:int(bits[32:],2)}


def t55_block(output,block):
    if re.search(r'\b(?:failed|error|timeout|wrong|ambiguous)\b',output,re.I):raise ValueError(tr('Чтение T5577 не подтверждено.'))
    values={int(m[1],16) for m in re.finditer(r'^\s*(?:\[[^\]\n]+\]\s*)?0*'+str(block)+r'\s*\|\s*([0-9A-F]{8})\s*\|',_text(output),re.M|re.I)}
    if len(values)!=1:raise ValueError(tr('Нет однозначного блока T5577 {0}.', f'{block}'))
    return values.pop()


class LFWritePlan:
    def __init__(self,identifier,clock,directory):
        self.uid=identifier.upper();self.clock=clock;self.expected=em410x_blocks(self.uid,clock)
        self.directory=directory;directory.mkdir(parents=True,exist_ok=True)
        self.backup={};self.trace={};self.config=None;self.pending=None;self.ready=False;self.approved=False;self.done=False
        self.error='';self.sent=False;self.verified=False
        self.queue=[('detect',lf('lf t55xx detect'))]
        self.queue += [(f'initial:{b}',lf('lf t55xx read',f'--pg1 -b {b}')) for b in (1,2)]
        self.queue += [(f'backup:{b}',lf('lf t55xx read',f'-b {b}')) for b in range(8)]
        self.queue += [(f'guard:{b}',lf('lf t55xx read',f'--pg1 -b {b}')) for b in (1,2)]
        self._report()

    def next_operation(self):
        if self.done:return None
        if self.pending:raise ValueError(tr('Предыдущий шаг LF ещё выполняется.'))
        if not self.queue:return None
        self.pending=self.queue.pop(0)
        if self.pending[0]=='write':self.sent=True;self._report()
        return self.pending[1]

    def consume(self,output,code):
        if not self.pending:raise ValueError(tr('Нет ожидаемого шага LF.'))
        stage,op=self.pending;self.pending=None
        if code:raise ValueError(tr('Шаг {0} завершился с кодом {1}.', f'{stage}', f'{code}'))
        if stage=='detect':
            if not re.search(r'Chip type\.{2,}\s*T55x7\b',output,re.I) or re.search(r'\bambiguous\b',output,re.I):raise ValueError(tr('Не подтверждена однозначная заготовка T5577/T55x7.'))
            match=re.search(r'Block0\.{2,}\s*([0-9A-F]{8})\b',output,re.I)
            if not match:raise ValueError(tr('Конфигурация T5577 не подтверждена.'))
            self.config=int(match[1],16)
            if self.config&0x10:raise ValueError(tr('Мастер рассчитан на T5577 без пароля. Защищённые чипы доступны в инструментах LF.'))
        elif ':' in stage:
            kind,number=stage.split(':');block=int(number);value=t55_block(output,block)
            if kind=='initial':self.trace[block]=value
            elif kind=='backup':
                if block==0 and value!=self.config:raise ValueError(tr('Конфигурация заготовки изменилась при резервном чтении.'))
                self.backup[block]=value
            elif kind in {'guard','final'}:
                if value!=self.trace.get(block):raise ValueError(tr('Заготовка изменилась: не совпала заводская область page 1.'))
            elif kind=='verify':
                if value!=self.expected.get(block,self.backup[block]):raise ValueError(tr('Не совпал блок {0} после записи.', f'{block}'))
            elif kind=='config_guard':
                if value!=self.backup[0]:raise ValueError(tr('Конфигурация изменилась после просмотра плана.'))
        elif stage=='write':
            if not re.search(r'\bDone!',output) or re.search(r'wrong|fail|error|timeout',output,re.I):raise ValueError(tr('Клиент не подтвердил запись LF.'))
        elif stage=='redetect':
            match=re.search(r'Block0\.{2,}\s*([0-9A-F]{8})\b',output,re.I)
            if not match or int(match[1],16)!=self.expected[0]:raise ValueError(tr('Не подтверждена новая конфигурация T5577.'))
        elif stage=='identifier':
            actual=parse_detection(output)
            if actual.family!='em410x' or actual.uid!=self.uid or actual.ambiguous_identity:raise ValueError(tr('Прочитанный EM410x ID не совпал с источником.'))
        if not self.queue:
            if not self.approved:
                if len(self.backup)!=8 or len(self.trace)!=2 or all(v in (0,0xffffffff) for v in self.trace.values()):raise ValueError(tr('Нет полного резервного чтения или различимой заводской области.'))
                self.ready=True
                (self.directory/'backup-page0.json').write_text(json.dumps({'FileType':'t55x7','blocks':{str(b):f'{v:08X}' for b,v in self.backup.items()},'trace_page1':self.trace},indent=2),encoding='utf-8')
            else:self.done=True;self.verified=True
        self._report()

    def approve(self):
        if not self.ready or self.approved or self.done or self.error:raise ValueError(tr('Сначала подготовьте новый план LF.'))
        self.approved=True
        self.queue=[(f'guard:{b}',lf('lf t55xx read',f'--pg1 -b {b}')) for b in (1,2)]
        self.queue += [('config_guard:0',lf('lf t55xx read','-b 0')),
                       ('write',lf('lf em 410x clone',f'--id {self.uid} --clk {self.clock}')),('redetect',lf('lf t55xx detect'))]
        self.queue += [(f'verify:{b}',lf('lf t55xx read',f'-b {b}')) for b in range(8)]
        self.queue += [('identifier',lf('lf em 410x reader',f'--clk {self.clock} -b'))]
        self.queue += [(f'final:{b}',lf('lf t55xx read',f'--pg1 -b {b}')) for b in (1,2)]
        self._report()

    def fail(self,error):self.error=str(error);self.done=True;self.queue=[];self.pending=None;self._report()

    def _report(self):
        value={'version':1,'profile':'EM410x -> T5577','identifier':self.uid,'clock':self.clock,
               'status':'failed' if self.error else 'verified' if self.verified else 'writing' if self.approved else 'review' if self.ready else 'preparing',
               'backup_page0':self.backup,'factory_page1':self.trace,'expected':self.expected,'may_have_written':self.sent,
               'verified':self.verified,'error':self.error,'backup_scope':'page0 blocks 0..7, page1 factory blocks 1..2'}
        temp=self.directory/'lf-write-report.tmp';temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(self.directory/'lf-write-report.json')
