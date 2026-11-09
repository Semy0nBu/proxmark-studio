"""Execute production Win32 serial helpers with deterministic API mocks."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


HERE = Path(__file__).resolve().parent


def adapter_checks(native):
    """Check that tested helpers actually own the current native adapter paths.

    These are source ownership/early-return checks, not simulated driver tests.
    """
    text = native.read_text(encoding='utf-8')
    code = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"',
                  lambda match: ' ' * len(match[0]), text)

    def body(name):
        match = re.search(r'\b' + name + r'\s*\([^)]*\)\s*\{', code)
        if not match:
            return ''
        start, depth = match.end(), 1
        for position in range(start, len(code)):
            depth += (code[position] == '{') - (code[position] == '}')
            if depth == 0:
                return code[start:position]
        return ''

    configure = body('uart_reconfigure_timeouts_polling')
    receive = body('uart_receive')
    guard = re.search(r'if\s*\(\s*uart_reconfigure_timeouts_polling\(sp\)\s*!=\s*PM3_SUCCESS\s*\)'
                      r'\s*\{\s*\*pszRxLen\s*=\s*0;\s*return\s+PM3_EIO\s*;', receive)
    return {'production_includes_tested_header': '#include "uart_win32_io.h"' in text,
            'runtime_configuration_uses_tested_helper': 'uart_win32_configure_timeouts(' in configure,
            'runtime_configuration_never_frees_or_closes': not re.search(r'\b(?:free|uart_close|CloseHandle|PurgeComm)\s*\(', configure),
            'receive_propagates_configuration_failure_before_ct_access': bool(guard) and guard.start() < receive.find('spw->ct'),
            'serial_read_uses_tested_helper': 'uart_win32_read_some(' in body('uart_read_serial'),
            'serial_write_uses_tested_helper': 'uart_win32_write_exact(' in body('uart_send'),
            'baud_query_uses_tested_helper': 'uart_win32_query_speed(' in body('uart_get_speed'),
            'only_startup_purges_receive_queue': len(re.findall(r'\bPurgeComm\s*\(', code)) == 1
                and 'PurgeComm(' in body('uart_open')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client-source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--compiler', type=Path, default=Path(
        r'D:\Tools\ProxSpace-PM3\msys2\ucrt64\bin\gcc.exe'))
    args = parser.parse_args()
    uart = args.client_source.resolve() / 'client/src/uart'
    headers = [uart / name for name in ('uart_win32_io.h', 'frame_reader.h')]
    native = uart / 'uart_win32.c'
    source = HERE / 'test_usb_uart_io.c'
    for path in (*headers, native, source):
        if not path.is_file():
            parser.error(f'Actual production/test source was not found: {path}')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment['PATH'] = str(args.compiler.parent) + os.pathsep + environment.get('PATH', '')
    results = []
    for name, optimization in (('usb-uart-debug', '-O0'), ('usb-uart-release', '-O2')):
        executable = output / (name + '.exe')
        command = [str(args.compiler), '-std=c11', '-Wall', '-Wextra', '-Wswitch-enum', '-Werror',
                   optimization, '-I' + str(uart), str(source), '-o', str(executable)]
        build = subprocess.run(command, capture_output=True, text=True,
                               env=environment, timeout=30)
        (output / (name + '-build.log')).write_text(build.stdout + build.stderr, encoding='utf-8')
        result = {'name': name, 'build_command': command, 'build_exit_code': build.returncode}
        if build.returncode == 0:
            run = subprocess.run([str(executable)], capture_output=True, text=True,
                                 env=environment, timeout=15)
            (output / (name + '-test.log')).write_text(run.stdout + run.stderr, encoding='utf-8')
            result.update(test_exit_code=run.returncode, output=run.stdout + run.stderr)
        else:
            result.update(output=build.stdout + build.stderr)
        results.append(result)
        if build.returncode:
            break
    bindings = adapter_checks(native)
    passed = all(bindings.values()) and len(results) == 2 and all(
        item['build_exit_code'] == 0 and item.get('test_exit_code') == 0 for item in results)
    files = (*headers, native, source, Path(__file__))
    report = {'generated_utc': datetime.now(timezone.utc).isoformat(),
              'passed': passed, 'hardware_used': False, 'win32_apis_mocked': True,
              'production_helpers_executed': [str(path) for path in headers],
              'tests': results,
              'adapter_source_checks': bindings,
              'scope': ['production serial read/write/baud/timeout helpers',
                        'short USB-style reads composed with the production frame reader',
                        'runtime timeout changes without RX purge or deallocation',
                        'failed and short writes without replay'],
              'limits': 'Mocked APIs do not prove real CDC/FPC driver, firmware or cable behavior.',
              'sha256': {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}}
    destination = output / 'USB_UART_TEST_RESULT.json'
    destination.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    for item in results:
        print(item['name'] + ': ' + item['output'], end='')
    print(destination)
    raise SystemExit(0 if passed else 1)


if __name__ == '__main__':
    main()
