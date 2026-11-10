"""Read-only post-write comparison against a frozen effective HF write plan.

Authentication success is evidence for hidden keys; a failed authentication
alone is never a confirmed mismatch. No request in this flow writes a card.
"""
from copy import deepcopy
from dataclasses import asdict
import json
import re
from uuid import uuid4
from .operations import Operation, parse_detection
from .writing import parse_block, block_sector, access_conditions, mfu_profile
from .studio_flows import Request
from .connection_checks import version_probe_status
from .studio_data import say


def readback_flow(plan, binding, transport, verified_transport=None):
    entries = deepcopy(plan.entries)
    target, options = deepcopy(plan.target), deepcopy(plan.options)
    auth = deepcopy(plan.auth)
    trailer_keys = {}
    for entry in entries:
        if entry['trailer']:
            trailer_keys[block_sector(entry['block'])] = [
                (kind, entry[field][a:b]) for field in ('new', 'old')
                for kind, a, b in (('A', 0, 12), ('B', 20, 32))]

    def same_target(output, code):
        found = parse_detection(output)
        if (code or not found.uid or found.ambiguous_identity or found.uid != target.uid
                or found.family != options.family
                or options.family == 'mfc' and found.classic_size != target.classic_size):
            raise ValueError(say('UID или профиль карты не совпал с планом.', 'Card UID or profile does not match the plan.'))

    def read(block, pair):
        if options.family == 'mfc':
            return Operation('mfc_read_block', classic_size=target.classic_size, block=block, known_type=pair[0], auth_key=pair[1])
        return Operation('mfu_read_page', block=block, auth_key=options.mfu_key, secure_channel=options.secure_channel)

    def read_result(output, code, block):
        if code in {-2, -3, -4, -5, -8} or re.search(r'connection (?:lost|closed)|UART .*failed|invalid serial port|command execution time\s*out', output, re.I):
            raise ValueError(say('Связь прервалась при проверке.', 'Connection lost during readback.'))
        return parse_block(output, block, 16 if options.family == 'mfc' else 4) if code == 0 else None

    def flow(directory):
        output, code = yield Request('hw version', timeout=20000)
        channel = verified_transport() if verified_transport else None
        if not version_probe_status(output, transport, code, verified_transport=channel)['connected']:
            raise ValueError(say('Свежий отклик платы не подтверждён.', 'A fresh device reply was not confirmed.'))
        same_target(*(yield Request('hf search')))
        if options.family == 'mfu':
            output, code = yield Request(Operation('mfu_info', auth_key=options.mfu_key, secure_channel=options.secure_channel))
            if code or mfu_profile(output) != plan.model:
                raise ValueError(say('Модель карты не совпала.', 'Card model does not match.'))
        compared, matched = set(), set()
        for entry in entries:
            block = entry['block'];expected = bytes.fromhex(entry['new'])
            sector = block_sector(block)
            pairs = (trailer_keys.get(sector, []) +
                     [(k, v) for k, v in auth.get(sector, {}).items() if k in ('A', 'B')] +
                     [(entry['type'], entry['key']), ('A', options.key_a), ('B', options.key_b)]) if options.family == 'mfc' else [('', '')]
            pairs = list(dict.fromkeys(pairs))
            observed = []
            for pair in pairs:
                output, code = yield Request(read(block, pair), allow_failure=True)
                raw = read_result(output, code, block)
                if raw is not None:
                    observed.append((pair, raw))
                    if not entry['trailer']:break
            if not observed:continue
            if not entry['trailer']:
                compared.add(block)
                if observed[0][1] == expected:matched.add(block)
                continue
            # Trailer reads mask A and may mask B. Never compare masked bytes.
            if any(raw[6:10] != expected[6:10] for _, raw in observed):
                compared.add(block);continue
            cond = access_conditions(observed[0][1])[3]
            good_a = any(pair == ('A', expected[:6].hex().upper()) for pair, _ in observed)
            bad_a = any(pair[0] == 'A' and pair[1].upper() != expected[:6].hex().upper() for pair, _ in observed)
            if cond in {0, 1, 2}:
                good_b = all(raw[10:] == expected[10:] for _, raw in observed)
                bad_b = not good_b
            else:
                good_b = any(pair == ('B', expected[10:].hex().upper()) for pair, _ in observed)
                bad_b = any(pair[0] == 'B' and pair[1].upper() != expected[10:].hex().upper() for pair, _ in observed)
            if bad_a or bad_b:compared.add(block)
            elif good_a and good_b:compared.add(block);matched.add(block)
        same_target(*(yield Request('hf search')))
        report = {'version': 1, 'kind': 'write-postcheck', 'check_id': uuid4().hex,
                  'binding': asdict(binding), 'compared_blocks': sorted(compared), 'matched_blocks': sorted(matched),
                  'final_target_confirmed': True, 'verification': 'readback-of-effective-plan'}
        path = directory / 'write-postcheck.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8');temporary.replace(path)
        return {'binding': asdict(binding), 'compared_blocks': sorted(compared), 'matched_blocks': sorted(matched),
                'final_target_confirmed': True, 'report': str(path)}
    return flow
