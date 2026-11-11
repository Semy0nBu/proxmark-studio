"""Exercise actual Windows UART DCB code with mocked handle APIs, never a port."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

HERE = Path(__file__).resolve().parent


def extract_function(text, name):
    match = re.search(r'\bbool\s+' + re.escape(name) + r'\s*\([^)]*\)\s*\{', text)
    if match is None:
        raise ValueError('Production function missing: ' + name)
    depth = 1
    for index in range(match.end(), len(text)):
        depth += (text[index] == '{') - (text[index] == '}')
        if depth == 0:
            return text[match.start():index+1]
    raise ValueError('Unclosed production function: ' + name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client-source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--compiler', type=Path, default=Path(r'D:/Tools/ProxSpace-PM3/msys2/ucrt64/bin/gcc.exe'))
    args = parser.parse_args()
    uart = args.client_source.resolve() / 'client/src/uart'
    native = uart/'uart_win32.c'
    header = uart/'uart_win32_dcb.h'
    source = HERE/'test_windows_dcb.c'
    text = native.read_text(encoding='utf8')
    assert '#include "uart_win32_dcb.h"' in text
    first = text.index('    // Prepare the device control')
    last = text.index('    uart_reconfigure_timeouts(UART_FPC_CLIENT_RX_TIMEOUT_MS);', first)
    init = text[first:last]
    first = text.index('    if (speed != sp->dcb.BaudRate && !uart_set_speed(sp, speed))')
    last = text.index('    // Clear only before', first)
    baud = text[first:last]
    generated = (extract_function(text, 'uart_set_speed') + '\n'
                 'static serial_port production_open_dcb(serial_port_windows_t *sp, const char *acPortName, bool slient) {\n'
                 + init + '\nreturn sp;\n}\n'
                 'static serial_port production_open_baud(serial_port_windows_t *sp, const char *acPortName, uint32_t speed, bool slient) {\n'
                 + baud + '\nreturn sp;\n}\n')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    extracted = output/'production_windows_dcb.inc'
    extracted.write_text(generated, encoding='utf8')
    environment = os.environ.copy()
    environment['PATH'] = str(args.compiler.parent) + os.pathsep + environment.get('PATH', '')
    results = []
    for name, optimization in (('windows-dcb-debug', '-O0'), ('windows-dcb-release', '-O2')):
        executable = output/(name+'.exe')
        command = [str(args.compiler), '-std=c11', '-Wall', '-Wextra', '-Wswitch-enum', '-Werror',
                   optimization, '-I'+str(uart), '-I'+str(output), str(source), '-o', str(executable)]
        build = subprocess.run(command, capture_output=True, text=True, encoding='utf8', env=environment, timeout=30)
        (output/(name+'-build.log')).write_text(build.stdout+build.stderr, encoding='utf8')
        row = {'name': name, 'build_command': command, 'build_exit_code': build.returncode}
        if build.returncode == 0:
            run = subprocess.run([str(executable)], capture_output=True, text=True, encoding='utf8', env=environment, timeout=15)
            (output/(name+'-test.log')).write_text(run.stdout+run.stderr, encoding='utf8')
            row.update(test_exit_code=run.returncode, output=run.stdout+run.stderr)
        else:
            row['output'] = build.stdout+build.stderr
        results.append(row)
        if build.returncode:
            break
    passed = len(results) == 2 and all(r['build_exit_code'] == 0 and r.get('test_exit_code') == 0 for r in results)
    files = (native, header, source, Path(__file__), extracted)
    report = {'passed': passed, 'generated_utc': datetime.now(timezone.utc).isoformat(),
              'hardware_used': False, 'serial_handles_opened': 0, 'win32_handle_apis_mocked': True,
              'actual_windows_parser_probe': True,
              'scope': ['production DCB initialization and transactional baud updates',
                        'actual extracted uart_open init/baud error cleanup and silent diagnostics',
                        'GetCommState/BuildCommDCB/SetCommState failure boundaries and preserved error codes',
                        'one existing115200 baud fallback without duplicate default application'],
              'limits': 'Mocked APIs do not establish behavior of a physical CDC/FPC driver, firmware or cable.',
              'tests': results,
              'sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}
    destination = output/'WINDOWS_DCB_TEST_RESULT.json'
    destination.write_text(json.dumps(report, indent=2)+'\n', encoding='utf8')
    for result in results:
        print(result['name']+': '+result['output'], end='')
    print(destination)
    raise SystemExit(0 if passed else 1)


if __name__ == '__main__':
    main()
