"""Portable declarative snapshots for one-card HF autowrite; no executable code."""
from .i18n import tr
import base64
from dataclasses import asdict,replace
import hashlib
import json
import re
from pathlib import Path
from .core import DumpData,_checked
from .writing import WriteOptions
from .operations import _uid

MAX_SCENARIO_BYTES=64*1024*1024


def save_scenario(path,dump,options,name=None):
    if name is None:name=tr('Автозапись HF')
    _checked(dump)
    validate_options(options)
    # Connection credentials come from the current library or explicit inputs;
    # they are not silently embedded into a portable scenario.
    portable=replace(options,key_a='FFFFFFFFFFFF',key_b='FFFFFFFFFFFF',mfu_key='')
    document={'format':'ProxmarkStudio.HFAutoWrite','version':1,'name':str(name)[:200],
              'source':{'data_base64':base64.b64encode(dump.data).decode('ascii'),
                        'sha256':hashlib.sha256(dump.data).hexdigest(),'format':dump.format,
                        'block_size':dump.block_size,'metadata':dump.metadata},'options':asdict(portable)}
    raw=json.dumps(document,ensure_ascii=False,indent=2,allow_nan=False)+'\n'
    if len(raw.encode('utf-8'))>MAX_SCENARIO_BYTES:raise ValueError(tr('Сценарий превышает 64 МиБ.'))
    Path(path).write_text(raw,encoding='utf-8')


def validate_options(options):
    if not isinstance(options,WriteOptions) or options.family not in {'mfc','mfu'}:
        raise ValueError(tr('Неверный профиль сценария.'))
    if any(type(getattr(options,k)) is not bool for k in ('secure_channel','include_keys','include_unknown')):
        raise ValueError(tr('Переключатели сценария должны быть bool.'))
    if type(options.first) is not int or type(options.last) is not int or not 0<=options.first<=options.last<=255:
        raise ValueError(tr('Неверный диапазон сценария.'))
    if any(not isinstance(getattr(options,key),str) for key in ('key_a','key_b','mfu_key','target_uid')):
        raise ValueError(tr('Ключи и UID сценария должны быть строками.'))
    if options.family=='mfc' and any(not re.fullmatch(r'[0-9A-Fa-f]{12}',key) for key in (options.key_a,options.key_b)):
        raise ValueError(tr('Ключи Classic должны содержать 12 HEX.'))
    if options.mfu_key and not re.fullmatch(r'(?:[0-9A-Fa-f]{8}|[0-9A-Fa-f]{32})',options.mfu_key):
        raise ValueError(tr('Неверная длина пароля / ключа Ultralight/NTAG.'))
    if options.target_uid:_uid(options.target_uid)
    if not isinstance(options.blocks, tuple) or any(type(b) is not int or not 0 <= b <= 255 for b in options.blocks):
        raise ValueError('Invalid selected blocks.')


def load_scenario(path):
    path=Path(path)
    if path.stat().st_size>MAX_SCENARIO_BYTES:raise ValueError(tr('Сценарий превышает 64 МиБ.'))
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError(tr('Повторяющееся поле сценария: ')+key)
            result[key]=value
        return result
    try:
        value=json.loads(path.read_text(encoding='utf-8-sig'),object_pairs_hook=unique)
        if not isinstance(value,dict) or value.get('format')!='ProxmarkStudio.HFAutoWrite' or value.get('version')!=1:
            raise ValueError(tr('Ожидался сценарий Proxmark Studio версии 1.'))
        source=value['source']
        data=base64.b64decode(source['data_base64'],validate=True)
        if hashlib.sha256(data).hexdigest()!=source['sha256']:raise ValueError(tr('Контрольная сумма снимка дампа не совпала.'))
        dump=_checked(DumpData(data,source['format'],source['metadata'],source['block_size']))
        if dump.metadata.get('partial_dump'):
            from .partial import validate_partial_metadata
            validate_partial_metadata(dump.metadata['partial_dump'],dump.data)
        options=WriteOptions(**value['options']);validate_options(options)
        return dump,options,str(value.get('name') or path.stem)
    except (KeyError,TypeError,UnicodeError,json.JSONDecodeError) as exc:
        raise ValueError(tr('Повреждён сценарий автозаписи: ')+str(exc)) from exc
