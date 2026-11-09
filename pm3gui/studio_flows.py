"""Reviewable laboratory transactions. Generators issue one typed command at a time."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import json
import re
from uuid import uuid4
from .client_catalog import split_command, changes_data, uses_rf
from .operations import Operation, Detection, CLASSIC_GEOMETRIES, parse_detection, _path_token, _text
from .reading import ClassicPartialRead
from .writing import parse_block
from .studio_data import decode_ndef, import_profile_dump, load_samples, lf_identity, structured_dump


@dataclass(frozen=True)
class Request:
    command: str | Operation
    args: str = ''
    timeout: int = 45000
    allow_failure: bool = False
    allow_capture_stop: bool = False

    def operation(self):
        if isinstance(self.command, Operation):
            if self.args:raise ValueError('A typed request does not take extra arguments.')
            self.command.render()
            return self.command
        command, args = split_command(self.command + (' ' + self.args if self.args else ''))
        return Operation('expert_tool', client_command=command, client_args=args)

    @property
    def line(self):return self.operation().render()

    @property
    def modifying(self):
        if isinstance(self.command, Operation):return self.command.kind in {'mfc_write_block', 'mfu_write_page'}
        command, args = split_command(self.line)
        return changes_data(command, args)


def one_command(command, args='', timeout=120000):
    if command in {'mem spiffs tree', 'mem spiffs info'}:
        if args:raise ValueError('The fenced SPIFFS query accepts no arguments.')
        reader = spiffs_listing if command == 'mem spiffs tree' else spiffs_info
        return lambda directory: reader(directory, timeout=min(timeout, 45000))
    def flow(directory):
        output, code = yield Request(command, args, timeout)
        return {'output': output, 'exit_code': code, 'verification': 'client-output-only'}
    return flow


def identity(output):
    text = _text(output)
    values = set()
    for match in re.finditer(r'\b(?:UID|IDm|CSN)\)?\s*[.:]+\s*((?:[0-9A-F]{2}[ :\t]*){4,10})(?![0-9A-F])', text, re.I):
        value = re.sub('[ :\t]', '', match[1]).upper()
        if len(value) in {8, 14, 16, 20}:values.add(value)
    if len(values) != 1:raise ValueError('Expected one confirmed UID/IDm/CSN. Leave one card on the antenna.')
    return values.pop()


def classic_selected(size, uid, blocks, keys):
    blocks = tuple(sorted(set(blocks)))
    if size not in CLASSIC_GEOMETRIES or not blocks or any(type(b) is not int or not 0 <= b < CLASSIC_GEOMETRIES[size][1] // 16 for b in blocks):
        raise ValueError('Invalid Classic block selection.')
    capped=[];counts={}
    for entry in sorted(keys,key=lambda e:not e.get('verified',False)):
        slot=(entry['sector'],entry.get('type',entry.get('key_type')))
        if counts.get(slot,0)<8:
            capped.append({**entry,'type':slot[1]});counts[slot]=counts.get(slot,0)+1
    def flow(directory):
        output, _ = yield Request(Operation('hf14a_select'))
        if identity(output) != uid.upper():raise ValueError('Card UID does not match the selected library record.')
        reader = ClassicPartialRead(Detection('mfc', uid, 'MIFARE Classic', size, True), capped, directory)
        reader.todo = {s: [b for b in todo if b in blocks] for s, todo in reader.todo.items()}
        reader.sectors = [s for s in reader.sectors if reader.todo[s]]
        if not reader.sectors:raise ValueError('There are no known keys for the selected sectors.')
        reader.done = False
        try:
            while (op := reader.next_operation()) is not None:
                output, code = yield Request(op, allow_failure=True)
                reader.consume(output, code)
            dump = reader.dump()
            return {'dump': dump, 'uid': uid, 'protocol': 'MIFARE Classic ' + size.upper(),
                    'selected_blocks': list(blocks), 'read_blocks': sorted(reader.confirmed),
                    'verification': 'uid-guarded-selected-blocks'}
        finally:
            if not reader.done:reader.stop('Stopped; only UID-confirmed sectors were retained.')
    return flow


def magic_uid(new_uid, atqa, sak, size='1k', *, review=False, target_uid=''):
    if not re.fullmatch(r'(?:[0-9A-Fa-f]{8}|[0-9A-Fa-f]{14})', new_uid):raise ValueError('Gen1a UID must contain 4 or 7 bytes.')
    if not re.fullmatch('[0-9A-Fa-f]{4}', atqa) or not re.fullmatch('[0-9A-Fa-f]{2}', sak):raise ValueError('ATQA is two bytes; SAK is one byte.')
    if size not in CLASSIC_GEOMETRIES:raise ValueError('Invalid Classic geometry.')
    def flow(directory):
        output, _ = yield Request('hf 14a info')
        old_uid = identity(output)
        if target_uid and old_uid!=target_uid.upper():raise ValueError('Destination identity does not match.')
        if not re.search(r'gen\s*1a|gen1a|magic.*?1a', output, re.I):
            raise ValueError('The card was not positively identified as Magic Gen1a. Use its generation-specific action.')
        prefix = directory / 'magic-before'
        yield Request('hf mf csave', f'--{size} -f {_path_token(prefix)}')
        backup = import_profile_dump(prefix.with_suffix('.json') if prefix.with_suffix('.json').is_file() else prefix.with_suffix('.bin'), 'Magic Gen1a')
        if len(backup.data) != CLASSIC_GEOMETRIES[size][1]:raise ValueError('Full Magic backup was not obtained.')
        if review:
            from .wizard_plans import review_changes
            guard_output,_=yield Request(Operation('hf14a_select'))
            if identity(guard_output)!=old_uid:raise ValueError('Magic card changed during backup.')
            old_atqa=re.search(r'\bATQA:\s*([0-9A-F]{2})\s*([0-9A-F]{2})',output,re.I)
            old_sak=re.search(r'\bSAK:\s*([0-9A-F]{2})',output,re.I)
            if not old_atqa or not old_sak or len(old_uid)!=len(new_uid):raise ValueError('The reviewed UID wizard requires current ATQA/SAK and the same UID length.')
            before=bytes.fromhex(old_uid+old_atqa[1]+old_atqa[2]+old_sak[1]);after=bytes.fromhex(new_uid+atqa+sak)
            changed=yield from review_changes(directory,'Magic Gen1a UID',old_uid,before,after,unit=len(before),
                scope='UID / ATQA / SAK',backup=prefix)
            if not changed:return {'uid':old_uid,'dump':backup,'protocol':'Magic Gen1a','backup':str(prefix),'verification':'unchanged-fields-no-write'}
            guard=directory/'magic-guard'
            yield Request('hf mf csave', f'--{size} -f {_path_token(guard)}')
            paths=[guard.with_suffix(s) for s in ('.json','.bin') if guard.with_suffix(s).is_file()]
            if not paths or import_profile_dump(paths[0],'Magic Gen1a').data!=backup.data:raise ValueError('Magic contents changed after review.')
        output, _ = yield Request(Operation('hf14a_select'))
        if identity(output) != old_uid:raise ValueError('Card changed after the backup.')
        yield Request('hf mf csetuid', f'--uid {new_uid} --atqa {atqa} --sak {sak}')
        output, _ = yield Request('hf 14a info')
        if identity(output) != new_uid.upper():raise ValueError('Written UID was not confirmed.')
        got_atqa = re.search(r'\bATQA:\s*([0-9A-F]{2})\s*([0-9A-F]{2})', output, re.I)
        got_sak = re.search(r'\bSAK:\s*([0-9A-F]{2})', output, re.I)
        # Iceman csetuid reverses CLI ATQA bytes into block 0; reader reverses them back.
        if not got_atqa or (got_atqa[1] + got_atqa[2]).upper() != atqa.upper() or not got_sak or got_sak[1].upper() != sak.upper():
            raise ValueError('UID changed, but ATQA/SAK were not confirmed. See backup and log.')
        result={'old_uid': old_uid, 'uid': new_uid.upper(), 'backup': str(prefix), 'verification': 'uid-atqa-sak-readback'}
        if review:
            after_prefix=directory/'magic-after'
            yield Request('hf mf csave', f'--{size} -f {_path_token(after_prefix)}')
            paths=[after_prefix.with_suffix(s) for s in ('.json','.bin') if after_prefix.with_suffix(s).is_file()]
            if not paths:raise ValueError('Post-write Magic snapshot was not saved.')
            after_dump=import_profile_dump(paths[0],'Magic Gen1a')
            if len(after_dump.data)!=len(backup.data) or after_dump.data[16:]!=backup.data[16:]:raise ValueError('Magic data outside block 0 changed.')
            output,_=yield Request(Operation('hf14a_select'))
            if identity(output)!=new_uid.upper():raise ValueError('Final Magic identity did not match.')
            result.update(dump=after_dump,protocol='Magic Gen1a')
        return result
    return flow


def emulator_read(size, blocks):
    if size not in CLASSIC_GEOMETRIES or any(type(b) is not int or not 0 <= b < CLASSIC_GEOMETRIES[size][1] // 16 for b in blocks):raise ValueError('Invalid emulator addresses.')
    def flow(directory):
        values = {}
        for block in blocks:
            output, _ = yield Request('hf mf egetblk', f'--blk {block}')
            raw = parse_block(output, block)
            if raw is None:
                matches = re.findall(r'(?<![\w])((?:[0-9A-F]{2} ){15}[0-9A-F]{2})(?![\w])', _text(output), re.I)
                if len(set(matches)) == 1:raw = bytes.fromhex(matches[0])
            if raw is None:raise ValueError(f'Emulator block {block} was not received.')
            values[block] = raw.hex().upper()
        return {'blocks': values, 'verification': 'emulator-memory-read'}
    return flow


def emulator_write(block, data):
    if type(block) is not int or not 0 <= block <= 255 or not re.fullmatch('[0-9A-Fa-f]{32}', data):raise ValueError('Choose one emulator block and 16 bytes.')
    def flow(directory):
        before, _ = yield Request('hf mf egetblk', f'--blk {block}')
        (directory/'emulator-before.txt').write_text(before, encoding='utf-8')
        yield Request('hf mf esetblk', f'--blk {block} -d {data}')
        result = yield from emulator_read('4k', [block])(directory)
        if result['blocks'][block] != data.upper():raise ValueError('Emulator readback does not match.')
        result['verification'] = 'emulator-block-readback'
        return result
    return flow


def traces(protocol='14a', source=None, capture=False):
    if protocol not in {'14a', '14b', '15', 'mf', 'iclass', 'legic', 'felica', 'hitag', 'iso7816'}:raise ValueError('Unsupported trace protocol.')
    def flow(directory):
        if source is not None:
            yield Request('trace load', '-f ' + _path_token(Path(source), existing=True))
        if capture:
            sniff = {'14a':'hf 14a sniff', 'mf':'hf 14a sniff', '14b':'hf 14b sniff', '15':'hf 15 sniff',
                     'iclass':'hf iclass sniff', 'hitag':'lf hitag sniff'}.get(protocol)
            if not sniff:raise ValueError('Use a supported capture protocol or import a trace file.')
            yield Request(sniff, timeout=1800000, allow_capture_stop=True)
            yield Request('hw break', timeout=15000)
            yield Request('hw fpgaoff', timeout=15000)
        prefix = directory/'capture'
        yield Request('trace save', ('-1 ' if source is not None else '') + '-f ' + _path_token(prefix))
        output, _ = yield Request('trace list', ('--buffer ' if source is not None else '') + f'-t {protocol}')
        return {'output': output, 'trace_file': str(prefix.with_suffix('.trace')), 'verification': 'trace-client-decoder'}
    return flow


LF_READERS = {'EM410x':'lf em 410x reader', 'HID Prox':'lf hid reader', 'Indala':'lf indala reader',
              'AWID':'lf awid reader', 'ioProx':'lf io reader', 'FDX-B':'lf fdxb reader'}


def read_lf_profile(profile):
    command = LF_READERS[profile]
    def flow(directory):
        output, _ = yield Request(command)
        fields = lf_identity(profile, output)
        dump = structured_dump(fields, profile, fields['identifier'], 'lf-identifier')
        return {'dump': dump, 'uid': fields['identifier'], 'protocol': profile, 'fields': fields,
                'verification': 'identifier-only-not-full-chip-memory'}
    return flow


HF_DUMP_PROFILES = {
    'ISO15693 / NFC-V': ('hf 15 reader', 'hf 15 dump'),
    'DESFire': ('hf mfdes info', 'hf mfdes dump'),
    'FeliCa': ('hf felica reader', 'hf felica dump'),
    'iCLASS / PicoPass': ('hf iclass reader', 'hf iclass dump'),
    'LEGIC': ('hf legic info', 'hf legic dump'),
    'Topaz': ('hf topaz info', 'hf topaz dump'),
    'MIFARE Plus': ('hf mfp info', 'hf mfp dump'),
}


def read_hf_profile(profile, arguments='', *, require_complete=False):
    reader, dump_command = HF_DUMP_PROFILES[profile]
    # A library workflow always owns its destination, never a user's -f option.
    if re.search(r'(?:^|\s)(?:-f|--file|--filename)(?:\s|=|$)', arguments):raise ValueError('The output path is managed by this session.')
    def flow(directory):
        before, _ = yield Request(reader)
        uid = identity(before)
        directory.mkdir(parents=True,exist_ok=True)
        prefix = directory/'card'
        dump_output,_=yield Request(dump_command, arguments + ' -f ' + _path_token(prefix), timeout=300000)
        output, _ = yield Request(reader)
        if identity(output) != uid:raise ValueError('The card changed while reading. The file was not added to the library.')
        paths = [prefix.with_suffix(x) for x in ('.json', '.bin') if prefix.with_suffix(x).is_file()]
        if not paths:raise ValueError('The client did not create a dump file.')
        dump = import_profile_dump(paths[0], profile)
        if require_complete:
            family_type={'ISO15693 / NFC-V':'15693','iCLASS / PicoPass':'iclass','LEGIC':'legic'}[profile]
            if not str(dump.metadata.get('source_json',{}).get('FileType','')).lower().startswith(family_type) or dump.metadata.get('partial_dump'):
                raise ValueError('The backup lacks the required complete family JSON layout.')
            count=len(dump.data)//dump.block_size
            if profile=='ISO15693 / NFC-V':
                # Native dump returns SUCCESS and a padded full-size JSON even
                # after a timeout. Only verified per-block progress proves reads.
                progress=[int(n) for n in re.findall(r'\bblk[ \t]+(\d+)\b',_text(dump_output),re.I)]
                if not progress or max(progress)!=count:raise ValueError('ISO15693 backup did not confirm reading every block; padded zeros are not backup evidence.')
            elif profile=='iCLASS / PicoPass':
                counts=[int(n) for n in re.findall(r'saving dump file\s*-\s*(\d+) blocks read',_text(dump_output),re.I)]
                if counts!=[count]:raise ValueError('iCLASS did not confirm the backed-up block count.')
            else:
                from .wizard_plans import checked_output
                checked_output(dump_output)
                if re.search(r'\bfail\b|only managed|partial|incomplete',_text(dump_output),re.I):raise ValueError('LEGIC backup was incomplete.')
        dump.metadata.update(uid=uid, read_scope='client-family-dump', raw_session=str(directory))
        return {'dump': dump, 'uid': uid, 'protocol': profile, 'path': str(paths[0]),
                'verification': 'uid-guarded-family-dump; completeness-is-family-specific'}
    return flow


def ndef_read(profile='Ultralight / NTAG', key=''):
    commands = {'Ultralight / NTAG':'hf mfu ndefread', 'Classic':'hf mf ndefread', 'Type 4A':'nfc type4a read', 'Type 4B':'nfc type4b read'}
    command = commands[profile]
    if key and not re.fullmatch(r'(?:[0-9A-Fa-f]{8}|[0-9A-Fa-f]{12}|[0-9A-Fa-f]{32})', key):raise ValueError('Invalid NDEF key length.')
    def flow(directory):
        prefix = directory/('ndef-' + uuid4().hex[:8])
        output, _ = yield Request(command, ('-k ' + key + ' ' if key else '') + '-f ' + _path_token(prefix))
        path = prefix.with_suffix('.bin')
        if not path.is_file():raise ValueError('NDEF data file was not saved by the client. See its log.')
        raw = path.read_bytes();records = decode_ndef(raw)
        return {'ndef': raw, 'records': records, 'output': output, 'verification': 'ndef-decoded'}
    return flow


def ndef_write(raw, key='', *, review=False, target_uid=''):
    raw=bytes(raw)
    records = decode_ndef(raw)
    if not raw or not raw[0] & 0x80:
        raise ValueError('The Ultralight writer expects bare NDEF records, without a Type 2 TLV wrapper.')
    if key and not re.fullmatch(r'(?:[0-9A-Fa-f]{8}|[0-9A-Fa-f]{32})', key):raise ValueError('Invalid Ultralight/NTAG key.')
    def flow(directory):
        output, _ = yield Request('hf 14a reader', '--skip')
        uid = identity(output)
        if target_uid and uid!=target_uid.upper():raise ValueError('Destination identity does not match.')
        from .writing import mfu_profile, MFU_PROFILES
        output, _ = yield Request('hf mfu info', '-k ' + key if key else '')
        model = mfu_profile(output)
        _, first, last = MFU_PROFILES[model]
        overhead = 3 if len(raw) < 255 else 5
        if len(raw) + overhead > (last - first + 1) * 4:
            raise ValueError('NDEF plus its TLV container does not fit in the verified user-memory range.')
        before = yield from ndef_read('Ultralight / NTAG', key)(directory)
        # Keep the original bytes even if the next write fails or is cancelled.
        (directory/'ndef-backup.bin').write_bytes(before['ndef'])
        path = directory/'ndef-new.bin';path.write_bytes(raw)
        if review:
            from .wizard_plans import ReviewGate
            import hashlib
            output,_=yield Request('hf 14a reader','--skip')
            if identity(output)!=uid:raise ValueError('Card changed during NDEF backup.')
            summary={'uid':uid,'before':before['ndef'].hex(),'after':raw.hex()}
            (directory/'ndef-plan.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
            digest=hashlib.sha256(json.dumps(summary,sort_keys=True).encode()).hexdigest()
            if before['ndef']==raw:return {**before,'uid':uid,'verification':'unchanged-ndef-no-write'}
            from .studio_data import say
            approved=yield ReviewGate('NDEF',uid,say('Только NDEF-сообщение','NDEF message only'),({'address':'NDEF','before':before['ndef'].hex(' ').upper(),'after':raw.hex(' ').upper()},),str(directory/'ndef-backup.bin'),digest)
            if approved is not True:raise ValueError('NDEF write was not approved.')
            output,_=yield Request('hf mfu info','-k '+key if key else '')
            if mfu_profile(output)!=model:raise ValueError('Ultralight/NTAG model changed after review.')
            current=yield from ndef_read('Ultralight / NTAG',key)(directory)
            if current['ndef']!=before['ndef'] or path.read_bytes()!=raw:raise ValueError('NDEF source or target changed after review.')
        output, _ = yield Request('hf 14a reader', '--skip')
        if identity(output) != uid:raise ValueError('The card changed after the NDEF backup.')
        yield Request('hf mfu ndefwrite', ('-k ' + key + ' ' if key else '') + '-f ' + _path_token(path, existing=True))
        after = yield from ndef_read('Ultralight / NTAG', key)(directory)
        output, _ = yield Request('hf 14a reader', '--skip')
        if identity(output) != uid or after['records'] != records:raise ValueError('NDEF readback or UID did not match.')
        return {**after, 'uid': uid, 'verification': 'ndef-readback-and-uid'}
    return flow


def capture_signal(arguments=''):
    if re.search(r'[;|&`\r\n]', arguments):raise ValueError('One LF read operation is allowed.')
    def flow(directory):
        yield Request('lf read', arguments, timeout=120000)
        prefix = directory/'signal'
        yield Request('data save', '-f ' + _path_token(prefix))
        path = prefix.with_suffix('.pm3')
        values, rate = load_samples(path)
        return {'samples': values, 'sample_rate': rate, 'path': str(path), 'verification': 'captured-samples'}
    return flow


def spiffs_file(name):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,31}', name) or name in {'.', '..'}:
        raise ValueError('SPIFFS filename: 1–31 letters, digits, dot, dash or underscore.')
    return name


def download_spiffs(name):
    name = spiffs_file(name)
    def flow(directory):
        directory.mkdir(parents=True,exist_ok=True)
        listing = yield from spiffs_listing(directory)
        if name not in listing['entries']:raise ValueError('The download source was not listed by the board.')
        dest = directory/'download.bin'
        if dest.exists():raise ValueError('Use a fresh directory for the SPIFFS download.')
        yield Request('mem spiffs dump', f'-s {name} -d ' + _path_token(dest))
        if not dest.is_file():raise ValueError('The download file was not created.')
        if dest.stat().st_size != listing['entries'][name]:raise ValueError('The SPIFFS download size differs from the fenced listing.')
        return {'path': str(dest), 'filename': name, 'bytes': dest.stat().st_size, 'verification': 'fenced-listing-and-download-size'}
    return flow


def spiffs_tree(output):
    """Parse listing syntax only; spiffs_listing supplies completion evidence."""
    text=_text(output)
    entries={m[2]:int(m[1]) for m in re.finditer(r'\[\d+\]\s+(\d+)\s+B\s+\|--\s+([A-Za-z0-9_.-]{1,31})(?=\s|\(|$)',text,re.M)}
    if not entries and '<empty>' not in text:raise ValueError('The SPIFFS listing was not recognised; no file changes were started.')
    return entries


def _fenced_spiffs_body(output, code, query):
    text = _text(output)
    from .auto_capture import transport_failed
    heading = re.search(r'Flash Memory '+re.escape(query)+r'\s*\(SPIFFS\)', text, re.I)
    # A newly opened persistent client may include an earlier bootstrap version
    # in this _Run. Only the OS section after the tree heading is the fence.
    arm = re.compile(r'\[\s*ARM\s*\]', re.I).search(text, heading.end()) if heading else None
    os_reply = re.compile(r'^\s*(?:\[[=+!]\]\s*)?OS\.{2,}\s+\S[^\n]*$', re.M).search(text, arm.end()) if arm else None
    if (code or transport_failed(text, code) or re.search(r'\bOFFLINE\s+mode\b', text, re.I)
            or heading is None or arm is None or os_reply is None
            or not heading.end() < arm.start() < os_reply.start()):
        raise ValueError('The SPIFFS '+query+' did not complete with a fresh device OS reply; no file changes were started.')
    return text[heading.end():os_reply.start()]


def spiffs_listing(directory, *, timeout=45000):
    output, code = yield Request(Operation('spiffs_tree'), timeout=timeout)
    entries = spiffs_tree(_fenced_spiffs_body(output, code, 'tree'))
    return {'output':output, 'entries':entries, 'exit_code':code,
            'verification':'spiffs-listing-fenced-by-fresh-device-os'}


def spiffs_info(directory, *, timeout=45000):
    output, code = yield Request(Operation('spiffs_info'), timeout=timeout)
    body = _fenced_spiffs_body(output, code, 'info')
    info = {}
    for field, label in (('block_size','Logical block size'), ('page_size','Logical page size'),
                         ('max_open_files','Max open files'), ('max_path_length','Max path length')):
        matches = re.findall(re.escape(label)+r'\.{2,}\s+(\d+)\b', body, re.I)
        if len(matches) != 1 or int(matches[0]) <= 0:
            raise ValueError('The SPIFFS information response is incomplete.')
        info[field] = int(matches[0])
    rows = re.findall(r'^\s*(?:\[[^\]\n]+\]\s*)?spiffs\s+(\d+)\s+B\s+(\d+)\s+B\s+(\d+)\s+B\s+(\d+)%?\s+/\s*$', body, re.M | re.I)
    if len(rows) != 1:raise ValueError('The SPIFFS usage statistics were not confirmed.')
    total, used, free, percent = map(int, rows[0])
    if not total or used > total or free > total or used+free != total or percent > 100:
        raise ValueError('The SPIFFS usage statistics are inconsistent.')
    info.update(total_bytes=total, used_bytes=used, available_bytes=free, used_percent=percent)
    return {'output':output, 'info':info, 'exit_code':code,
            'verification':'spiffs-info-fenced-by-fresh-device-os'}


def upload_spiffs(name, payload, new_file=False):
    name=spiffs_file(name);payload=bytes(payload)
    if len(payload)>2*1024*1024:raise ValueError('SPIFFS upload exceeds 2 MiB.')
    def flow(directory):
        listing=yield from spiffs_listing(directory);entries=listing['entries']
        if new_file:
            if name in entries:raise ValueError('The new filename already exists on the board.')
        else:
            if name not in entries:raise ValueError('For a new file, select the explicit new-file option.')
            backup=yield from download_spiffs(name)(directory/'before')
            if Path(backup['path']).stat().st_size!=entries[name]:raise ValueError('Existing file backup has the wrong size.')
        frozen=directory/'upload.bin';frozen.write_bytes(payload)
        yield Request('mem spiffs upload','-s '+_path_token(frozen,existing=True)+' -d '+name,timeout=120000)
        after=yield from download_spiffs(name)(directory/'after')
        if Path(after['path']).read_bytes()!=payload:raise ValueError('SPIFFS readback does not match the uploaded bytes.')
        return {**after,'verification':'spiffs-backup-and-byte-readback' if not new_file else 'spiffs-new-file-byte-readback'}
    return flow


def change_spiffs(action, source, dest=''):
    source=spiffs_file(source)
    if action not in {'remove','copy','rename'}:raise ValueError('Unsupported file action.')
    if action!='remove':
        dest=spiffs_file(dest)
        if source==dest:raise ValueError('Source and destination must differ.')
    def flow(directory):
        listing=yield from spiffs_listing(directory);entries=listing['entries']
        if source not in entries:raise ValueError('Source file was not listed by the board.')
        if action!='remove' and dest in entries:raise ValueError('The destination already exists. Choose a new name.')
        before=yield from download_spiffs(source)(directory/'before')
        payload=Path(before['path']).read_bytes()
        if len(payload)!=entries[source]:raise ValueError('Backup size does not match the board listing.')
        args='-f '+source if action=='remove' else '-s '+source+' -d '+dest
        yield Request('mem spiffs '+action,args)
        listing=yield from spiffs_listing(directory);after=listing['entries']
        if action in {'remove','rename'} and source in after:raise ValueError('Source file still exists after the requested operation.')
        if action!='remove':
            result=yield from download_spiffs(dest)(directory/'after')
            if Path(result['path']).read_bytes()!=payload:raise ValueError('Destination file bytes did not match the original.')
        return {'backup':before['path'],'verification':'spiffs-listing-and-byte-readback' if action!='remove' else 'spiffs-backup-and-removal-listed'}
    return flow


def simulate_family(dump, profile, mfu_type=7):
    from .core import export_dump
    if dump.metadata.get('partial_dump'):
        raise ValueError('Use a complete family snapshot for this simulation profile.')
    document = dump.metadata.get('structured_document') or dump.metadata.get('source_json')
    if not isinstance(document, dict):raise ValueError('This profile requires the original PM3 JSON and its protocol metadata.')
    filetype = str(document.get('FileType', '')).lower()
    expected = {'Ultralight / NTAG': 'mfu', 'ISO15693 / NFC-V': '15693', 'FeliCa': 'felica', 'LEGIC': 'legic'}
    from .studio_data import is_felica_document
    matches=is_felica_document(document) if profile=='FeliCa' else profile in expected and filetype.startswith(expected[profile])
    if not matches:raise ValueError('The snapshot schema does not match the selected simulation family.')
    if profile == 'Ultralight / NTAG' and mfu_type not in {2, 7, 13, 14}:raise ValueError('Select an Ultralight simulation type.')
    def flow(directory):
        source = directory/'simulation.json'
        if dump.metadata.get('structured_document'):
            source.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
        else:export_dump(dump, source)
        yield Request('hw break', timeout=15000)
        yield Request('hw fpgaoff', timeout=15000)
        if profile == 'FeliCa':
            output, _ = yield Request('hf felica sim', '-f ' + _path_token(source, existing=True), timeout=1800000)
            return {'output': output, 'verification': 'felica-simulator-client-result; no-independent-ram-readback'}
        stem = {'Ultralight / NTAG': 'hf mfu', 'ISO15693 / NFC-V': 'hf 15', 'LEGIC': 'hf legic'}[profile]
        yield Request(stem + ' eload', '-f ' + _path_token(source, existing=True))
        prefix = directory/'simulation-readback'
        extra = ''
        if profile == 'LEGIC':
            if len(dump.data) not in {22, 256, 1024}:raise ValueError('Unsupported LEGIC memory size.')
            extra = f' --{len(dump.data)}'
        yield Request(stem + ' esave', '-f ' + _path_token(prefix) + extra)
        paths = [prefix.with_suffix(s) for s in ('.json', '.bin') if prefix.with_suffix(s).is_file()]
        if not paths:raise ValueError('Emulator readback was not saved.')
        readback = import_profile_dump(paths[0], profile)
        if readback.data != dump.data:raise ValueError('Emulator RAM does not match the selected snapshot.')
        args = '-t ' + str(mfu_type) if profile == 'Ultralight / NTAG' else extra
        if profile == 'ISO15693 / NFC-V':args = f'-b {dump.block_size}'
        output, _ = yield Request(stem + ' sim', args, timeout=1800000)
        return {'output': output, 'verification': 'emulator-ram-readback; external-reader-not-tested'}
    return flow


def lf_profile_arguments(fields, action):
    profile = fields['protocol']
    if profile not in LF_READERS or action not in {'clone', 'sim'}:raise ValueError('Unsupported LF profile/action.')
    if profile == 'EM410x':
        args='--id '+fields['identifier']
        if 'clock' in fields:
            if fields['clock'] not in (32,64):raise ValueError('EM410x clock must be 32 or 64.')
            args+=' --clk '+str(fields['clock'])
        return 'lf em 410x '+action,args
    stem = {'HID Prox':'hid', 'Indala':'indala', 'AWID':'awid', 'ioProx':'io', 'FDX-B':'fdxb'}[profile]
    def number(name,maximum):
        n=fields.get(name)
        if type(n) is not int or not 0 <= n <= maximum:raise ValueError('Missing or out-of-range LF field: '+name)
        return n
    if profile in {'HID Prox','Indala'}:
        value=fields['identifier']
        if not re.fullmatch('[0-9A-Fa-f]{1,56}',value) or profile=='HID Prox' and len(value)>24:raise ValueError('Invalid raw LF identifier.')
        width=((len(value)+1)//2)*2
        if profile=='Indala' and fields.get('bit_length') in {64,224}:width=fields['bit_length']//4
        value=value.zfill(width)
        args='--raw '+value
    elif profile == 'AWID':
        length=number('bit_length',50)
        if length not in ({26,34,37,50} if action=='clone' else {26,32,36,40}):
            raise ValueError('The client does not advertise this AWID format for the selected action.')
        args=f'--fmt {length} --fc {number("facility_code",65535)} --cn {number("card_number",4294967295)}'
    elif profile == 'ioProx':args=f'--vn {number("version",255)} --fc {number("facility_code",255)} --cn {number("card_number",65535)}'
    else:
        if type(fields.get('animal')) is not bool or type(fields.get('data_block')) is not bool:
            raise ValueError('Read this FDX-B card again to capture its animal/data-block flags.')
        if fields.get('rudi') or fields.get('reserved',0) or fields.get('user_info',0) or fields.get('replacement',0):
            raise ValueError('This FDX-B variant has fields the native clone command cannot reproduce.')
        args=f'-c {number("country",1023)} -n {number("national_code",(1<<38)-1)}'
        if fields.get('animal'):args+=' -a'
        if fields.get('data_block'):args+=' --extended '+f'{number("extended",0xffffff):06X}'
    return 'lf '+stem+' '+action,args


def clone_lf_profile(fields, *, review=False):
    from copy import deepcopy
    fields=deepcopy(fields)
    from .lf_writing import LFWritePlan, t55_block
    command,args=lf_profile_arguments(fields,'clone')
    def flow(directory):
        # Reuse the existing read-only T5577 preparation, never approve its EM write.
        probe=LFWritePlan('0000000000',64,directory/'blank-backup')
        while (op:=probe.next_operation()) is not None:
            output,code=yield Request(op);probe.consume(output,code)
        if not probe.ready:raise ValueError('Blank-tag backup was not completed.')
        if review:
            from .wizard_plans import ReviewGate
            from .studio_data import say
            from .lf_writing import em410x_blocks
            import hashlib
            plan={'profile':fields['protocol'],'identifier':fields['identifier'],'before':probe.backup,'factory':probe.trace,
                  'command':command,'args':args,'verification_scope':'decoded-identifier, factory-page and unrelated-blocks'}
            encoded=json.dumps(plan,sort_keys=True,indent=2)
            (directory/'lf-plan.json').write_text(encoded,encoding='utf-8')
            planned=em410x_blocks(fields['identifier'],fields.get('clock',64)) if fields['protocol']=='EM410x' else {}
            affected={'EM410x':2,'HID Prox':3,'AWID':3,'ioProx':2,'FDX-B':4}.get(fields['protocol'],7 if fields.get('bit_length')==224 else 2)
            plan_rows=tuple({'address':str(b),'before':f'{v:08X}','after':f'{planned[b]:08X}' if b in planned else
                f'{v:08X}' if b>affected else (say('Кодировщик клиента: ','Client encoder: ')+fields['protocol'])} for b,v in sorted(probe.backup.items()))
            approved=yield ReviewGate(fields['protocol']+' '+fields['identifier']+' → T5577',' / '.join(f'{v:08X}' for _,v in sorted(probe.trace.items())),
                say('Проверяются ID, заводская область и посторонние блоки. Для EM410x также сравниваются все 8 блоков.',
                    'ID, factory area and unrelated blocks are verified. EM410x also compares all 8 blocks.'),
                plan_rows,
                str(probe.directory/'backup-page0.json'),hashlib.sha256(encoded.encode()).hexdigest())
            if approved is not True:raise ValueError('LF write was not approved.')
            for block in range(8):
                output,_=yield Request('lf t55xx read',f'-b {block}')
                if t55_block(output,block)!=probe.backup[block]:raise ValueError('Blank-tag data changed after review.')
        for block in (1,2):
            output,_=yield Request('lf t55xx read',f'--pg1 -b {block}')
            if t55_block(output,block)!=probe.trace[block]:raise ValueError('The blank tag changed before writing.')
        output,_=yield Request('lf t55xx read','-b 0')
        if t55_block(output,0)!=probe.backup[0]:raise ValueError('Blank-tag configuration changed before writing.')
        output,_=yield Request(command,args,timeout=120000)
        if not re.search(r'\bDone!',output) or re.search(r'\b(?:failed|error|timeout)\b',output,re.I):raise ValueError('The client did not acknowledge the LF write.')
        yield Request('lf t55xx detect')
        output,_=yield Request(LF_READERS[fields['protocol']],timeout=45000)
        actual=lf_identity(fields['protocol'],output)
        # Firmware HID prints variable-width hex. Ignore only leading zero padding.
        want=fields['identifier'].upper();got=actual['identifier'].upper()
        equal=(got.lstrip('0')==want.lstrip('0')) if fields['protocol']!='FDX-B' else (actual['country'],actual['national_code'])==(fields['country'],fields['national_code'])
        if not equal:raise ValueError('The LF identifier read after writing does not match.')
        profile=fields['protocol']
        last_changed={'EM410x':2,'HID Prox':3,'AWID':3,'ioProx':2,'FDX-B':4}.get(profile,7 if fields.get('bit_length')==224 else 2)
        after_blocks={}
        for block in range(8):
            output,_=yield Request('lf t55xx read',f'-b {block}')
            value=t55_block(output,block);after_blocks[block]=value
            if block>last_changed and value!=probe.backup[block]:raise ValueError('An unrelated T5577 block changed after writing.')
            if review and profile=='EM410x' and value!=planned.get(block,probe.backup[block]):raise ValueError('T5577 block does not match the independently encoded EM410x plan.')
        (directory/'after-page0.json').write_text(json.dumps(after_blocks,indent=2),encoding='utf-8')
        for block in (1,2):
            output,_=yield Request('lf t55xx read',f'--pg1 -b {block}')
            if t55_block(output,block)!=probe.trace[block]:raise ValueError('Factory page changed after writing.')
        dump=structured_dump(actual,fields['protocol'],actual['identifier'],'lf-identifier')
        dump.metadata.update(target_chip='T5577',factory_page1=probe.trace)
        return {'fields':actual,'dump':dump,'uid':actual['identifier'],'protocol':fields['protocol'],
                'verification':'lf-all-blocks-identifier-and-factory-readback' if review and profile=='EM410x' else 'lf-identifier-factory-and-unrelated-blocks-readback; payload-not-an-independent-full-compare',
                'backup':str(probe.directory/'backup-page0.json')}
    return flow


HF_RESTORE_PROFILES = {'ISO15693 / NFC-V':'hf 15 restore', 'iCLASS / PicoPass':'hf iclass restore', 'LEGIC':'hf legic restore'}


def restore_hf_profile(source, profile, read_args, write_args, *, review=False, target_uid=''):
    from .core import export_dump
    from copy import deepcopy
    source=deepcopy(source)
    if profile not in HF_RESTORE_PROFILES:raise ValueError('This family has no whole-image restore command. Choose a file/block action.')
    if source.metadata.get('structured_document') or source.metadata.get('partial_dump'):raise ValueError('This writer requires a complete, flat family memory map in the original JSON.')
    ftype=str(source.metadata.get('source_json',{}).get('FileType','')).lower()
    expected_type={'ISO15693 / NFC-V':'15693','iCLASS / PicoPass':'iclass','LEGIC':'legic'}[profile]
    if not ftype.startswith(expected_type):raise ValueError('Snapshot FileType does not match the destination family.')
    if '__studio_source__.json' not in write_args:raise ValueError('The restore source path must be managed by the session.')
    if profile=='LEGIC' and re.search(r'--ob\b',write_args):raise ValueError('The verified restore uses exact bytes. Obfuscation/clone actions have their own profile commands.')
    if profile=='LEGIC' and read_args.strip():raise ValueError('Verified LEGIC backups use raw bytes without deobfuscation options.')
    if profile=='ISO15693 / NFC-V' and re.search(r'(?:^|\s)(?:-u|--uid)\b',write_args):raise ValueError('Destination UID is managed by the transaction.')
    def flow(directory):
        # Read-only preflight and immutable backup precede the first write.
        before=yield from read_hf_profile(profile,read_args,require_complete=review)(directory/'before')
        backup=before['dump'];uid=before['uid']
        if target_uid and uid!=target_uid.upper():raise ValueError('Destination identity does not match.')
        if len(backup.data)!=len(source.data) or backup.block_size!=source.block_size:raise ValueError('Source and destination memory geometries do not match.')
        expected=bytearray(backup.data)
        if profile=='LEGIC':expected[7:]=source.data[7:]
        elif profile=='iCLASS / PicoPass':
            first=re.search(r'--first\s+(\d+)',write_args);last=re.search(r'--last\s+(\d+)',write_args)
            if not first or not last:raise ValueError('Choose the first and last iCLASS blocks.')
            first,last=int(first[1]),int(last[1])
            if review and first<6:raise ValueError('The iCLASS wizard preserves configuration, UID and key blocks 0–5.')
            if not 0 <= first <= last < len(source.data)//source.block_size:raise ValueError('iCLASS range is out of bounds.')
            expected[first*source.block_size:(last+1)*source.block_size]=source.data[first*source.block_size:(last+1)*source.block_size]
        else:expected[:]=source.data
        frozen=directory/'restore-source.json';export_dump(source,frozen)
        frozen_bytes=frozen.read_bytes()
        (directory/'expected-memory.bin').write_bytes(expected)
        if review:
            from .wizard_plans import review_changes
            from .studio_data import say
            changed=yield from review_changes(directory,profile,uid,backup.data,expected,unit=backup.block_size,
                scope=say('Сравнивается выбранная память; служебные поля назначения сохраняются согласно профилю.',
                    'Selected memory is compared; destination service fields are preserved according to the profile.'),backup=directory/'before')
            if not changed:return {**before,'verification':'unchanged-memory-no-write'}
            current=yield from read_hf_profile(profile,read_args,require_complete=True)(directory/'guard')
            if current['uid']!=uid or current['dump'].data!=backup.data:raise ValueError('Destination contents changed after review.')
            if frozen.read_bytes()!=frozen_bytes:raise ValueError('Source file changed after review.')
        reader=HF_DUMP_PROFILES[profile][0]
        output,_=yield Request(reader)
        if identity(output)!=uid:raise ValueError('The card changed after the backup.')
        args=write_args.replace('__studio_source__.json',_path_token(frozen,existing=True))
        if profile=='ISO15693 / NFC-V':args+=' -u '+uid
        yield Request(HF_RESTORE_PROFILES[profile],args,timeout=300000)
        after=yield from read_hf_profile(profile,read_args,require_complete=review)(directory/'after')
        if after['uid']!=uid or after['dump'].data!=bytes(expected):raise ValueError('Restored memory or destination identity did not match readback.')
        return {'dump':after['dump'],'uid':uid,'protocol':profile,'backup':str(directory/'before'),
                'verification':'family-restore-memory-and-identity-readback'}
    return flow
