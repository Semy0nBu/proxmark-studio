"""Pure fixture generation for an explicitly selected, backed-up 1K test card.

This module does not access hardware. It only accepts transport-configured
cards and preserves block0. ACL 7F0788 gives data blocks 000 and trailer 011:
both keys authenticate, key B remains secret and can restore the trailer.
NXP MF1S50YYX_V1, section8.7.2 / table7.
"""
from dataclasses import dataclass
import hashlib
import re
import secrets

DEFAULT_KEY = 'FFFFFFFFFFFF'
TEST_ACCESS = bytes.fromhex('7F078869')


@dataclass(frozen=True)
class TestCardFixture:
    uid: str
    original: bytes
    expected: bytes
    keys: tuple

    def key_file(self) -> bytes:
        return b''.join(bytes.fromhex(entry['key_hex']) for kind in ('A', 'B')
                        for entry in self.keys if entry['type'] == kind)

    def data_blocks(self, sector: int):
        if type(sector) is not int or not 0 <= sector < 16:
            raise ValueError('Expected 1K sector0..15')
        return [(block, self.expected[block*16:(block+1)*16])
                for block in range(sector*4, sector*4+3) if block != 0]


def create_1k_fixture(original: bytes, uid: str, verified_keys: list) -> TestCardFixture:
    uid = uid.upper()
    if not re.fullmatch(r'(?:[0-9A-F]{8}|[0-9A-F]{14})', uid):
        raise ValueError('Expected confirmed4/7-byte UID')
    if type(original) is not bytes or len(original) != 1024 or not original.startswith(bytes.fromhex(uid)):
        raise ValueError('Expected complete1024-byte backup with matching block0')
    slots = {(entry['sector'], entry['type']): entry['key_hex'].upper() for entry in verified_keys}
    if len(slots) != 32 or any(slots.get((sector, kind)) != DEFAULT_KEY
                               for sector in range(16) for kind in ('A', 'B')):
        raise ValueError('This fixture writer requires all32 transport keys verified')
    for sector in range(16):
        trailer = original[(sector*4+3)*16:(sector*4+4)*16]
        if trailer != bytes.fromhex('FFFFFFFFFFFFFF078069FFFFFFFFFFFF'):
            raise ValueError('Expected transport configuration in every trailer')
    blocks = [original[i:i+16] for i in range(0, 1024, 16)]
    keys, used = [], {DEFAULT_KEY, '000000000000'}
    for sector in range(16):
        pair = []
        for kind in ('A', 'B'):
            if sector == 0 and kind == 'A':
                value = DEFAULT_KEY
            else:
                value = secrets.token_hex(6).upper()
                while value in used:
                    value = secrets.token_hex(6).upper()
                used.add(value)
            keys.append({'sector': sector, 'type': kind, 'key_hex': value})
            pair.append(bytes.fromhex(value))
        for block in range(sector*4, sector*4+3):
            if block:
                suffix = hashlib.sha256(f'{uid}:{block}'.encode('ascii')).hexdigest()[:5]
                blocks[block] = f'PM3TEST {block:02d} {suffix}'.encode('ascii')
        blocks[sector*4+3] = pair[0] + TEST_ACCESS + pair[1]
    expected = b''.join(blocks)
    if len(expected) != 1024 or expected[:16] != original[:16]:
        raise ValueError('Invalid generated fixture')
    return TestCardFixture(uid, original, expected, tuple(keys))
