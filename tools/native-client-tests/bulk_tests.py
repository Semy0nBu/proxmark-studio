"""Build and execute the actual production bulk-download decoder/engine."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client-source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--compiler', type=Path, default=Path(r'D:\Tools\ProxSpace-PM3\msys2\ucrt64\bin\gcc.exe'))
    args = parser.parse_args()
    header = args.client_source.resolve()/'client/src/comms_bulk_download.h'
    if not header.is_file():parser.error('Actual production comms_bulk_download.h was not found')
    source = HERE/'test_bulk_download.c'
    out = args.output.resolve();out.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy();env['PATH'] = str(args.compiler.parent)+os.pathsep+env.get('PATH','')
    commands, tests = [], []
    for name, optimization in (('bulk-debug','-O0'), ('bulk-release','-O2')):
        executable = out/(name+'.exe')
        command = [str(args.compiler), '-std=c99', '-pedantic-errors', '-Wall', '-Wextra', '-Wswitch-enum', '-Werror',
                   optimization, '-I'+str(header.parent), str(source), '-o', str(executable)]
        commands.append(command)
        build = subprocess.run(command, capture_output=True, text=True, env=env, timeout=30)
        (out/(name+'-build.log')).write_text(build.stdout+build.stderr, encoding='utf-8')
        if build.returncode:raise SystemExit(build.stdout+build.stderr)
        result = subprocess.run([str(executable)], capture_output=True, text=True, env=env, timeout=20)
        (out/(name+'-test.log')).write_text(result.stdout+result.stderr, encoding='utf-8')
        tests.append({'name':name, 'returncode':result.returncode, 'output':result.stdout+result.stderr})
    passed = all(t['returncode'] == 0 for t in tests)
    files = (header, args.client_source.resolve()/'client/src/comms.c', source, Path(__file__))
    report = {'generated_utc':datetime.now(timezone.utc).isoformat(), 'passed':passed,
              'build_commands':commands, 'tests':tests, 'hardware_used':False,
              'production_header_executed':str(header), 'transport_io_mocked':True,
              'scope':['actual normalized NG decoder','segmented 512-byte and one-request modes',
                       'atomic output commit','bounds/coverage/status/terminal proof',
                       'single deadline/cancellation/disconnection','deterministic allocator failure'],
              'crc_scope':'CRC-discarded frame is represented as a missing chunk; this harness does not calculate wire CRC.',
              'sha256':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}
    path = out/'BULK_TEST_RESULT.json';path.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    for test in tests:print(test['name']+': '+test['output'],end='')
    print(path)
    raise SystemExit(0 if passed else 1)


if __name__ == '__main__':main()
