"""Single, UID-bound Classic recovery runs; no automatic attack fallback."""
from __future__ import annotations

import json
from .operations import (Operation, CLASSIC_GEOMETRIES, OPERATION_TIMEOUT_MS,
                         parse_detection, parse_candidate_verification, parse_target_key)
from .studio_flows import Request
from .studio_data import say


METHODS = {'Nested': 'mfc_nested_target', 'Hardnested': 'mfc_hardnested_target',
           'Darkside': 'mfc_darkside_seed'}


def _classic(output, uid='', size=''):
    detected = parse_detection(output)
    if (detected.ambiguous_identity or detected.family != 'mfc' or not detected.supported
            or not detected.uid or not detected.classic_size):
        raise ValueError(say('Не подтверждена одна карта MIFARE Classic.',
                             'One MIFARE Classic card was not confirmed.'))
    if uid and detected.uid != uid:
        raise ValueError(say('UID изменился. Поиск остановлен.', 'UID changed. Recovery stopped.'))
    if size and detected.classic_size != size:
        raise ValueError(say('Размер карты не совпадает с выбранным профилем.',
                             'Card geometry does not match the selected profile.'))
    return detected


def probe_classic(directory):
    output, _ = yield Request(Operation('hf14a_select'))
    detected = _classic(output)
    return {'uid': detected.uid, 'classic_size': detected.classic_size,
            'verification': 'classic-identification'}


def manual_recovery(method, size, uid='', *, source_sector=0, source_type='A',
                    key='', target_sector=0, target_type='B'):
    if method not in METHODS or size not in CLASSIC_GEOMETRIES:
        raise ValueError(say('Выберите метод и профиль Classic.', 'Choose a method and Classic geometry.'))
    uid = ''.join(uid.split()).upper()
    if uid:
        from .operations import _uid
        uid = _uid(uid)
    key = ''.join(key.split()).upper()
    operation = (Operation(METHODS[method], classic_size=size, target_sector=0)
                 if method == 'Darkside' else Operation(METHODS[method], classic_size=size,
                    known_sector=source_sector, known_type=source_type, auth_key=key,
                    target_sector=target_sector, target_type=target_type))
    operation.render()  # Validate everything before opening a device session.

    def flow(directory):
        output, _ = yield Request(Operation('hf14a_select'))
        bound = _classic(output, uid, size).uid
        if method != 'Darkside':
            check = Operation('mfc_check_candidate', classic_size=size,
                              known_sector=source_sector, known_type=source_type, auth_key=key)
            output, code = yield Request(check, timeout=30000, allow_failure=True)
            if code or not parse_candidate_verification(output, source_sector, source_type, key, size):
                raise ValueError(say('Исходный ключ не прошёл аутентификацию. Атака не запущена.',
                                    'The source key did not authenticate. The attack was not started.'))
            output, _ = yield Request(Operation('hf14a_select'))
            _classic(output, bound, size)
        output, code = yield Request(operation, timeout=OPERATION_TIMEOUT_MS[operation.kind], allow_failure=True)
        candidate = parse_target_key(output, operation) if not code else None
        if not candidate:
            raise ValueError(say('Ключ не найден этим методом. Повтор автоматически не запускается; подробности в журнале.',
                                'This method did not find a key. It is not retried automatically; see the journal for details.'))
        output, _ = yield Request(Operation('hf14a_select'))
        _classic(output, bound, size)
        sector, kind = (0, 'A') if method == 'Darkside' else (target_sector, target_type)
        check = Operation('mfc_check_candidate', classic_size=size,
                          known_sector=sector, known_type=kind, auth_key=candidate)
        output, code = yield Request(check, timeout=30000, allow_failure=True)
        if code or not parse_candidate_verification(output, sector, kind, candidate, size):
            raise ValueError(say('Найденный кандидат не прошёл аутентификацию и не добавлен в библиотеку.',
                                'The recovered candidate did not authenticate and was not added to the library.'))
        output, _ = yield Request(Operation('hf14a_select'))
        _classic(output, bound, size)
        result = {'uid': bound, 'classic_size': size, 'method': method,
                  'keys': [{'sector': sector, 'type': kind, 'key_hex': candidate}],
                  'verification': 'separate-key-authentication-and-uid'}
        (directory / 'classic-recovery.json').write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return result
    return flow
