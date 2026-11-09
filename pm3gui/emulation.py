"""Immutable library snapshots for benign Classic/EM410x simulation."""
from .i18n import tr
import hashlib,json,re
from .operations import CLASSIC_GEOMETRIES,Operation,_path_token,_uid


def simulation_profile(dump,record=None):
    record=record or {};meta=dump.metadata
    if len(dump.data)==5 and (meta.get('protocol')=='EM410x' or 'em410' in str(record.get('protocol','')).lower() or dump.block_size==5):
        return 'em410x','',dump.data.hex().upper()
    size=next((name for name,(_,length) in CLASSIC_GEOMETRIES.items() if length==len(dump.data)),None)
    if dump.block_size!=16 or size is None or meta.get('read_validation',{}).get('kind')=='mfu':
        raise ValueError(tr('Поддержаны Classic Mini/1K/2K/4K и идентификаторы EM410x.'))
    uid=(record.get('uid') or meta.get('partial_dump',{}).get('uid') or meta.get('read_validation',{}).get('uid')
         or meta.get('source_json',{}).get('Card',{}).get('UID') or dump.data[:4].hex())
    return 'mfc',size,_uid(uid)


class EmulationPlan:
    def __init__(self,dump,record,directory,uid='',clock=64,allow_partial=False):
        self.family,self.size,default=simulation_profile(dump,record)
        if dump.metadata.get('partial_dump') and not allow_partial:raise ValueError(tr('Подтвердите использование неполного образа с заполнителями.'))
        self.uid=_uid(uid or default) if self.family=='mfc' else (uid or default).upper()
        if self.family=='em410x' and (not re.fullmatch(r'[0-9A-F]{10}',self.uid) or clock not in (32,64)):
            raise ValueError(tr('EM410x: ID из 10 HEX, RF/32 или RF/64.'))
        self.data=bytes(dump.data);self.directory=directory;directory.mkdir(parents=True,exist_ok=True)
        self.clock=clock;self.stage='created';self.error=''
        if self.family=='mfc':
            self.source=directory/'emulator-source.eml'
            self.source.write_text('\n'.join(self.data[i:i+16].hex().upper() for i in range(0,len(self.data),16))+'\n',encoding='ascii')
        self.report('created')

    def load_operation(self):
        return Operation('hf_tool',hf_command='hf mf eload',hf_args=f'--{self.size} -f '+_path_token(self.source,existing=True))

    def verify_operation(self):
        return Operation('hf_tool',hf_command='hf mf esave',hf_args=f'--{self.size} -f '+_path_token(self.directory/'emulator-readback'))

    def verify_memory(self):
        path=self.directory/'emulator-readback.bin'
        if not path.is_file() or path.read_bytes()!=self.data:raise ValueError(tr('Память эмулятора не совпала с выбранным снимком.'))

    def start_operation(self):
        if self.family=='mfc':return Operation('hf_tool',hf_command='hf mf sim',hf_args=f'--{self.size} -u {self.uid}')
        return Operation('lf_tool',lf_command='lf em 410x sim',lf_args=f'--id {self.uid} --clk {self.clock}')

    def report(self,state,error=''):
        self.stage=state;self.error=str(error)
        value={'version':1,'family':self.family,'size':self.size,'uid':self.uid,'clock':self.clock,
               'source_sha256':hashlib.sha256(self.data).hexdigest(),'state':state,'error':self.error,
               'simulation_only':True,'reader_attacks':False}
        path=self.directory/'emulation-report.tmp';path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');path.replace(self.directory/'emulation-report.json')
