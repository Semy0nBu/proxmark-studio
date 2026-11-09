"""Exercise the bounded recovery scheduler with synthetic client responses only."""

import unittest
from unittest.mock import patch

from pm3gui.operations import CLASSIC_GEOMETRIES, Operation, sector_first_block, sector_trailer
from pm3gui.recovery import RecoveryPlan, write_complete_key_file

from pathlib import Path
import tempfile


def known_keys(size="mini", missing=()):
    missing = set(missing)
    return [
        {"sector": sector, "type": kind, "key_hex": f"{0x112233440000 + sector * 2 + index:012X}"}
        for sector in range(CLASSIC_GEOMETRIES[size][0])
        for index, kind in enumerate(("A", "B"))
        if (sector, kind) not in missing
    ]


def slots(entries):
    return {(entry["sector"], entry.get("type", entry.get("key_type"))): entry["key_hex"]
            for entry in entries}


def checked_key(operation):
    """A one-sector positive chk table, with its real trailer block number."""
    key_a = operation.auth_key if operation.known_type == "A" else "------------"
    key_b = operation.auth_key if operation.known_type == "B" else "------------"
    result_a = int(operation.known_type == "A")
    result_b = int(operation.known_type == "B")
    return ("[+] Sec | Blk | key A        |res| key B        |res\n"
            f"[+] {operation.known_sector:03d} | {sector_trailer(operation.known_sector):03d} | "
            f"{key_a} | {result_a} | {key_b} | {result_b}\n")


def recovered_candidate(operation, key="A0A1A2A3A4A5"):
    if operation.kind == "mfc_hardnested_target":
        return f"[+] Brute force completed. Key found: {key}\n"
    if operation.kind == "mfc_darkside_seed":
        return f"[+] Found valid key [ {key} ]\n"
    return (f"[+] Target block {sector_first_block(operation.target_sector):4d} "
            f"key type {operation.target_type} -- found valid key [ {key} ]\n")


def trailer_response(sector, *, access="FF 07 80", key_b="A0 A1 A2 A3 A4 A5"):
    return ("[=]  # | data                                            | ascii\n"
            f"[+] {sector_trailer(sector):3d} | 00 00 00 00 00 00 {access} 69 {key_b} | ................\n")


