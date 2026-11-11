"""Offline command catalog contracts; synthetic help and temporary executables."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from pm3gui.catalog_snapshot import (CatalogCancelled, CatalogError, CatalogSnapshot,
    _run_offline, capture_catalog, compare_catalogs, decode_help, executable_sha256,
    load_catalog, parse_fulltext, require_confirmed_command, save_catalog, syntax_fingerprint, version_identity)
from pm3gui.i18n import set_language


HELP = 'List commands\n\nusage:\n    help [-h]\n\noptions:\n    -h, --help                     Show help\n'
READ = 'Read a block\n\nusage:\n    hf test read [-h] --block <dec>\n\noptions:\n    -h, --help                     Show help\n    --block <dec>                  Block number\n\nexamples/notes:\n    hf test read --block 1\n'


def dump(rows=None):
    rows = rows or [('help', True, HELP), ('hf test read', False, READ)]
    return ('Proxmark3 command dump\n' + ''.join('\n'+'-'*87+'\n'+name+'\n'+
        'available offline: '+('yes' if offline else 'no')+'\n\n'+text+'\n'
        for name, offline, text in rows)+'\nFull help dump done.\n').encode('utf-8')


class CatalogTests(unittest.TestCase):
    def setUp(self):
        set_language('en');self.addCleanup(lambda:set_language('ru'))
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = self.root/'client.exe';self.client.write_bytes(b'synthetic client version one')
        self.calls = []

    def probe(self, executable, argument, timeout, cancelled):
        self.calls.append((str(executable), argument))
        return {'--help':b'options: --fulltext --version', '--fulltext':dump(), '--version':b'RRG/test v1'}[argument]

    def capture(self):
        return capture_catalog(self.client, probe=self.probe)

    def test_parser_preserves_menus_empty_help_and_known_offline_state(self):
        rows = parse_fulltext(dump([('help', True, HELP), ('hf test', True, 'read { Reader }'),
                                    ('hf test config', False, '')]))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows['hf test config']['help'], '')
        self.assertIs(rows['help']['offline'], True)

    def test_truncated_output_is_never_a_partial_success(self):
        for text in (b'', dump().replace(b'Full help dump done.', b''), dump().replace(b'Full help dump done.', b'done')):
            with self.subTest(text=text[-30:]), self.assertRaises(CatalogError):parse_fulltext(text)

    def test_complete_footer_does_not_hide_a_malformed_middle_entry(self):
        for text in (dump().replace(b'available offline: no',b'offline availability: no'),
                     dump().replace(b'hf test read\navailable', b'\navailable')):
            with self.assertRaises(CatalogError):parse_fulltext(text)

    def test_category_banner_is_not_part_of_the_preceding_commands_help(self):
        text = dump().replace(b'\nFull help dump done.',
            b'\n'+b'='*87+b'\nnfc type2\n\n { NFC Type 2 commands... }\n\nFull help dump done.')
        self.assertEqual(parse_fulltext(text)['hf test read']['help'], READ.strip())
        menu = 'help             Show help\n'
        banner = '\n'+'='*87+'\nnfc type2\n\n { NFC Type 2 commands... }\n'
        self.assertEqual(syntax_fingerprint(menu),syntax_fingerprint(menu+banner))

    def test_conflicting_duplicates_and_invalid_names_fail(self):
        same = [('help', True, HELP), ('help', True, HELP)]
        self.assertEqual(len(parse_fulltext(dump(same))), 1)
        invalid = [same[:-1]+[('help', False, HELP)], [('help', True, HELP), ('hf a; wipe', False, READ)],
                   [('help', True, HELP), ('hf  test', False, READ)]]
        for rows in invalid:
            with self.subTest(rows=rows), self.assertRaises(CatalogError):parse_fulltext(dump(rows))
        with self.assertRaises(CatalogError):parse_fulltext(dump().replace(b'offline: no', b'offline: maybe'))

    def test_mixed_windows_encoding_keeps_valid_utf8_and_strips_ansi(self):
        result = decode_help('Ключ '.encode()+b'\xb5 '+b'\x1b[31mred\x1b[0m\x07')
        self.assertEqual(result, 'Ключ µ red')

    def test_version_identity_excludes_session_log_and_private_paths(self):
        raw = b'[=] Session log C:/Users/private/.proxmark3/log.txt\r\nClient: Iceman v1 compiler: GCC\r\n'
        self.assertEqual(version_identity(raw), 'Client: Iceman v1 compiler: GCC')
        self.assertNotIn('private', version_identity(raw))

    def test_capture_uses_only_offline_switches_and_records_content_fingerprint(self):
        progress = []
        snapshot = capture_catalog(self.client, probe=self.probe, progress=progress.append)
        self.assertEqual([entry[1] for entry in self.calls], ['--help', '--fulltext', '--version'])
        self.assertEqual(progress, ['--help', '--fulltext', '--version'])
        self.assertEqual(snapshot.client_sha256, executable_sha256(self.client))
        self.assertTrue(snapshot.matches(self.client));self.assertEqual(snapshot.version, 'RRG/test v1')
        with self.assertRaises(TypeError):snapshot.commands['help']['help'] = 'modified'

    def test_capture_rejects_no_fulltext_support_without_fallback_commands(self):
        calls = []
        def probe(_exe, argument, *_):calls.append(argument);return b'legacy --help only'
        with self.assertRaises(CatalogError):capture_catalog(self.client, probe=probe)
        self.assertEqual(calls, ['--help'])

    def test_binary_replacement_during_capture_discards_result(self):
        def probe(executable, argument, *args):
            result = self.probe(executable, argument, *args)
            if argument == '--fulltext':self.client.write_bytes(b'synthetic client version two')
            return result
        with self.assertRaisesRegex(CatalogError, 'changed'):capture_catalog(self.client, probe=probe)

    def test_cancel_stops_before_any_process(self):
        with self.assertRaises(CatalogCancelled):capture_catalog(self.client, probe=self.probe, cancelled=lambda:True)
        self.assertEqual(self.calls, [])

    def test_cache_roundtrip_rejects_same_size_same_timestamp_content_replacement(self):
        snapshot = self.capture();cache = self.root/'profile'/'client-catalog.json';save_catalog(snapshot, cache)
        self.assertEqual(load_catalog(cache, self.client).to_dict(), snapshot.to_dict())
        stamp = self.client.stat();self.client.write_bytes(b'X'*stamp.st_size)
        import os
        os.utime(self.client, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertFalse(snapshot.matches(self.client))
        with self.assertRaisesRegex(CatalogError, 'changed'):load_catalog(cache, self.client)
        self.assertEqual(load_catalog(cache).client_sha256, snapshot.client_sha256)

    def test_cache_invalid_duplicate_or_wrong_schema_fails_closed(self):
        snapshot = self.capture();value = snapshot.to_dict();cache = self.root/'cache.json'
        invalid = [[], {'format':'Unknown'}, {**value, 'commands':value['commands']*2},
                   {**value, 'commands':[{'command':'help','help':'x','offline':1,'description':'x'}]},
                   {**value, 'client_sha256':'wrong'}, {**value, 'client_path':12},
                   {**value, 'version':'x'*4097}, {**value, 'version':''}, {**value, 'version':'\ud800'},
                   {**value, 'version':'bad\nversion'},
                   {**value, 'captured_at':'not a date'}, {**value, 'client_path':'bad\npath'},
                   {**value, 'commands':[{**value['commands'][0], 'help':'\ud800'}]}]
        for data in invalid:
            cache.write_text(json.dumps(data), encoding='utf-8')
            with self.subTest(data=data), self.assertRaises(CatalogError):load_catalog(cache)

    def test_failed_atomic_cache_replace_preserves_previous_catalog(self):
        snapshot = self.capture();cache = self.root/'cache.json';save_catalog(snapshot, cache)
        old = cache.read_bytes()
        with patch.object(Path, 'replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):save_catalog(replace(snapshot, version='new'), cache)
        self.assertEqual(cache.read_bytes(), old)
        self.assertEqual(list(self.root.glob('cache.json.*.tmp')), [])

    def test_comparison_distinguishes_added_removed_and_syntax_changes(self):
        first = self.capture()
        rows = dict(first.commands);rows.pop('hf test read')
        rows['hf new'] = {'command':'hf new','offline':False,'help':READ,'description':'New'}
        rows['help'] = {**rows['help'], 'offline':False}
        diff = compare_catalogs(first, replace(first, commands=rows))
        self.assertEqual(diff.added, ('hf new',));self.assertEqual(diff.removed, ('hf test read',))
        self.assertEqual(diff.changed, ('help',))

    def test_description_and_example_changes_do_not_invalidate_existing_form(self):
        self.assertEqual(syntax_fingerprint(READ), syntax_fingerprint(READ.replace('Read a block','Read this block').replace('Block number','Choose block').replace('--block 1','--block 2')))
        for changed in (READ.replace('--block <dec>','--page <dec>'), READ.replace('[-h] --block', '[-h] [--block')+' ]', READ.replace('<dec>', '<hex>')):
            self.assertNotEqual(syntax_fingerprint(READ), syntax_fingerprint(changed))

    def test_existing_forms_require_matching_binary_and_grammar(self):
        snapshot = self.capture();reference = dict(snapshot.commands)
        self.assertEqual(require_confirmed_command(snapshot, self.client, 'hf test read','--block 1',reference=reference)['command'], 'hf test read')
        with self.assertRaises(CatalogError):require_confirmed_command(None, self.client, 'help')
        with self.assertRaises(CatalogError):require_confirmed_command(snapshot, self.client, 'hf missing')
        with self.assertRaises(CatalogError):require_confirmed_command(snapshot, self.client, 'help', 'x; wipe')
        reference['hf test read'] = {**reference['hf test read'], 'help':READ.replace('--block','--page')}
        with self.assertRaisesRegex(CatalogError, 'syntax changed'):require_confirmed_command(snapshot, self.client, 'hf test read',reference=reference)
        self.client.write_bytes(b'another client')
        with self.assertRaises(CatalogError):require_confirmed_command(snapshot, self.client, 'help')

    def test_empty_help_does_not_confirm_unknown_legacy_syntax(self):
        snapshot = self.capture();rows = dict(snapshot.commands)
        rows['hf config'] = {'command':'hf config','offline':False,'help':'','description':''}
        snapshot = replace(snapshot, commands=rows)
        with self.assertRaisesRegex(CatalogError, 'syntax help'):require_confirmed_command(snapshot, self.client, 'hf config',reference=rows)

    def test_offline_availability_change_also_invalidates_the_existing_form(self):
        snapshot = self.capture();reference = {name:dict(row) for name,row in snapshot.commands.items()}
        reference['help']['offline'] = False
        with self.assertRaisesRegex(CatalogError, 'syntax changed'):
            require_confirmed_command(snapshot,self.client,'help',reference=reference)

    def test_offline_process_boundary_rejects_interactive_or_port_arguments(self):
        for argument in ('-p COM3', '-c hf search', '--flash', 'hf search'):
            with self.assertRaises(CatalogError):_run_offline(sys.executable, argument, 1)
        result = _run_offline(sys.executable, '--version', 5)
        self.assertIn(b'Python', result)

    def test_timeout_kills_child_without_accepting_partial_help(self):
        class Process:
            returncode = None
            def __init__(self):self.killed=False
            def wait(self, timeout=None):
                if timeout is not None:raise subprocess.TimeoutExpired('fake', timeout)
                self.returncode = -1;return self.returncode
            def poll(self):return self.returncode
            def kill(self):self.killed=True
        process = Process()
        with patch('pm3gui.catalog_snapshot.subprocess.Popen', return_value=process) as start:
            with self.assertRaisesRegex(CatalogError, 'timed out'):_run_offline(self.client, '--fulltext', 0.001)
        self.assertTrue(process.killed)
        self.assertEqual(start.call_args.args[0], [str(self.client), '--fulltext'])

    def test_errors_have_readable_russian_and_english_messages(self):
        error = CatalogError('The selected client does not advertise this command.')
        self.assertEqual(str(error), 'The selected client does not advertise this command.')
        set_language('ru');self.assertIn('Выбранный клиент',str(error))
        self.assertIn('код -5',str(CatalogError('The client could not export its command help (code -5).')))


if __name__ == '__main__':unittest.main()
