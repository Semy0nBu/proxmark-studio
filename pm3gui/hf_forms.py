"""Native HF parameter descriptions derived from the bundled client help."""
from .i18n import tr
from dataclasses import dataclass
import re
from .core import parse_hex
from .hf_catalog import COMMANDS,render_hf_command

FAMILIES={'14a':'ISO14443-A','14b':'ISO14443-B','15':'ISO15693','mf':'MIFARE Classic / Magic',
          'mfu':'Ultralight / NTAG','mfdes':'DESFire','mfp':'MIFARE Plus','iclass':'iCLASS / PicoPass',
          'felica':'FeliCa','topaz':'Topaz','legic':'Legic','ntag424':'NTAG 424','st25ta':'ST25TA'}
FAMILIES.update({'em':'EM4xxx','t55xx':'T55xx / T5577','hitag':'Hitag','hid':'HID Prox','indala':'Indala',
                 'awid':'AWID','io':'ioProx','pyramid':'Pyramid','nedap':'Nedap','viking':'Viking','nexwatch':'NexWatch'})
TITLES={'rdbl':'Прочитать блок','wrbl':'Записать блок','rdsc':'Прочитать сектор','dump':'Считать дамп',
        'restore':'Записать дамп','info':'Информация о карте','reader':'Определить карту','search':'Найти карту',
        'read':'Прочитать данные','write':'Записать данные','raw':'Обмен данными','list':'Просмотреть обмен',
        'sniff':'Перехват обмена','sim':'Эмуляция карты','help':'Справка','chk':'Проверить ключи',
        'fchk':'Быстрая проверка ключей','autopwn':'Автоматическое восстановление ключей',
        'nested':'Восстановление nested','hardnested':'Восстановление hardnested','staticnested':'Восстановление staticnested',
        'darkside':'Восстановление darkside','csetuid':'Изменить UID Magic','cload':'Записать Magic из файла',
        'cgetblk':'Прочитать блок Magic','csetblk':'Записать блок Magic','eload':'Загрузить дамп в эмулятор',
        'esave':'Сохранить память эмулятора','eview':'Просмотреть память эмулятора','wipe':'Очистить карту',
        'format':'Форматировать карту','auth':'Аутентификация','ndefread':'Прочитать NDEF','ndefwrite':'Записать NDEF',
        'ndefdump':'Сохранить NDEF','setkey':'Изменить ключ','setuid':'Изменить UID','keygen':'Генерация ключей',
        'pwdgen':'Подбор известного пароля','tune':'Измерить антенну','config':'Настройки карты',
        'clone':'Записать на заготовку','detect':'Определить параметры','trace':'Информация о чипе','demod':'Декодировать сигнал'}
LABELS={'blk':'Блок / страница','block':'Блок / страница','tblk':'Целевой блок','sec':'Сектор',
        'key':'Ключ','kfn':'Файл ключей','file':'Файл','filename':'Файл','uid':'UID','data':'Данные',
        'keyno':'Номер ключа','algo':'Алгоритм','kdf':'Диверсификация ключа','kdfi':'Данные диверсификации',
        'cmode':'Режим обмена','ccset':'Набор команд','schann':'Защищённый канал','aid':'ID приложения',
        'fid':'ID файла','offset':'Смещение','len':'Длина','length':'Длина','count':'Количество',
        'timeout':'Тайм-аут','wait':'Пауза','verbose':'Подробный вывод','force':'Принудительный режим',
        'no-auth':'Без аутентификации','no-default':'Пропустить стандартные ключи','all':'Оба ключа',
        'apdu':'Показывать APDU','opt':'Флаг OPTION','ua':'Без адресации','credit':'Ключ Credit',
        'elite':'Ключ Elite','raw':'Без преобразования ключа','nr':'Повтор NR/MAC','shallow':'Модуляция ASK',
        'dump':'Сохранить найденные ключи','emu':'Записать ключи в эмулятор','commit':'Подтвердить транзакцию',
        'debit':'Уменьшить значение','updaterec':'Номер записи','isoid':'ISO ID приложения',
        'fileisoid':'ISO ID файла','readerid':'ID считывателя','trkey':'Ключ считывателя','bits':'Число бит',
        'a':'Ключ A','b':'Ключ B','c':'Смещение ключа','v':'Подробный вывод','*':'Найти карту','2':'Медленный режим',
        '@':'Непрерывный режим','l':'Изменить порядок байтов','ki':'Индекс ключа','mac':'MAC',
        'mini':'Classic Mini','1k':'Classic 1K','2k':'Classic 2K','4k':'Classic 4K'}
LABELS.update({'id':'Идентификатор','fc':'Код объекта (FC)','cn':'Номер карты (CN)','pwd':'Пароль',
               'q5':'Заготовка Q5 / T5555','em':'Заготовка EM4305','clk':'Тактовый делитель','mod':'Модуляция'})


def _commands(command):
    if command.startswith('lf '):
        from .lf_catalog import COMMANDS as lf_commands
        return lf_commands
    if not command.startswith('hf '):
        from .client_catalog import COMMANDS as client_commands
        return client_commands
    return COMMANDS


@dataclass(frozen=True)
class Parameter:
    name:str
    flag:str
    aliases:tuple[str,...]
    label:str
    kind:str
    description:str
    required:bool=False
    choices:tuple[str,...]=()
    repeated:bool=False