class RecoveryPlanTests(unittest.TestCase):
    def next_operation(self, plan):
        operation = plan.next_operation()
        if operation is not None:
            # The scheduler must always produce an operation accepted by the
            # same typed command builder used by the real runner.
            operation.render()
        return operation

    def failed_response(self, operation):
        if operation.kind == "mfc_nonce_probe":
            return "[+] Prng detection..... weak\n"
        return "[-] No valid key found\n"

    def advance_to_attack(self, plan, *, nonce_kind="weak", history=None):
        for _ in range(200):
            operation = self.next_operation(plan)
            self.assertIsNotNone(operation, "Plan ended before the intended target was attempted")
            if history is not None:
                history.append(operation)
            if operation.kind in {"mfc_nested_target", "mfc_hardnested_target"}:
                return operation
            output = (f"[+] Prng detection..... {nonce_kind}\n" if operation.kind == "mfc_nonce_probe"
                      else self.failed_response(operation))
            plan.consume(operation, output)
        self.fail("Scheduler did not reach a target within its finite preparatory work")

    def test_complete_map_finishes_without_any_command(self):
        entries = known_keys("4k")
        plan = RecoveryPlan("4k", entries)
        self.assertIsNone(self.next_operation(plan))
        self.assertTrue(plan.done)
        self.assertEqual(slots(plan.found), slots(entries))

    def test_missing_a_is_never_fabricated_from_a_readable_trailer(self):
        plan = RecoveryPlan("mini", known_keys(missing={(4, "A")}))
        seen = []
        for _ in range(200):
            operation = self.next_operation(plan)
            if operation is None:
                break
            seen.append(operation.kind)
            plan.consume(operation, self.failed_response(operation))
        else:
            self.fail("Failed recovery did not terminate")
        self.assertNotIn("mfc_read_trailer", seen)
        self.assertNotIn((4, "A"), slots(plan.found))
        self.assertTrue(plan.done)

    def test_readable_b_is_evidence_without_requiring_b_authentication(self):
        plan = RecoveryPlan("mini", known_keys(missing={(4, "B")}))
        operation = self.next_operation(plan)
        self.assertEqual(operation.kind, "mfc_read_trailer")
        self.assertEqual((operation.known_sector, operation.known_type), (4, "A"))
        added = plan.consume(operation, trailer_response(4))
        self.assertEqual(slots(added), {(4, "B"): "A0A1A2A3A4A5"})
        self.assertEqual(slots(plan.found)[(4, "B")], "A0A1A2A3A4A5")
        self.assertIsNone(self.next_operation(plan))
        self.assertTrue(plan.done)

    def test_closed_or_invalid_acl_never_promotes_raw_b_bytes(self):
        # 7F 07 88 is valid C1/C2/C3=011 for the trailer: B is not readable.
        for access in ("7F 07 88", "00 00 00"):
            with self.subTest(access=access):
                plan = RecoveryPlan("mini", known_keys(missing={(4, "B")}))
                operation = self.next_operation(plan)
                self.assertEqual(operation.kind, "mfc_read_trailer")
                self.assertEqual(plan.consume(operation, trailer_response(4, access=access)), [])
                self.assertNotIn((4, "B"), slots(plan.found))

    def test_attack_result_remains_candidate_until_separate_positive_check(self):
        initial = known_keys(missing={(4, "A")})
        plan = RecoveryPlan("mini", initial)
        attack = self.advance_to_attack(plan)
        self.assertEqual(plan.consume(attack, recovered_candidate(attack)), [])
        self.assertEqual(slots(plan.found), slots(initial))
        check = self.next_operation(plan)
        self.assertEqual(check.kind, "mfc_check_candidate")
        self.assertEqual((check.known_sector, check.known_type, check.auth_key),
                         (attack.target_sector, attack.target_type, "A0A1A2A3A4A5"))
        added = plan.consume(check, checked_key(check))
        self.assertEqual(slots(added), {(4, "A"): "A0A1A2A3A4A5"})
        self.assertIsNone(self.next_operation(plan))
        self.assertTrue(plan.done)

    def test_rejected_attack_candidate_never_changes_confirmed_map(self):
        initial = known_keys(missing={(4, "A")})
        plan = RecoveryPlan("mini", initial)
        attack = self.advance_to_attack(plan)
        plan.consume(attack, recovered_candidate(attack))
        check = self.next_operation(plan)
        self.assertEqual(check.kind, "mfc_check_candidate")
        self.assertEqual(plan.consume(check, "[-] No valid key found\n"), [])
        self.assertEqual(slots(plan.found), slots(initial))

    def test_success_after_transient_auth2_errors_is_checked_instead_of_discarded(self):
        for nonce_kind, attack_kind in (("weak", "mfc_nested_target"),
                                        ("hard", "mfc_hardnested_target")):
            with self.subTest(nonce_kind=nonce_kind):
                initial = known_keys(missing={(4, "A")})
                plan = RecoveryPlan("mini", initial)
                attack = self.advance_to_attack(plan, nonce_kind=nonce_kind)
                self.assertEqual(attack.kind, attack_kind)
                output = ("[#] AcquireEncryptedNonces: Auth2 error len=1\n" * 3
                          + recovered_candidate(attack))
                self.assertEqual(plan.consume(attack, output, code=0), [])
                self.assertEqual(slots(plan.found), slots(initial))
                check = self.next_operation(plan)
                self.assertEqual((check.kind, check.known_sector, check.known_type, check.auth_key),
                                 ("mfc_check_candidate", 4, "A", "A0A1A2A3A4A5"))
                self.assertEqual(slots(plan.consume(check, checked_key(check))),
                                 {(4, "A"): "A0A1A2A3A4A5"})
                self.assertIsNone(self.next_operation(plan))
                self.assertTrue(plan.done)

    def test_new_attack_key_is_reused_on_other_slots_without_another_probe(self):
        missing = {(sector, kind) for sector in range(36, 40) for kind in ("A", "B")}
        initial = known_keys("4k", missing)
        plan = RecoveryPlan("4k", initial)
        history = []
        attack = self.advance_to_attack(plan, history=history)
        new_key = "A0A1A2A3A4A5"
        self.assertNotIn(new_key, slots(initial).values())
        self.assertEqual(plan.consume(attack, recovered_candidate(attack, new_key)), [])
        check = self.next_operation(plan)
        history.append(check)
        self.assertEqual(check.kind, "mfc_check_candidate")
        first_slot = (attack.target_sector, attack.target_type)
        self.assertEqual(slots(plan.consume(check, checked_key(check))), {first_slot: new_key})
        reused_slots = set()
        for _ in range(100):
            operation = self.next_operation(plan)
            if operation is None:
                break
            history.append(operation)
            self.assertIn(operation.kind, {"mfc_read_trailer", "mfc_check_candidate"},
                          "A newly found key should be tried before any further attack or PRNG probe")
            if operation.kind == "mfc_check_candidate" and operation.auth_key == new_key:
                target = (operation.known_sector, operation.known_type)
                self.assertNotIn(target, slots(plan.found))
                added = plan.consume(operation, checked_key(operation))
                self.assertEqual(slots(added), {target: new_key})
                reused_slots.add(target)
            else:
                plan.consume(operation, self.failed_response(operation))
        else:
            self.fail("New-key reuse did not finish within bounded work")
        self.assertEqual(reused_slots, missing - {first_slot})
        self.assertEqual(sum(operation.kind == "mfc_nonce_probe" for operation in history), 1)
        self.assertEqual(len(plan.found), 80)
        self.assertTrue(plan.done)

    def test_dictionary_already_checked_by_fchk_skips_old_candidate_checks(self):
        missing = {(sector, kind) for sector in range(36, 40) for kind in ("A", "B")}
        initial = known_keys("4k", missing)
        checked = [entry["key_hex"].lower() for entry in initial]
        plan = RecoveryPlan("4k", initial, checked_candidates=checked)
        probe = self.next_operation(plan)
        self.assertEqual(probe.kind, "mfc_nonce_probe",
                         "The already checked dictionary must not be repeated on the eight missing slots")
        plan.consume(probe, "[+] Prng detection..... weak\n")
        attack = self.next_operation(plan)
        self.assertEqual(attack.kind, "mfc_nested_target")
        self.assertIn((attack.target_sector, attack.target_type), missing)
        self.assertEqual(slots(plan.found), slots(initial))

    def test_recent_authenticated_key_overrides_checked_dictionary_and_has_priority(self):
        missing = {(sector, kind) for sector in range(36, 40) for kind in ("A", "B")}
        initial = known_keys("4k", missing)
        new_key = "A0A1A2A3A4A5"
        checked = [entry["key_hex"] for entry in initial] + [new_key]
        plan = RecoveryPlan("4k", initial, checked_candidates=checked)
        history = []
        attack = self.advance_to_attack(plan, history=history)
        self.assertEqual((attack.target_sector, attack.target_type), (36, "A"))
        self.assertEqual(plan.consume(attack, recovered_candidate(attack, new_key)), [])
        check = self.next_operation(plan)
        history.append(check)
        self.assertEqual((check.kind, check.known_sector, check.known_type, check.auth_key),
                         ("mfc_check_candidate", 36, "A", new_key))
        self.assertEqual(slots(plan.consume(check, checked_key(check))), {(36, "A"): new_key})
        reused_slots = []
        for _ in range(5):
            operation = self.next_operation(plan)
            self.assertIsNotNone(operation)
            history.append(operation)
            if operation.kind == "mfc_read_trailer":
                plan.consume(operation, "[-] Cmd Error: 04\n")
                continue
            self.assertEqual(operation.kind, "mfc_check_candidate")
            self.assertEqual(operation.auth_key, new_key,
                             "A recent RF-confirmed key must precede the old dictionary, even if checked before")
            target = (operation.known_sector, operation.known_type)
            self.assertEqual(slots(plan.consume(operation, checked_key(operation))), {target: new_key})
            reused_slots.append(target)
            if len(reused_slots) == 2:
                break
        self.assertEqual(reused_slots, [(36, "B"), (37, "A")])
        # 37A has no known opposite key; its candidate must come from the
        # recent-key priority rather than the initial eight dictionary values.
        self.assertEqual(sum(operation.kind == "mfc_nonce_probe" for operation in history), 1)

    def test_nonzero_or_aborted_result_cannot_promote_an_attack_candidate(self):
        for code in (-2, 1):
            with self.subTest(code=code):
                initial = known_keys(missing={(4, "A")})
                plan = RecoveryPlan("mini", initial)
                attack = self.advance_to_attack(plan)
                self.assertEqual(plan.consume(attack, recovered_candidate(attack), code=code), [])
                self.assertEqual(slots(plan.found), slots(initial))

    def test_four_k_scheduler_targets_only_unknown_tail_and_keeps_geometry(self):
        missing = {(sector, kind) for sector in range(36, 40) for kind in ("A", "B")}
        initial = known_keys("4k", missing)
        plan = RecoveryPlan("4k", initial)
        seen = []
        for _ in range(300):
            operation = self.next_operation(plan)
            if operation is None:
                break
            seen.append(operation)
            if operation.kind == "mfc_check_candidate":
                self.assertIn((operation.known_sector, operation.known_type), missing)
                self.assertIn(operation.auth_key, slots(initial).values())
            elif operation.kind in {"mfc_nested_target", "mfc_hardnested_target"}:
                self.assertIn((operation.target_sector, operation.target_type), missing)
                self.assertIn(f"--tblk {192 + (operation.target_sector - 36) * 16}", operation.render())
            plan.consume(operation, self.failed_response(operation))
        else:
            self.fail("Recovery of missing 4K tail did not terminate")
        self.assertNotIn("mfc_read_trailer", [operation.kind for operation in seen])
        self.assertEqual(sum(operation.kind == "mfc_nonce_probe" for operation in seen), 1)
        self.assertLessEqual(sum(operation.kind == "mfc_staticnested_all" for operation in seen), 1)
        self.assertEqual(slots(plan.found), slots(initial))
        self.assertTrue(plan.done)

    def test_repeated_refused_targets_stop_without_restarting_whole_card(self):
        missing = {(sector, kind) for sector in range(36, 40) for kind in ("A", "B")}
        plan = RecoveryPlan("4k", known_keys("4k", missing), max_target_failures=3)
        attacks = []
        for _ in range(300):
            operation = self.next_operation(plan)
            if operation is None:
                break
            if operation.kind in {"mfc_nested_target", "mfc_hardnested_target"}:
                attacks.append(operation)
                output = "[#] AcquireEncryptedNonces: Auth2 error len=1\n" * 50
            else:
                output = self.failed_response(operation)
            plan.consume(operation, output)
        else:
            self.fail("Repeated NAKs were not bounded")
        self.assertGreater(len(attacks), 0)
        targets = {(operation.target_sector, operation.target_type) for operation in attacks}
        self.assertLessEqual(len(targets), 3)
        self.assertLessEqual(len(attacks), 6)
        self.assertEqual(len(set(attacks)), len(attacks), "A failed command was repeated")
        for target in targets:
            self.assertLessEqual(sum((operation.target_sector, operation.target_type) == target
                                     for operation in attacks), 2)
        self.assertTrue(plan.done)
        self.assertTrue(plan.stop_reason)
        self.assertIsNone(self.next_operation(plan))

    def test_total_deadline_stops_before_another_command_and_preserves_keys(self):
        initial = known_keys(missing={(4, "A")})
        with patch("pm3gui.recovery.time.monotonic", return_value=100):
            plan = RecoveryPlan("mini", initial, max_seconds=5)
        with patch("pm3gui.recovery.time.monotonic", return_value=106):
            self.assertIsNone(self.next_operation(plan))
        self.assertTrue(plan.done)
        self.assertTrue(plan.stop_reason)
        self.assertEqual(slots(plan.found), slots(initial))

    def test_only_the_pending_operation_can_supply_a_result(self):
        initial = known_keys(missing={(4, "A")})
        plan = RecoveryPlan("mini", initial)
        operation = self.next_operation(plan)
        with self.assertRaises(ValueError):
            self.next_operation(plan)
        with self.assertRaises(ValueError):
            plan.consume(Operation("mfc_nonce_probe", classic_size="mini"), checked_key(operation))
        self.assertEqual(slots(plan.found), slots(initial))
        plan.consume(operation, "[-] No valid key found\n")
        self.assertIsNotNone(self.next_operation(plan))

    def test_hard_nonce_chooses_hardnested_and_still_checks_its_candidate(self):
        initial = known_keys(missing={(4, "A")})
        plan = RecoveryPlan("mini", initial)
        for _ in range(100):
            operation = self.next_operation(plan)
            self.assertIsNotNone(operation)
            if operation.kind == "mfc_nonce_probe":
                plan.consume(operation, "[+] Prng detection..... hard\n")
                break
            plan.consume(operation, self.failed_response(operation))
        else:
            self.fail("No PRNG probe was scheduled")
        attack = self.next_operation(plan)
        self.assertEqual(attack.kind, "mfc_hardnested_target")
        self.assertEqual(plan.consume(attack, recovered_candidate(attack)), [])
        self.assertEqual(slots(plan.found), slots(initial))
        check = self.next_operation(plan)
        self.assertEqual(check.kind, "mfc_check_candidate")
        self.assertEqual(slots(plan.consume(check, checked_key(check))), {(4, "A"): "A0A1A2A3A4A5"})

    def test_static_nonce_runs_one_staticnested_and_verifies_its_result(self):
        initial = known_keys(missing={(4, "A")})
        plan = RecoveryPlan("mini", initial)
        for _ in range(100):
            operation = self.next_operation(plan)
            self.assertIsNotNone(operation)
            if operation.kind == "mfc_nonce_probe":
                plan.consume(operation, "[+] Static nonce..... yes\n")
                break
            plan.consume(operation, self.failed_response(operation))
        else:
            self.fail("No PRNG probe was scheduled")
        attack = self.next_operation(plan)
        self.assertEqual(attack.kind, "mfc_staticnested_all")
        output = ("[+] Sec | Blk | key A        |res| key B        |res\n"
                  "[+] 004 | 019 | A0A1A2A3A4A5 | C | ------------ | 0\n")
        self.assertEqual(plan.consume(attack, output), [])
        self.assertEqual(slots(plan.found), slots(initial))
        check = self.next_operation(plan)
        self.assertEqual(check.kind, "mfc_check_candidate")
        self.assertEqual(slots(plan.consume(check, checked_key(check))), {(4, "A"): "A0A1A2A3A4A5"})
        self.assertIsNone(self.next_operation(plan))

    def test_unknown_nonce_never_guesses_an_attack_method(self):
        initial = known_keys(missing={(4, "A")})
        plan = RecoveryPlan("mini", initial)
        seen = []
        for _ in range(100):
            operation = self.next_operation(plan)
            if operation is None:
                break
            seen.append(operation.kind)
            plan.consume(operation, "Unrecognized response\n")
        else:
            self.fail("Unknown PRNG did not terminate the plan")
        self.assertIn("mfc_nonce_probe", seen)
        self.assertTrue(all(kind in {"mfc_check_candidate", "mfc_nonce_probe"} for kind in seen))
        self.assertEqual(slots(plan.found), slots(initial))

    def test_no_seed_darkside_result_is_also_a_candidate_until_checked(self):
        plan = RecoveryPlan("mini", [])
        probe = self.next_operation(plan)
        self.assertEqual(probe.kind, "mfc_nonce_probe")
        plan.consume(probe, "[+] Prng detection..... weak\n")
        attack = self.next_operation(plan)
        self.assertEqual(attack.kind, "mfc_darkside_seed")
        self.assertEqual(plan.consume(attack, recovered_candidate(attack)), [])
        self.assertEqual(plan.found, [])
        check = self.next_operation(plan)
        self.assertEqual((check.kind, check.known_sector, check.known_type),
                         ("mfc_check_candidate", 0, "A"))
        self.assertEqual(slots(plan.consume(check, checked_key(check))), {(0, "A"): "A0A1A2A3A4A5"})

    def test_complete_key_file_has_a_then_b_arrays_without_unknown_fillers(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "recovered.bin"
            entries = known_keys("4k")
            with self.assertRaises(ValueError):
                write_complete_key_file(path, "4k", entries[:-1])
            self.assertFalse(path.exists())
            write_complete_key_file(path, "4k", entries)
            values = slots(entries)
            expected = b"".join(bytes.fromhex(values[(sector, kind)])
                                for kind in ("A", "B") for sector in range(40))
            self.assertEqual(path.read_bytes(), expected)
            with self.assertRaises(FileExistsError):
                write_complete_key_file(path, "4k", entries)
            self.assertEqual(path.read_bytes(), expected)


if __name__ == "__main__":
    unittest.main()
