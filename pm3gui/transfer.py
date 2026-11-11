"""Facts and compatibility explanations for the existing one-card writers.

An assessment describes the scope of a checked plan. It is never RF evidence
and cannot approve a write. No UID is guessed from an arbitrary byte stream.
"""
from dataclasses import dataclass
import re
from .i18n import tr
from .operations import CLASSIC_GEOMETRIES
from .card_snapshot import known_byte_mask


@dataclass(frozen=True)
class SourceFacts:
    name: str = ''
    family: str = ''
    protocol: str = ''
    uid: str = ''
    size: int = 0
    known: int | None = None
    geometry: str = ''
    scope: str = ''
    error: str = ''


@dataclass(frozen=True)
class Check:
    title: str
    state: str  # pending / ok / limit / blocked
    detail: str
    critical: bool = False
    brief: str = ''


def source_facts(dump, record=None, name=''):
    if dump is None:return SourceFacts(name=name)
    record = record or {}
    meta = dump.metadata
    invalid=tr('Карта известных байтов повреждена. Выберите другую версию.')
    if not isinstance(meta,dict):return SourceFacts(name=name,error=invalid)
    doc = meta.get('structured_document',{})
    raw = meta.get('source_json',{})
    validation = meta.get('read_validation',{})
    partial = meta.get('partial_dump',{})
    doc,raw,validation,partial=({} if value is None else value for value in (doc,raw,validation,partial))
    if not all(isinstance(value,dict) for value in (doc,raw,validation,partial)):
        return SourceFacts(name=name,error=invalid)
    protocol = str(record.get('protocol') or meta.get('protocol') or doc.get('protocol') or '')
    uid = str(record.get('uid') or partial.get('uid') or meta.get('uid') or raw.get('UID') or '')
    if not re.fullmatch(r'[0-9a-fA-F ]{8,32}', uid):uid = ''
    uid = uid.replace(' ', '').upper()
    family = validation.get('kind') or partial.get('kind') or ''
    ftype = raw.get('FileType', '')
    if not isinstance(family,str) or not isinstance(ftype,str):
        return SourceFacts(name=name,error=invalid)
    labels = protocol.lower()
    if not family:
        if ftype in {'mfcard', 'mfc v2', 'mfc v3'} or 'classic' in labels:family = 'mfc'
        elif ftype == 'mfu' or 'ntag' in labels or 'ultralight' in labels:family = 'mfu'
        elif 'em410' in labels or len(dump.data) == 5:family = 'em410x'
        elif not doc and not protocol and ftype in {'', 'raw'}:
            family = 'mfc' if dump.block_size == 16 and len(dump.data) in {v[1] for v in CLASSIC_GEOMETRIES.values()} else 'mfu' if dump.block_size == 4 else ''
            if not family:
                from .writing import MFU_PROFILES
                if len(dump.data) in {value[0] for value in MFU_PROFILES.values()}:family='mfu'
    geometry = next((key for key, value in CLASSIC_GEOMETRIES.items() if value[1] == len(dump.data)), '') if family == 'mfc' else ''
    scope = tr('Память из сохранённого снимка')
    known, error = None, ''
    if doc:
        scope = tr('Структурированная запись; объём файла не равен памяти чипа')
    elif family == 'em410x' or meta.get('record_kind') in {'identifier', 'lf-identifier'}:
        scope = tr('Только идентификатор; полный образ памяти отсутствует')
    else:
        try:known = sum(known_byte_mask(dump, protocol=protocol or ('MIFARE Classic' if family == 'mfc' else 'Ultralight/NTAG' if family == 'mfu' else '')))
        except (ValueError, TypeError, KeyError):error = tr('Карта известных байтов повреждена. Выберите другую версию.')
    return SourceFacts(str(name or record.get('name') or tr('Снимок данных')), family,
                       protocol or ('MIFARE Classic ' + geometry.upper() if geometry else 'Ultralight / NTAG' if family == 'mfu' else family),
                       uid, len(dump.data), known, geometry, scope, error)


def hf_checks(source, plan=None, options=None):
    rows = []
    if source.error:return [Check(tr('Источник'), 'blocked', source.error)]
    if source.family not in {'mfc', 'mfu'}:
        return [Check(tr('Семейство'), 'blocked', tr('Для этого источника выберите мастер его семейства. Обычная HF-запись рассчитана на Classic и Ultralight/NTAG.'))]
    family = options.family if options else source.family
    if family != source.family:
        rows.append(Check(tr('Профиль'), 'blocked', tr('Выбранный профиль не совпадает с семейством источника.')))
    target = getattr(plan, 'target', None)
    confirmed = bool(target and target.uid and not target.ambiguous_identity and target.family in {'mfc', 'mfu'})
    if not confirmed:
        rows.append(Check(tr('Заготовка'), 'pending', tr('Уберите исходную карту и положите одну заготовку на нужную антенну. Нажмите «Проверить карту».')))
    elif target.family != family:
        rows.append(Check(tr('Семейство'), 'blocked', tr('Семейства источника и заготовки различаются. Выберите заготовку того же поддерживаемого семейства.')))
    else:
        rows.append(Check(tr('Семейство'), 'ok', target.protocol))
        length = CLASSIC_GEOMETRIES.get(target.classic_size, (0, 0))[1] if family == 'mfc' else 0
        if family == 'mfu' and getattr(plan, 'model', ''):
            from .writing import MFU_PROFILES
            length = MFU_PROFILES[plan.model][0]
        if length and source.size != length:
            rows.append(Check(tr('Объём'), 'blocked', tr('В источнике {0} байт, у заготовки {1}. Этот мастер не обрезает и не дополняет образ автоматически. Выберите совпадающий профиль памяти.', source.size, length)))
        else:
            rows.append(Check(tr('Объём'), 'ok' if length else 'pending', tr('Объём памяти совпадает: {0} байт.', length) if length else tr('Модель и диапазон пользовательской памяти ещё проверяются.')))
    rows.append(Check('UID', 'limit', tr('Сохраняется UID заготовки. Обычная запись данных не переносит UID исходной карты; для изменения UID нужен отдельный мастер Magic.'),True,tr('UID заготовки сохранится.')))
    rows.append(Check(tr('Служебные области'), 'limit', tr('UID, условия доступа и служебные страницы сохраняются. Ключи A/B переносятся только отдельным выбранным параметром и при наличии подтверждённых данных.'),bool(options and getattr(options,'include_keys',False))))
    if source.known is not None and source.known < source.size:
        detail = tr('Неизвестно {0} байт. Непрочитанные области пропускаются.', source.size - source.known)
        if options and options.include_unknown:
            detail = tr('Выбрана запись заполнителей. Непрочитанные пользовательские блоки могут заменить реальные данные нулями; неизвестные A/B всё равно исключены.')
        rows.append(Check(tr('Неполный источник'), 'limit', detail,True))
    ready = bool(plan and plan.ready and not plan.error and not plan.done)
    rows.append(Check(tr('Доступ и резервная копия'), 'ok' if ready else 'pending',
        tr('Выбранные области прочитаны, резервные данные сохранены. Запись начнётся только после подтверждения плана.') if ready else
        tr('Права доступа, ключи и резервное чтение проверяются до разрешения записи.')))
    if plan and plan.error:rows.append(Check(tr('Проверка остановлена'), 'blocked', plan.error))
    return rows