def action_title(command):
    token=command.split()[-1]
    return tr(TITLES.get(token,_commands(command)[command]['description'].split('\n')[0] or token))


def family_title(command):
    group=command.split()[1]
    if command.startswith('lf em ') and len(command.split())>=3:return 'EM'+command.split()[2].upper()
    return FAMILIES.get(group,group.upper())


def parameters(command):
    help_text=_commands(command)[command]['help']
    help_text=re.sub(r'\x1b\[[0-9;]*[A-Za-z]','',help_text)
    options=re.search(r'^options:\s*\n(.*?)(?=^examples/notes:|\Z)',help_text,re.M|re.S)
    if not options:return ()
    usage=re.search(r'^usage:\s*\n(.*?)(?=^options:|\Z)',help_text,re.M|re.S)
    usage=usage[1] if usage else ''
    required_usage=usage
    while '[' in required_usage:
        updated=re.sub(r'\[[^\[\]]*\]','',required_usage)
        if updated==required_usage:break
        required_usage=updated
    rows=[]
    for line in options[1].splitlines():
        match=re.match(r'^\s+(--?[^\s,<>]+(?:,\s*--?[^\s,<>]+)*)(?:\s+(<[^>]+>))?\s+(.*)$',line)
        positional=re.match(r'^\s+(<[^>]+>)\s+(.*)$',line)
        if match:
            aliases=tuple(part.strip() for part in match[1].split(','))
            if '--help' in aliases or aliases==('-h',):continue
            flag=next((a for a in aliases if a.startswith('--')),aliases[0])
            hint=(match[2] or '').strip('<>')
            description=match[3].strip()
            name=flag.lstrip('-')
            required=any(re.search(r'(?<!\S)'+re.escape(a)+r'(?!\S)',required_usage) for a in aliases)
            repeated=any(re.search(re.escape(a)+r'\s+<[^>]+>\]?\.\.\.',usage) for a in aliases)
        elif positional:
            aliases=();flag='';hint=positional[1].strip('<>');description=positional[2].strip()
            name='positional_'+str(len(rows));required=bool(re.search(re.escape(positional[1]),required_usage))
            repeated=bool(re.search(re.escape(positional[1])+r'\]?\.\.\.',usage))
        else:continue
        kind=('bool' if not hint else 'choice' if '|' in hint else 'hex' if hint.lower()=='hex'
              else 'file' if hint.lower() in {'fn','filename','file'} else 'int' if hint.lower() in {'dec','int','ms','us','num'} else 'text')
        if kind=='choice' and set(hint.lower().split('|'))<={'hex','dec','pem','der','path'}:
            kind='file' if 'path' in hint.lower().split('|') else 'text'
        label=tr(LABELS.get(name,description if kind=='bool' else name.replace('-',' ').capitalize()))
        if len(name)==1 and name in {'a','b','c','k','s','r','3'} and not re.search(r'key [AB]|keytype|key type|target key',description,re.I):
            label=description.split('.')[0]
        if command=='hf 14a raw':
            label={'a':tr('Включить поле без выбора карты'),'c':tr('Добавить CRC'),'k':tr('Оставить поле включённым'),
                   '3':tr('Выбрать ISO14443-3'),'r':tr('Не ждать ответ'),'s':tr('Включить поле и выбрать карту'),
                   'b':tr('Число бит')}.get(name,label)
        if command.startswith('lf ') and name=='raw':label=tr('Необработанные данные')
        if not flag:label=tr('Данные') if kind=='hex' else tr('Значение')
        if command=='hf mf wrbl' and name=='data':required=True
        rows.append(Parameter(name,flag,aliases,label,kind,description,required,
                              tuple(hint.split('|')) if kind=='choice' else (),repeated))
    return tuple(rows)


def _value(parameter,value):
    text=str(value).strip()
    if parameter.kind=='hex':
        text=parse_hex(text).hex().upper()
    elif parameter.kind=='int':
        if not re.fullmatch(r'-?\d+',text):raise ValueError(parameter.label+tr(': введите целое число.'))
        text=str(int(text))
    elif parameter.kind=='choice' and text not in parameter.choices:
        raise ValueError(parameter.label+tr(': выберите значение из списка.'))
    if any(ord(c)<32 for c in text) or any(c in text for c in '";|&`'):
        raise ValueError(parameter.label+tr(': недопустимые символы параметра.'))
    return '"'+text+'"' if any(c.isspace() for c in text) else text


def form_arguments(command,values):
    tokens=[];positional=[]
    for field in parameters(command):
        value=values.get(field.name,False if field.kind=='bool' else '')
        if field.kind=='bool':
            if type(value) is not bool:raise ValueError(field.label+tr(': ожидается переключатель.'))
            if value:tokens.append(field.flag)
            continue
        items=value if isinstance(value,list) else [value]
        items=[v for v in items if v is not None and str(v).strip()]
        if field.required and not items:raise ValueError(tr('Заполните поле «{0}».',field.label))
        for item in items:
            encoded=_value(field,item)
            if not encoded:continue
            if field.flag:tokens.extend([field.flag,encoded])
            else:positional.append(encoded)
    text=' '.join(tokens+positional)
    if command.startswith('lf '):
        from .lf_catalog import render_lf_command
        render_lf_command(command,text)
    elif command.startswith('hf '):render_hf_command(command,text)
    else:
        from .client_catalog import render_command
        render_command(command,text)
    return text
