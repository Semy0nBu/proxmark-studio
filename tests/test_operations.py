"""Synthetic read-operation fixtures; no serial ports are opened."""
import json
from pathlib import Path
import tempfile
import unittest

from pm3gui.operations import (
    CLASSIC_GEOMETRIES, Detection, Operation, identity_dump,
    key_file_for_session, parse_detection, parse_found_keys, parse_candidate_verification,
    parse_nonce_kind, parse_target_key, parse_trailer_block, readable_key_b_from_trailer,
    parse_antenna_measurement, parse_lf_sweep,
    parse_recovered_keys, recovery_key_file_for_session, sector_first_block, sector_trailer,
    validate_key_file, validate_recovery_key_file, validate_read_dump,
)


def classic_log(size="1k"):
    count = CLASSIC_GEOMETRIES[size][0]
    return "\n".join(f"[+] Sector... {sector:2d} block...{block:2d} ( ok )"
                     for sector in range(count)
                     for block in range(4 if sector < 32 else 16))


class DetectionTests(unittest.TestCase):
    def test_classic_named_and_clone_sak_profile(self):
        fixture = "[+] UID: 04 11 22 33 44 55 66\n[+] SAK: 08 [2]\n[+] Possible types:\n[+] Valid ISO14443-A tag found"
        result = parse_detection(fixture)
        self.assertEqual((result.family, result.classic_size, result.uid), ("mfc", "1k", "04112233445566"))
        self.assertIn("SAK", result.reason)
        result = parse_detection(fixture + "\nMIFARE Classic 4K")
        self.assertEqual(result.classic_size, "4k")

    def test_missing_multiple_and_unsupported_are_not_successful_reads(self):
        self.assertFalse(parse_detection("[-] No known/supported 13.56 MHz tags found").supported)
        multiple = parse_detection("UID: AA BB CC DD\nUID: 11 22 33 44\nMIFARE Classic 1K")
        self.assertFalse(multiple.supported)
        self.assertEqual(multiple.uid, "")
        unknown = parse_detection("UID: 11 22 33 44\nSAK: 00\nValid ISO14443-A tag found")
        self.assertFalse(unknown.supported)
        self.assertEqual(unknown.uid, "11223344")

    def test_mfu_and_lf_identifier(self):
        mfu = parse_detection("\x1b[32m[+] UID: 04 11 22 33 44 55 66\x1b[0m\nNTAG 216")
        self.assertEqual((mfu.family, mfu.protocol), ("mfu", "NTAG 216"))
        em = parse_detection("[+] EM 410x ID 0102030405\n[+] Valid EM410x ID found!")
        self.assertEqual((em.family, em.uid), ("em410x", "0102030405"))
        dump = identity_dump(em)
        self.assertEqual(dump.data, bytes.fromhex("0102030405"))
        self.assertEqual(dump.metadata["record_kind"], "identifier")
        with self.assertRaises(ValueError):
            identity_dump(mfu)


