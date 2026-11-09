import unittest
from pm3gui.testcard import create_1k_fixture, TEST_ACCESS


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.uid = 'A1B2C3D4'
        data = bytearray(1024)
        data[:4] = bytes.fromhex(self.uid)
        for sector in range(16):
            data[(sector*4+3)*16:(sector*4+4)*16] = bytes.fromhex('FFFFFFFFFFFFFF078069FFFFFFFFFFFF')
        self.original = bytes(data)
        self.keys = [{'sector': sector, 'type': kind, 'key_hex': 'FFFFFFFFFFFF'}
                     for sector in range(16) for kind in ('A', 'B')]

    def test_preserves_manufacturer_block_and_never_lists_it_for_writing(self):
        fixture = create_1k_fixture(self.original, self.uid, self.keys)
        self.assertEqual(fixture.expected[:16], self.original[:16])
        self.assertEqual(len(fixture.expected), 1024)
        blocks = [block for sector in range(16) for block, _ in fixture.data_blocks(sector)]
        self.assertEqual(len(blocks), 47)
        self.assertNotIn(0, blocks)
        self.assertTrue(all(len(fixture.expected[block*16:(block+1)*16]) == 16 for block in blocks))

    def test_keys_are_unique_except_one_explicit_dictionary_seed_and_keyfile_order_matches(self):
        fixture = create_1k_fixture(self.original, self.uid, self.keys)
        self.assertEqual(fixture.keys[0], self.keys[0])
        self.assertEqual(len({item['key_hex'] for item in fixture.keys}), 32)
        keyfile = fixture.key_file()
        self.assertEqual(len(keyfile), 192)
        for item in fixture.keys:
            offset = (item['sector'] + (16 if item['type'] == 'B' else 0))*6
            self.assertEqual(keyfile[offset:offset+6].hex().upper(), item['key_hex'])

    def test_access_conditions_have_correct_redundancy_and_keep_b_private(self):
        byte6, byte7, byte8 = TEST_ACCESS[:3]
        c1, c2, c3 = byte7 >> 4, byte8 & 15, byte8 >> 4
        self.assertEqual(byte6 & 15, c1 ^ 15)
        self.assertEqual(byte6 >> 4, c2 ^ 15)
        self.assertEqual(byte7 & 15, c3 ^ 15)
        self.assertEqual([(c1 >> n & 1, c2 >> n & 1, c3 >> n & 1) for n in range(4)],
                         [(0, 0, 0)]*3+[(0, 1, 1)])

    def test_wrong_uid_partial_backup_or_nontransport_card_are_rejected(self):
        for uid, data, keys in [('11223344', self.original, self.keys),
                                (self.uid, self.original[:-1], self.keys),
                                (self.uid, self.original, self.keys[:-1]),
                                (self.uid, self.original[:-16]+bytes(16), self.keys)]:
            with self.subTest(uid=uid, size=len(data)), self.assertRaises(ValueError):
                create_1k_fixture(data, uid, keys)
