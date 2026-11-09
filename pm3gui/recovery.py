"""Bounded, inspectable MIFARE Classic recovery plan using individual RRG commands.

This module never opens a port. Each result belongs to exactly one pending step;
an attack's candidate is authenticated separately before becoming a known key.
"""
from __future__ import annotations
from .i18n import tr

from collections import deque
import re
import time

from .operations import (CLASSIC_GEOMETRIES, Operation, parse_candidate_verification,
                         parse_nonce_kind, parse_recovered_keys, parse_target_key,
                         readable_key_b_from_trailer)


class RecoveryPlan:
    def __init__(self, classic_size: str, found: list[dict], *, max_seconds=900,
                 max_target_failures=3, checked_candidates=()):
        if classic_size not in CLASSIC_GEOMETRIES:
            raise ValueError(tr('Неизвестный профиль Classic.'))
        if max_seconds <= 0 or max_target_failures < 1:
            raise ValueError(tr('Нужны положительные пределы восстановления.'))
        self.classic_size = classic_size
        self.sectors = CLASSIC_GEOMETRIES[classic_size][0]
        self._keys = {}
        for entry in found:
            self._validate_entry(entry)
            slot = (entry['sector'], entry['type'])
            if slot in self._keys and self._keys[slot]['key_hex'] != entry['key_hex'].upper():
                raise ValueError(tr('Разные подтверждённые значения одного ключа.'))
            self._keys[slot] = {**entry, 'key_hex': entry['key_hex'].upper()}
        self._deadline = time.monotonic() + max_seconds
        self.max_target_failures = max_target_failures
        self.events = []
        self.done = False
        self.stop_reason = ''
        self.nonce_kind = 'unknown'
        self._phase = 'trailers'
        self._probed = False
        self._recent_keys = []
        self._checked_candidates = {value.upper() for value in checked_candidates}
        if any(not re.fullmatch(r'[0-9A-F]{12}', value) for value in self._checked_candidates):
            raise ValueError(tr('Неверное значение проверенного словаря.'))
        self._pending = None
        self._queue = deque()
        self._visited_trailers = set()
        self._candidate_attempts = set()
        self._attack_attempts = set()
        self._readable_b = set()
        self._static_attempted = False
        self._darkside_attempted = False
        self._target_failures = 0
        self._failed_targets = set()
        self._nested_unusable = False
        self._candidate_source = {}
        self._authenticated_seeds = {slot for slot in self._keys if slot[1] == 'A'}

    def _validate_entry(self, entry):
        if (type(entry.get('sector')) is not int or not 0 <= entry['sector'] < self.sectors
                or entry.get('type') not in {'A', 'B'}
                or not re.fullmatch(r'[0-9A-Fa-f]{12}', entry.get('key_hex', ''))):
            raise ValueError(tr('Недопустимая подтверждённая пара сектор/ключ.'))

    @property
    def found(self):
        return [dict(self._keys[slot]) for slot in sorted(self._keys)]

    def _missing(self):
        return [(sector, kind) for sector in range(self.sectors) for kind in ('A', 'B')
                if (sector, kind) not in self._keys]

    def _finish(self, reason):
        self.done = True
        self.stop_reason = reason
        self.events.append(reason)
        return None

    def _operation(self, kind, **kwargs):
        return Operation(kind, classic_size=self.classic_size, **kwargs)

    def _issue(self, operation):
        self._pending = operation
        if operation.kind == 'mfc_nonce_probe':
            detail = tr('Определение типа PRNG')
        elif operation.kind == 'mfc_read_trailer':
            detail = tr('Сектор {0}: чтение доступного B через A', f'{operation.known_sector}')
        elif operation.kind == 'mfc_check_candidate':
            detail = tr('Сектор {0}, {1}: проверка кандидата', f'{operation.known_sector}', f'{operation.known_type}')
        elif operation.kind == 'mfc_darkside_seed':
            detail = tr('Darkside: поиск исходного A сектора 0')
        elif operation.kind == 'mfc_staticnested_all':
            detail = tr('Staticnested: один запуск для статического PRNG')
        else:
            method = 'hardnested' if 'hardnested' in operation.kind else 'nested'
            detail = (tr('Сектор {0}, {1}: {2}, исходный {3}{4}', f'{operation.target_sector}', f'{operation.target_type}', f'{method}', f'{operation.known_sector}', f'{operation.known_type}'))
        self.events.append(detail)
        return operation

    def next_operation(self):
        if self._pending is not None:
            raise ValueError(tr('Сначала обработайте результат текущего этапа.'))
        if self.done:
            return None
        if not self._missing():
            return self._finish(tr('Все ключи A/B получены.'))
        if time.monotonic() >= self._deadline:
            return self._finish(tr('Достигнут общий лимит времени; найденные ключи сохранены.'))
        if self._queue:
            return self._issue(self._queue.popleft())
        # A newly recovered A can expose readable B before any further attacks.
        for sector, kind in sorted(self._keys):
            if kind == 'A' and (sector, 'B') not in self._keys and sector not in self._visited_trailers:
                self._visited_trailers.add(sector)
                return self._issue(self._operation('mfc_read_trailer', known_sector=sector,
                                                  auth_key=self._keys[(sector, 'A')]['key_hex']))
        if self._phase in {'trailers', 'reuse'}:
            self._phase = 'reuse'
            values = list(dict.fromkeys(self._recent_keys + [entry['key_hex'] for entry in self.found]))[:8]
            for sector, kind in self._missing():
                opposite = self._keys.get((sector, 'B' if kind == 'A' else 'A'))
                candidates = list(dict.fromkeys(([opposite['key_hex']] if opposite else []) + values))
                for value in candidates:
                    if value in self._checked_candidates and value not in self._recent_keys:
                        continue
                    attempt = (sector, kind, value)
                    if attempt not in self._candidate_attempts:
                        self._candidate_attempts.add(attempt)
                        self._candidate_source[attempt] = 'reused'
                        return self._issue(self._operation('mfc_check_candidate', known_sector=sector,
                                                          known_type=kind, auth_key=value))
            self._phase = 'probe'
        if self._phase == 'probe':
            self._phase = 'attacks'
            if not self._probed:
                self._probed = True
                return self._issue(self._operation('mfc_nonce_probe'))
        if len(self._failed_targets) >= self.max_target_failures:
            return self._finish(tr('Несколько целей отвергают nested-аутентификацию (Auth2). Проверьте положение карты, её профиль и доступность секторов; повторы прекращены.'))
        if self.nonce_kind in {'static_encrypted', 'unknown'}:
            return self._finish(tr('Тип PRNG не позволяет выбрать подтверждённый метод автоматически. Нужна проверка профиля карты; найденные ключи сохранены.'))
        if not self._keys:
            if self.nonce_kind == 'weak' and not self._darkside_attempted:
                self._darkside_attempted = True
                return self._issue(self._operation('mfc_darkside_seed', target_sector=0))
            return self._finish(tr('Нет исходного ключа для nested-методов. Доступный поиск исходного ключа завершён.'))
        seeds = sorted((slot for slot in self._keys if slot not in self._readable_b),
                       key=lambda slot: (slot[1] != 'A', slot[0]))
        if not seeds:
            return self._finish(tr('Нет ключа, пригодного для исходной аутентификации.'))
        # B can contain readable data instead of an authentication key. Check it
        # before using it as a seed; A slots came from authentication already.
        seed = next((slot for slot in seeds if slot in self._authenticated_seeds), None)
        if seed is None:
            slot = seeds[0]
            value = self._keys[slot]['key_hex']
            self._candidate_source[(*slot, value)] = 'seed'
            return self._issue(self._operation('mfc_check_candidate', known_sector=slot[0],
                                              known_type=slot[1], auth_key=value))
        if self.nonce_kind == 'static':
            if self._static_attempted:
                return self._finish(tr('Ограниченный staticnested завершён; оставшиеся ключи не получены.'))
            self._static_attempted = True
            return self._issue(self._operation('mfc_staticnested_all', known_sector=seed[0],
                                              known_type=seed[1], auth_key=self._keys[seed]['key_hex']))
        for target in self._missing():
            local_seed = (target[0], 'B' if target[1] == 'A' else 'A')
            methods = ['mfc_hardnested_target']
            if self.nonce_kind == 'weak' and not self._nested_unusable:
                methods.insert(0, 'mfc_nested_target')
            anchors = sorted(self._authenticated_seeds - self._readable_b,
                             key=lambda slot: (slot != local_seed, slot[1] != 'A', abs(slot[0] - target[0])))
            # At most two distinct sources; do not retry an acquisition using
            # the same source/target after its one-byte rejection.
            for anchor in anchors[:2]:
                for method in methods:
                    attempt = (target, anchor, method)
                    rejected_pair = (target, anchor, 'rejected')
                    if attempt not in self._attack_attempts and rejected_pair not in self._attack_attempts:
                        self._attack_attempts.add(attempt)
                        return self._issue(self._operation(method, known_sector=anchor[0], known_type=anchor[1],
                                                          auth_key=self._keys[anchor]['key_hex'],
                                                          target_sector=target[0], target_type=target[1]))
        return self._finish(tr('Доступные адресные методы завершены; оставшиеся ключи не получены.'))

    def _accept(self, sector, kind, key, method, *, readable=False):
        slot = (sector, kind)
        key = key.upper()
        if slot in self._keys:
            if self._keys[slot]['key_hex'] != key:
                raise ValueError(tr('Новый результат противоречит подтверждённому ключу.'))
            return []
        entry = {'sector': sector, 'type': kind, 'key_hex': key, 'method': method}
        self._keys[slot] = entry
        self._recent_keys.insert(0, key)
        self._phase = 'reuse'
        if readable:
            self._readable_b.add(slot)
        else:
            self._authenticated_seeds.add(slot)
        self._target_failures = 0
        self._failed_targets.clear()
        self.events.append(tr('Получен {0}{1}: {2}', f'{sector}', f'{kind}', f'{method}'))
        return [dict(entry)]

    def _verify_candidate(self, sector, kind, value, source):
        if (sector, kind) in self._keys:
            return
        self._candidate_source[(sector, kind, value.upper())] = source
        self._queue.append(self._operation('mfc_check_candidate', known_sector=sector,
                                           known_type=kind, auth_key=value.upper()))

    def consume(self, operation, output, code=0):
        if operation != self._pending:
            raise ValueError(tr('Ответ не соответствует текущему этапу восстановления.'))
        self._pending = None
        if operation.kind == 'mfc_nonce_probe':
            self.nonce_kind = parse_nonce_kind(output) if code == 0 else 'unknown'
            self.events.append(f'PRNG: {self.nonce_kind}')
            return []
        if operation.kind == 'mfc_read_trailer':
            value = readable_key_b_from_trailer(output, operation.known_sector) if code == 0 else None
            if value:
                return self._accept(operation.known_sector, 'B', value, 'readable_trailer', readable=True)
            return []
        if operation.kind == 'mfc_check_candidate':
            slot = (operation.known_sector, operation.known_type)
            source = self._candidate_source.pop((*slot, operation.auth_key), 'reused')
            valid = code == 0 and parse_candidate_verification(output, *slot, operation.auth_key, self.classic_size)
            if source == 'seed':
                if valid:
                    self._authenticated_seeds.add(slot)
                else:
                    self._readable_b.add(slot)
                return []
            if valid:
                return self._accept(*slot, operation.auth_key, source)
            return []
        if code == 0:
            if operation.kind == 'mfc_staticnested_all':
                for entry in parse_recovered_keys(output, self.classic_size):
                    self._verify_candidate(entry['sector'], entry['type'], entry['key_hex'], 'staticnested')
                if self._queue:
                    return []
            else:
                value = parse_target_key(output, operation)
                if value:
                    target = (0, 'A') if operation.kind == 'mfc_darkside_seed' else (operation.target_sector, operation.target_type)
                    method = {'mfc_nested_target': 'nested', 'mfc_hardnested_target': 'hardnested',
                              'mfc_darkside_seed': 'darkside'}[operation.kind]
                    self._verify_candidate(*target, value, method)
                    return []
        # A one-byte Auth2 response supplies no nonce for calculation. Treat it
        # as a failed target, not as a reason to repeat the same acquisition.
        if re.search(r'Auth2 error len=1\b', output, re.I):
            self._target_failures += 1
            target = (operation.target_sector, operation.target_type)
            anchor = (operation.known_sector, operation.known_type)
            self._failed_targets.add(target)
            self._attack_attempts.add((target, anchor, 'rejected'))
            self.events.append(tr('Цель {0}{1} отвергла nested-аутентификацию.', f'{operation.target_sector}', f'{operation.target_type}'))
            return []
        if operation.kind == 'mfc_nested_target' and re.search(
                r'not vulnerable.*nested|unpredictable|failed.*(?:prng|calibrat)|not predictable', output, re.I):
            self._nested_unusable = True
            self.events.append(tr('Обычный nested не подходит этому PRNG; следующий метод — hardnested.'))
        if code != 0:
            self.events.append(tr('Этап завершился без подтверждённого ключа; повтор этой команды пропущен.'))
            return []
        return []


def write_complete_key_file(path, classic_size, found):
    """RRG key.bin: all A keys followed by all B keys, without unknown fillers."""
    count = CLASSIC_GEOMETRIES[classic_size][0]
    slots = {(entry['sector'], entry['type']): entry['key_hex'] for entry in found}
    if set(slots) != {(sector, kind) for sector in range(count) for kind in ('A', 'B')}:
        raise ValueError(tr('Нельзя создать полный key.bin с неизвестными ключами.'))
    data = b''.join(bytes.fromhex(slots[(sector, kind)]) for kind in ('A', 'B') for sector in range(count))
    if len(data) != count * 12:
        raise ValueError(tr('Неверная длина ключей.'))
    with open(path, 'xb') as output:
        output.write(data)
