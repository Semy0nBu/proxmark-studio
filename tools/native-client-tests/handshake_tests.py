"""Compile and exercise the production startup PING validator."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client-source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    native = args.client_source.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    compiler = Path(r'D:/Tools/ProxSpace-PM3/msys2/ucrt64/bin/gcc.exe')
    environment = os.environ.copy()
    environment['PATH'] = str(compiler.parent) + os.pathsep + environment.get('PATH', '')
    source = Path(__file__).with_name('test_handshake.c')
    results = []
    for optimization in ('-O0', '-O2'):
        executable = output / ('handshake' + optimization[1:] + '.exe')
        command = [str(compiler), '-std=c11', '-Wall', '-Wextra', '-Werror', optimization,
                   '-I' + str(native / 'include'), '-I' + str(native / 'client/src'),
                   str(source), '-o', str(executable)]
        build = subprocess.run(command, env=environment, text=True, capture_output=True, timeout=30)
        run = subprocess.run([str(executable)], env=environment, text=True, capture_output=True,
                             timeout=15) if build.returncode == 0 else None
        result = {'optimization': optimization, 'build_command': command,
                  'build_exit_code': build.returncode, 'build_output': build.stdout + build.stderr,
                  'test_exit_code': run.returncode if run else None,
                  'test_output': run.stdout + run.stderr if run else ''}
        results.append(result)
    comms = (native / 'client/src/comms.c').read_text(encoding='utf-8')
    bound = 'pm3_handshake_ping_matches(&resp, data)' in comms and \
        'pcrypto_rng_fill_oneshot(data, len, "pm3-connection-ping")' in comms
    passed = bound and all(item['build_exit_code'] == 0 and item['test_exit_code'] == 0
                           for item in results)
    header = native / 'client/src/comms_handshake.h'
    report = {'passed': passed, 'hardware_used': False, 'tests': results,
              'production_binding': bound, 'header_sha256': hashlib.sha256(header.read_bytes()).hexdigest()}
    (output / 'HANDSHAKE_TEST_RESULT.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if passed else 1)


if __name__ == '__main__':
    main()