class OperationTests(unittest.TestCase):
    def test_bounded_lf_search_uses_finite_capture_and_separate_offline_decode(self):
        self.assertEqual(Operation('lf_capture_search_buffer').render(), 'lf read -v -s 30000')
        self.assertEqual(Operation('lf_search_buffer').render(), 'lf search -1')
        for kind in ('lf_capture_search_buffer', 'lf_search_buffer'):
            for fields in ({'sample_count':30000}, {'lf_divisor':88},
                           {'client_args':'; hw reset'}, {'auth_key':'FFFFFFFFFFFF'},
                           {'output_prefix':Path('capture')}):
                with self.subTest(kind=kind, fields=fields), self.assertRaises(ValueError):
                    Operation(kind, **fields).render()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pm3 operations ")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_commands_are_typed_and_paths_are_single_quoted_arguments(self):
        dictionary = self.root / "flipper keys.dic"
        dictionary.write_text("FFFFFFFFFFFF\n")
        keys = self.root / "keys.bin"
        keys.write_bytes(bytes(192))
        check = Operation("mfc_check_keys", dictionary_path=dictionary).render()
        self.assertEqual(check, f'hf mf fchk --1k --dump -f "{dictionary.as_posix()}"')
        read = Operation("mfc_dump", self.root / "card", key_file=keys).render()
        self.assertEqual(read, f'hf mf dump --1k -f "{(self.root / "card").as_posix()}" -k "{keys.as_posix()}"')
        self.assertEqual(Operation("mfc_info").render(), "hf 14a reader --skip")
        self.assertEqual(Operation("hf14a_select").render(), "hf 14a reader --skip")
        self.assertEqual(Operation("lf_em410x_read").render(), "lf em 410x reader")
        mfu = Operation("mfu_dump", self.root / "card", auth_key="aabbccdd").render()
        self.assertTrue(mfu.endswith("-k AABBCCDD"))

    def test_rejects_injection_wrong_key_geometry_and_existing_artifacts(self):
        for operation in [Operation("hf mf restore"), Operation("mfc_check_keys", classic_size="1k; hw reset"),
                          Operation("mfu_dump", self.root / "card", auth_key="FFFFFFFF; quit"),
                          Operation("mfu_dump", self.root / "card;quit"),
                          Operation("mfc_info", auth_key="FFFFFFFF")]:
            with self.subTest(operation=operation.kind), self.assertRaises(ValueError):
                operation.render()
        keyfile = self.root / "keys.bin"
        keyfile.write_bytes(bytes(60))
        with self.assertRaises(ValueError):
            Operation("mfc_dump", self.root / "card", key_file=keyfile).render()
        (self.root / "card.json").write_text("{}")
        with self.assertRaises(ValueError):
            Operation("mfu_dump", self.root / "card").render()
        dictionary = self.root / "bad;quit.dic"
        dictionary.touch()
        with self.assertRaises(ValueError):
            Operation("mfc_check_keys", dictionary_path=dictionary).render()

    def test_found_key_table_requires_positive_evidence_and_binary_crosscheck(self):
        output = "[+] 000 | 003 | FFFFFFFFFFFF | 1 | ------------ | 0\n[+] 001 | 007 | 000000000000 | 1 | A0A1A2A3A4A5 | 1\n"
        result = parse_found_keys(output)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0], {"sector": 0, "type": "A", "key_hex": "FFFFFFFFFFFF"})
        raw = bytearray(b"\xff" * 192)
        raw[6:12] = bytes(6)
        raw[102:108] = bytes.fromhex("A0A1A2A3A4A5")
        keyfile = self.root / "keys.bin"
        keyfile.write_bytes(raw)
        self.assertEqual(validate_key_file(keyfile, output), result)
        with self.assertRaises(ValueError):
            validate_key_file(keyfile, "No keys found")
        raw[0] = 0
        keyfile.write_bytes(raw)
        with self.assertRaises(ValueError):
            validate_key_file(keyfile, output)
        with self.assertRaises(ValueError):
            parse_found_keys("[+] 000 | 007 | FFFFFFFFFFFF | 1 | ------------ | 0")
        self.assertEqual(parse_found_keys("[+] 000 | 003 | FFFFFFFFFFFF | 0 | A0A1A2A3A4A5 | X"), [])

    def test_four_k_trailer_geometry_and_key_filename(self):
        self.assertEqual(sector_trailer(31), 127)
        self.assertEqual(sector_trailer(32), 143)
        self.assertEqual(sector_trailer(39), 255)
        self.assertEqual(key_file_for_session(self.root, "04 11 22 33 44 55 66").name,
                         "hf-mf-04112233445566-key.bin")
        with self.assertRaises(ValueError):
            key_file_for_session(self.root, "AABB;quit")

    def test_targeted_commands_follow_four_k_geometry_and_exact_flags(self):
        seed = dict(classic_size="4k", known_sector=0, known_type="A", auth_key="a0a1a2a3a4a5")
        self.assertEqual([sector_first_block(s) for s in range(36, 40)], [192, 208, 224, 240])
        self.assertEqual(Operation("mfc_check_candidate", **seed).render(),
                         "hf mf chk --4k --tblk 0 -a -k A0A1A2A3A4A5 --no-default")
        self.assertEqual(Operation("mfc_read_trailer", **(seed | {"known_sector": 39})).render(),
                         "hf mf rdbl --blk 255 -a -k A0A1A2A3A4A5")
        self.assertEqual(Operation("mfc_nested_target", **seed, target_sector=36, target_type="B").render(),
                         "hf mf nested --4k --blk 0 -a -k A0A1A2A3A4A5 --tblk 192 --tb")
        self.assertEqual(Operation("mfc_hardnested_target", **seed, target_sector=39).render(),
                         "hf mf hardnested --blk 0 -a -k A0A1A2A3A4A5 --tblk 240 --ta")
        self.assertEqual(Operation("mfc_staticnested_all", **seed).render(),
                         "hf mf staticnested --4k --blk 0 -a -k A0A1A2A3A4A5")
        self.assertEqual(Operation("mfc_nonce_probe").render(), "hf 14a info")
        self.assertEqual(Operation("mfc_darkside_seed", target_sector=0).render(), "hf mf darkside --blk 0")
        self.assertEqual(Operation("mfc_nested_target").recommended_timeout_ms, 120000)
        self.assertEqual(Operation("mfc_hardnested_target").recommended_timeout_ms, 300000)
        invalid = [
            Operation("mfc_nested_target", **seed, target_sector=40),
            Operation("mfc_nested_target", **seed, target_sector=True),
            Operation("mfc_nested_target", **seed, target_sector=0),
            Operation("mfc_nested_target", **seed, target_sector=36, target_type="A;quit"),
            Operation("mfc_hardnested_target", **(seed | {"classic_size": "1k"}), target_sector=36),
            Operation("mfc_read_trailer", **(seed | {"known_type": "B"})),
            Operation("mfc_staticnested_all", **seed, target_sector=36),
            Operation("mfc_nonce_probe", auth_key="FFFFFFFFFFFF"),
            Operation("mfc_check_candidate", **(seed | {"auth_key": "FFFFFFFFFFFF;quit"})),
            Operation("mfc_darkside_seed", target_sector=1),
            Operation("mfc_darkside_seed", target_sector=0, target_type="B"),
            Operation("mfc_darkside_seed", target_sector=0, **seed),
        ]
        for operation in invalid:
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                operation.render()

    def test_candidate_check_requires_exact_slot_key_and_positive_result(self):
        output = "[+] 036 | 207 | A0A1A2A3A4A5 | 1 | B0B1B2B3B4B5 | 1\n"
        self.assertTrue(parse_candidate_verification(output, 36, "A", "A0A1A2A3A4A5", "4k"))
        self.assertFalse(parse_candidate_verification(output, 36, "A", "B0B1B2B3B4B5", "4k"))
        self.assertFalse(parse_candidate_verification(output, 37, "A", "A0A1A2A3A4A5", "4k"))
        self.assertFalse(parse_candidate_verification(output.replace("| 1", "| 0"), 36, "A", "A0A1A2A3A4A5", "4k"))

    def test_target_results_require_explicit_success_bound_to_target(self):
        seed = dict(classic_size="4k", known_sector=0, auth_key="FFFFFFFFFFFF", target_sector=36, target_type="B")
        nested = Operation("mfc_nested_target", **seed)
        valid = "[+] Target block  192 key type B -- found valid key [ A0A1A2A3A4A5 ]"
        self.assertEqual(parse_target_key(valid, nested), "A0A1A2A3A4A5")
        for bad in [valid.replace("192", "208"), valid.replace("type B", "type A"),
                    "Command completed successfully", "candidate key A0A1A2A3A4A5",
                    valid + "\n" + valid.replace("A0A1A2A3A4A5", "B0B1B2B3B4B5")]:
            self.assertIsNone(parse_target_key(bad, nested))
        hard = Operation("mfc_hardnested_target", **seed)
        output = "[=] 12 | 100 | Brute force completed. Key found: A0A1A2A3A4A5 | 42 |"
        self.assertEqual(parse_target_key(output, hard), "A0A1A2A3A4A5")
        self.assertIsNone(parse_target_key("Testing key: A0A1A2A3A4A5", hard))
        dark = Operation("mfc_darkside_seed", target_sector=0)
        self.assertEqual(parse_target_key("[+] Found valid key [ FFFFFFFFFFFF ]", dark), "FFFFFFFFFFFF")
        self.assertIsNone(parse_target_key("All key candidates failed", dark))

    def test_trailer_key_b_requires_valid_readable_acl_and_exact_block(self):
        def row(acl="FF 07 80", block=207, key="B0 B1 B2 B3 B4 B5"):
            return f"[=] {block} | 00 00 00 00 00 00 {acl} 69 {key} | ...."
        self.assertEqual(len(parse_trailer_block(row(), 36)), 16)
        self.assertEqual(readable_key_b_from_trailer(row(), 36), "B0B1B2B3B4B5")
        self.assertEqual(readable_key_b_from_trailer(row(key="00 00 00 00 00 00"), 36), "000000000000")
        self.assertIsNone(readable_key_b_from_trailer(row("FF 06 80"), 36))
        self.assertIsNone(readable_key_b_from_trailer(row("7F 07 88"), 36))  # trailer condition 011
        self.assertIsNone(readable_key_b_from_trailer(row(block=191), 36))
        self.assertIsNone(parse_trailer_block(row() + "\n" + row(key="FF FF FF FF FF FF"), 36))
        self.assertIsNone(parse_trailer_block("[!] Failed to read block 207", 36))

    def test_nonce_detection_accepts_only_explicit_classification(self):
        for text, expected in [
            ("[+] Prng detection..... weak", "weak"),
            ("[+] Prng detection..... hard", "hard"),
            ("[+] Static nonce....... yes", "static"),
            ("[+] Static enc nonce... yes", "static_encrypted"),
            ("[!] Prng detection...... fail", "unknown"),
            ("Normal nonce", "unknown"),
            ("Prng detection..... weak\nPrng detection..... hard", "unknown"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(parse_nonce_kind(text), expected)

    def test_recovery_render_seed_dictionary_and_suffix_are_validated(self):
        dictionary = self.root / "my keys.dic"
        dictionary.write_text("FFFFFFFFFFFF\n")
        operation = Operation("mfc_recover", classic_size="4k", dictionary_path=dictionary,
                              auth_key="a0a1a2a3a4a5", known_sector=0, known_type="A")
        self.assertEqual(operation.render(),
                         f'hf mf autopwn --4k -s 0 -a -k A0A1A2A3A4A5 -f "{dictionary.as_posix()}" --suffix gui-recovery')
        self.assertEqual(Operation("mfc_recover").render(), "hf mf autopwn --1k --suffix gui-recovery")
        self.assertEqual(recovery_key_file_for_session(self.root, "AABBCCDD").name,
                         "hf-mf-AABBCCDD-key-gui-recovery.bin")
        for operation in [Operation("mfc_recover", output_suffix="a; hf mf restore"),
                          Operation("mfc_recover", auth_key="FFFFFFFF"),
                          Operation("mfc_recover", auth_key="FFFFFFFFFFFF", known_sector=True),
                          Operation("mfc_recover", classic_size="1k", auth_key="FFFFFFFFFFFF", known_sector=16),
                          Operation("mfc_recover", auth_key="FFFFFFFFFFFF", known_sector=0, known_type="C"),
                          Operation("mfc_recover", known_sector=0),
                          Operation("mfc_recover", key_file=dictionary),
                          Operation("mfc_dump", known_sector=0)]:
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                operation.render()

    def test_recovered_table_legends_and_binary_use_only_latest_evidence(self):
        header = "[+] Sec | Blk | key A |res| key B |res\n[+] -----+-----+--------------+---+--------------+----\n"
        old_table = header + "[+] 000 | 003 | FFFFFFFFFFFF | 1 | A0A1A2A3A4A5 | D\n"
        final_table = header + "[+] 000 | 003 | ------------ | 0 | A0A1A2A3A4A5 | D\n[+] 001 | 007 | 112233445566 | N | 000000000000 | H\n"
        result = parse_recovered_keys(old_table + "progress...\n" + final_table)
        self.assertEqual(len(result), 3)
        self.assertFalse(any(entry["sector"] == 0 and entry["type"] == "A" for entry in result))
        self.assertEqual({entry["method"] for entry in result}, {"dictionary", "nested", "hardnested"})
        raw = bytearray(b"\xff" * 192)
        raw[6:12] = bytes.fromhex("112233445566")
        raw[96:102] = bytes.fromhex("A0A1A2A3A4A5")
        raw[102:108] = bytes(6)
        path = self.root / "recover.bin"
        path.write_bytes(raw)
        self.assertEqual(validate_recovery_key_file(path, old_table + final_table), result)
        self.assertEqual(parse_recovered_keys("[+] 000 | 003 | FFFFFFFFFFFF | D | A0A1A2A3A4A5 | N"), [])
        self.assertEqual(parse_found_keys(final_table), [])
        raw[6] = 0xFF
        path.write_bytes(raw)
        with self.assertRaises(ValueError):
            validate_recovery_key_file(path, final_table)
        with self.assertRaises(ValueError):
            parse_recovered_keys(header + "[+] 032 | 127 | FFFFFFFFFFFF | N | A0A1A2A3A4A5 | N", "4k")

    def test_recovery_four_k_all_legends_have_expected_array_offsets(self):
        header = "[+] Sec | Blk | key A |res| key B |res\n"
        keys_a = [sector.to_bytes(6, "big") for sector in range(40)]
        keys_b = [(sector + 100).to_bytes(6, "big") for sector in range(40)]
        legends = "DSURNHCA1"
        rows = [f"[+] {sector:03d} | {sector_trailer(sector):03d} | {keys_a[sector].hex()} | {legends[sector % len(legends)]} | {keys_b[sector].hex()} | H"
                for sector in range(40)]
        output = header + "\n".join(rows)
        path = self.root / "four-k-recover.bin"
        path.write_bytes(b"".join(keys_a + keys_b))
        entries = validate_recovery_key_file(path, output, "4k")
        self.assertEqual(len(entries), 80)
        self.assertEqual(next(entry["key_hex"] for entry in entries if entry["sector"] == 39 and entry["type"] == "B"), keys_b[39].hex().upper())


class AntennaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pm3 antenna ")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def sweep(self):
        operation = Operation("antenna_sweep", output_prefix=self.root / "lf-sweep")
        command = operation.render()
        self.assertEqual(command, f'hw tune; data save -f "{(self.root / "lf-sweep").as_posix()}"')
        path = operation.output_prefix.with_suffix(".pm3")
        values = [-128] * 19 + [-78] * 237
        values[95] = values[94] = -50  # 78 * .512V; a quantized peak plateau
        values[88] = -58
        path.write_text("\n".join(map(str, values)) + "\n", encoding="ascii")
        output = ("[=] Measuring antenna characteristics...\n[=] -------- LF Antenna ----------\n"
                  "[+] 125.00 kHz ........... 40.00 V\n[+] 134.83 kHz ........... 36.00 V\n"
                  "[+] 125.00 kHz optimal.... 40.00 V\n[+] Approx. Q factor measurement\n"
                  "[+] Frequency bandwidth... 9.5\n[+] Peak voltage.......... 7.0\n"
                  "[+] LF antenna............ ok\n[=] -------- HF Antenna ----------\n"
                  "[+] 13.56 MHz............. 13.56 V\n[+] Approx. Q factor measurement\n"
                  "[+] Peak voltage.......... 2.4\n[+] HF antenna ( ok )\n"
                  "[=] -------- LF tuning graph ------------\n"
                  "[=] Q factor must be measured without tag on the antenna\n"
                  f"[+] Saved 256 bytes to PM3 file `{path.as_posix()}`\n")
        return operation, output, path

    @staticmethod
    def batch(band="HF", readings=(10000, 12000), frequency="125.00"):
        header = "[=] Measuring HF antenna\n" if band == "HF" else f"[=] Measuring LF antenna at {frequency} kHz\n"
        progress = "[=] 0 mV / 0 V / 65 Vmax   \r"
        progress += "".join(f"[=] {value} mV / {value // 1000} V / {max(readings) // 1000} Vmax   \r" for value in readings)
        return header + progress + (f"[=] Min....... {min(readings)} mV\n[=] Max....... {max(readings)} mV\n"
                                    f"[=] Average... {sum(readings) / len(readings):.3f} mV\n[=] Done!\n")

    def test_finite_commands_reject_infinite_modes_and_parameter_injection(self):
        self.assertEqual(Operation("antenna_hf").render(), "hf tune --iter 5 --value --verbose")
        self.assertEqual(Operation("antenna_lf", sample_count=1, lf_divisor=88).render(),
                         "lf tune --iter 1 --divisor 88 --value --verbose")
        self.assertEqual(Operation("antenna_field_off").render(), "hw fpgaoff")
        for operation in [Operation("antenna_hf", sample_count=0), Operation("antenna_hf", sample_count=21),
                          Operation("antenna_lf", sample_count=True), Operation("antenna_lf", sample_count="5; hw reset"),
                          Operation("antenna_lf", lf_divisor=18), Operation("antenna_lf", lf_divisor=256),
                          Operation("antenna_lf", lf_divisor=True), Operation("antenna_hf", lf_divisor=88),
                          Operation("antenna_sweep", self.root / "lf;sweep"),
                          Operation("antenna_sweep", self.root / "lf-sweep", sample_count=1),
                          Operation("antenna_field_off", auth_key="FFFFFFFFFFFF"),
                          Operation("antenna_lf", key_file=self.root / "keys.bin"),
                          Operation("hf14a_select", sample_count=1)]:
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                operation.render()
        (self.root / "lf-sweep.pm3").touch()
        with self.assertRaises(ValueError):
            Operation("antenna_sweep", self.root / "lf-sweep").render()

    def test_finite_stats_confirm_actual_samples_and_units(self):
        hf = Operation("antenna_hf", sample_count=2)
        result = parse_antenna_measurement(self.batch(), hf)
        self.assertEqual(result["samples_v"], [10, 12])
        self.assertEqual((result["min_v"], result["max_v"], result["average_v"], result["latest_v"], result["hf_v"]), (10, 12, 11, 12, 11))
        self.assertEqual(result["frequency_khz"], 13560)
        self.assertIsNone(result["lf125_v"])
        lf = Operation("antenna_lf", sample_count=2, lf_divisor=88)
        result = parse_antenna_measurement(self.batch("LF", frequency="134.83"), lf)
        self.assertAlmostEqual(result["frequency_khz"], 12000 / 89)
        self.assertEqual(result["lf134_v"], 11)
        self.assertIsNone(result["hf_v"])
        self.assertEqual(parse_antenna_measurement(self.batch(readings=(0, 0)), hf)["average_v"], 0)
        for output in [self.batch() + "timeout while waiting for Proxmark HF measure, aborting\n",
                       self.batch().replace("11000.000", "nan"), self.batch().replace("11000.000", "11500.000"),
                       self.batch().replace("Min....... 10000", "Min....... 9000"),
                       self.batch(readings=(10000,)), self.batch().replace("Done!", ""), self.batch() * 2]:
            with self.subTest(output=output[-80:]), self.assertRaises(ValueError):
                parse_antenna_measurement(output, hf)
        with self.assertRaises(ValueError):
            parse_antenna_measurement(self.batch("LF", frequency="134.20"), lf)
        with self.assertRaises(ValueError):
            parse_antenna_measurement(self.batch(), hf, -2)

    def test_sweep_reports_native_voltages_q_and_quantized_graph_separately(self):
        operation, output, path = self.sweep()
        result = parse_antenna_measurement(output, operation)
        self.assertEqual((result["lf125_v"], result["lf134_v"], result["lf_peak_khz"], result["lf_peak_v"], result["hf_v"]), (40, 36, 125, 40, 13.56))
        self.assertEqual((result["lf_q_bandwidth"], result["lf_q_voltage"], result["hf_q_voltage"]), (9.5, 7, 2.4))
        self.assertEqual((result["lf_status"], result["hf_status"]), ("ok", "ok"))
        self.assertEqual(len(result["lf_curve"]), 237)
        self.assertEqual(result["lf_curve"][0]["divisor"], 255)
        peak = next(point for point in result["lf_curve"] if point["divisor"] == 95)
        self.assertAlmostEqual(peak["voltage_v"], 39.936)
        self.assertEqual(result["curve_quantum_v"], .512)
        self.assertIsNone(result["graph_unavailable_reason"])
        for bad in [output.replace("Saved 256", "Saved 255"), output.replace("125.00 kHz optimal", "130.43 kHz optimal"),
                    output.replace("40.00 V", "30.00 V"), output.replace("HF antenna ( ok )", ""),
                    output.replace("Q factor must be measured without tag on the antenna", ""),
                    output + "[!] Antenna tuning failed\n", output * 2]:
            with self.subTest(output=bad[-90:]), self.assertRaises(ValueError):
                parse_antenna_measurement(bad, operation)
        original = path.read_text()
        for content in [original + "0\n", original.replace("-128", "-129", 1), original.replace("-128", "-127", 1),
                        "0\n" * 256, "-128\n" * 256, original.replace("-50", "nan", 1)]:
            path.write_text(content)
            with self.assertRaises(ValueError):
                parse_lf_sweep(path)
        path.write_text(original)

    def test_zero_lf_graph_preserves_missing_voltage_and_native_unusable_status(self):
        operation = Operation("antenna_sweep", self.root / "empty")
        output = ("[=] -------- LF Antenna ----------\n[!] LF antenna............ unusable\n"
                  "[=] -------- HF Antenna ----------\n[+] HF antenna ( unusable )\n"
                  "[-] All values are zero. Not showing LF tuning graph\n"
                  "[=] Q factor must be measured without tag on the antenna\n"
                  "[!] Graphbuffer is empty, nothing to save\n")
        result = parse_antenna_measurement(output, operation)
        self.assertIsNone(result["lf125_v"])
        self.assertIsNone(result["hf_v"])
        self.assertEqual(result["lf_curve"], [])
        self.assertEqual(result["graph_unavailable_reason"], "all_lf_values_zero")
        self.assertEqual(result["lf_status"], "unusable")


class DumpValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def save_json(self, filetype, uid, data, size):
        path = self.root / "card.json"
        root = {"FileType": filetype, "Card": {"UID": uid},
                "blocks": {str(i // size): data[i:i + size].hex() for i in range(0, len(data), size)}}
        path.write_text(json.dumps(root))
        return path

    def test_classic_requires_every_successful_block_not_just_succeeded_message(self):
        detection = Detection("mfc", "AABBCCDD", "MIFARE Classic 1K", "1k", True)
        path = self.save_json("mfc v2", detection.uid, bytes(1024), 16)
        dump = validate_read_dump(path, detection, classic_log())
        self.assertEqual(dump.metadata["read_validation"]["verified_blocks"], 64)
        for bad in ["Succeeded in dumping all blocks", classic_log().replace("Sector...  3 block... 2 ( ok )", ""),
                    classic_log() + "\n[-] Sector... 3 Block... 2 ( fail )", classic_log() + "\n[!] Access rights prevent reading sector... 2 block... 1 ( skip )"]:
            with self.subTest(log=bad[-80:]), self.assertRaises(ValueError):
                validate_read_dump(path, detection, bad)
        with self.assertRaises(ValueError):
            validate_read_dump(path, Detection("mfc", "11223344", "MFC", "1k", True), classic_log())
        with self.assertRaises(ValueError):
            validate_read_dump(path, detection, classic_log(), returncode=-2)
        malformed = json.loads(path.read_text())
        malformed["Card"] = None
        path.write_text(json.dumps(malformed))
        with self.assertRaises(ValueError):
            validate_read_dump(path, detection, classic_log())

    def test_classic_four_k_and_wrong_size(self):
        detection = Detection("mfc", "AABBCCDD", "MIFARE Classic 4K", "4k", True)
        path = self.save_json("mfc v2", detection.uid, bytes(4096), 16)
        self.assertEqual(validate_read_dump(path, detection, classic_log("4k")).metadata["read_validation"]["verified_blocks"], 256)
        path = self.save_json("mfc v2", detection.uid, bytes(1024), 16)
        with self.assertRaises(ValueError):
            validate_read_dump(path, detection, classic_log("4k"))

    def test_mfc_v2_truncated_header_requires_exact_layout_and_post_read_uid(self):
        full_uid = "AABBCCDD000804"
        detection = Detection("mfc", full_uid, "MIFARE Classic-compatible", "1k", True)
        data = bytes.fromhex(full_uid + "000000000000000000") + bytes(1008)
        self.assertEqual(len(data), 1024)
        path = self.save_json("mfc v2", full_uid[:8], data, 16)
        original = json.loads(path.read_text())
        original["Card"].update(SAK="08", ATQA="0400")
        path.write_text(json.dumps(original))
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            validate_read_dump(path, detection, classic_log())
        with self.assertRaises(ValueError):
            validate_read_dump(path, detection, classic_log(), confirmed_uid="11223344000804")
        dump = validate_read_dump(path, detection, classic_log(), confirmed_uid=full_uid)
        validation = dump.metadata["read_validation"]
        self.assertEqual(validation["uid_source"], "rf_select_before_and_after_read")
        self.assertEqual(validation["header_mismatch"]["declared_uid"], full_uid[:8])
        self.assertEqual(dump.metadata["source_json"]["Card"]["UID"], full_uid[:8])
        self.assertEqual(path.read_bytes(), before)
        original["Card"]["UID"] = "11223344"
        path.write_text(json.dumps(original))
        with self.assertRaises(ValueError):
            validate_read_dump(path, detection, classic_log(), confirmed_uid=full_uid)
        original["Card"]["UID"] = full_uid[:8]
        original["blocks"]["0"] = "00" + original["blocks"]["0"][2:]
        path.write_text(json.dumps(original))
        with self.assertRaises(ValueError):
            validate_read_dump(path, detection, classic_log(), confirmed_uid=full_uid)

    def test_post_read_uid_mismatch_rejects_an_otherwise_valid_dump(self):
        detection = Detection("mfc", "AABBCCDD", "MIFARE Classic 1K", "1k", True)
        path = self.save_json("mfc v2", detection.uid, bytes(1024), 16)
        with self.assertRaises(ValueError):
            validate_read_dump(path, detection, classic_log(), confirmed_uid="11223344")

    def test_mfu_rejects_partial_and_model_mismatches(self):
        detection = Detection("mfu", "04112233445566", "NTAG 213", supported=True)
        path = self.save_json("mfu", detection.uid, bytes(180), 4)
        output = "[+] Reading tag memory...\n[+] Saved to json file card.json"
        dump = validate_read_dump(path, detection, output)
        self.assertTrue(dump.metadata["read_validation"]["secret_fields_may_be_unreadable"])
        with self.assertRaises(ValueError):
            validate_read_dump(path, detection, output + "\n[!] Partial dump created. (44 of 45 blocks)")
        with self.assertRaises(ValueError):
            validate_read_dump(path, detection, "Saved to json file card.json")
        path = self.save_json("mfu", detection.uid, bytes(176), 4)
        with self.assertRaises(ValueError):
            validate_read_dump(path, detection, output)

    def test_mfu_binary_header_and_autodetected_model_are_crosschecked(self):
        detection = Detection("mfu", "04112233445566", "MIFARE Ultralight / NTAG", supported=True)
        data = bytes(range(180))
        path = self.save_json("mfu", detection.uid, data, 4)
        header = bytearray(56)
        header[11] = 44
        binary = path.with_suffix(".bin")
        binary.write_bytes(header + data)
        output = "[+] TYPE: NTAG 213 144bytes\n[+] Reading tag memory...\n[+] Saved to json file card.json"
        self.assertEqual(validate_read_dump(path, detection, output).metadata["read_validation"]["capacity_check"], "model_size")
        header[11] = 43
        binary.write_bytes(header + data)
        with self.assertRaises(ValueError):
            validate_read_dump(path, detection, output)
        binary.write_bytes(data)
        with self.assertRaises(ValueError):
            validate_read_dump(path, detection, output)
        with self.assertRaises(ValueError):
            validate_read_dump(path, detection, output.replace("NTAG 213 144bytes", "NTAG UNKNOWN"))


if __name__ == "__main__":
    unittest.main()
