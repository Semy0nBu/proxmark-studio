"""Compile and execute the actual production framed RX helper without hardware."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--client-source', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--compiler', type=Path, default=Path(r'D:\Tools\ProxSpace-PM3\msys2\ucrt64\bin\gcc.exe'))
    a = p.parse_args()
    header = a.client_source.resolve()/'client/src/uart/frame_reader.h'
    if not header.is_file(): p.error('Actual production frame_reader.h was not found')
    out = a.output.resolve(); out.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy(); env['PATH'] = str(a.compiler.parent) + os.pathsep + env.get('PATH','')
    commands, results = [], []
    for name in ('test_frame_reader', 'test_frame_tx_lifecycle'):
        exe = out/(name+'.exe')
        command = [str(a.compiler), '-std=c99', '-pedantic-errors', '-Wall', '-Wextra', '-Werror',
                   '-O2', '-pthread', '-I'+str(header.parent), str(HERE/(name+'.c')), '-o', str(exe)]
        commands.append(command)
        build = subprocess.run(command, capture_output=True, text=True, env=env, timeout=30)
        (out/(name+'-build.log')).write_text(build.stdout+build.stderr, encoding='utf-8')
        if build.returncode: raise SystemExit(build.stdout+build.stderr)
        test = subprocess.run([str(exe)], capture_output=True, text=True, env=env, timeout=15)
        (out/(name+'-test.log')).write_text(test.stdout+test.stderr, encoding='utf-8')
        results.append({'name':name,'returncode':test.returncode,'output':test.stdout+test.stderr})
    files = (header, header.parent/'tx_lifecycle.h', HERE/'test_frame_reader.c',
             HERE/'test_frame_tx_lifecycle.c', HERE/'frame_tests.py')
    passed = all(t['returncode']==0 for t in results)
    report = {'generated_utc':datetime.now(timezone.utc).isoformat(),
              'passed':passed, 'build_commands':commands,
              'test_exit_code':0 if passed else 1, 'tests':results,
              'hardware_used':False, 'production_helper_used':True,
              'sha256':{str(f):hashlib.sha256(f.read_bytes()).hexdigest() for f in files}}
    (out/'FRAME_TEST_RESULT.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    for t in results: print(t['output'],end='')
    print(out/'FRAME_TEST_RESULT.json')
    raise SystemExit(0 if passed else 1)

if __name__=='__main__': main()
