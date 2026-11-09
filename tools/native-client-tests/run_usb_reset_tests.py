"""Run production USB RX/reset code with host FIFO/register fixtures, without hardware."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess


HERE = Path(__file__).resolve().parent
MARKER = '/* @USB_RESET_PRODUCTION_BODIES@ */'
SIGNATURES = (
    'static void usb_reset_rx_state(void)',
    'static void usb_reset_endpoint(uint32_t mask)',
    'void usb_disable(void)',
    'bool usb_check(void)',
    'bool usb_poll_validate_length(void)',
)


def function(text, signature):
    """Keep the selected firmware's function body, including conditional code."""
    start = text.index(signature)
    brace = text.index('{', start)
    depth = 0
    for position in range(brace, len(text)):
        if text[position] == '{':
            depth += 1
        elif text[position] == '}':
            depth -= 1
            if depth == 0:
                return text[start:position+1]
    raise ValueError('Unterminated production function: '+signature)


def control_case(text, name, following, signature):
    body = text.split('case '+name+':', 1)[1].split('case '+following+':', 1)[0]
    body = body.rsplit('break;', 1)[0]
    return signature+' {\n'+body+'\n}'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client-source', type=Path, required=True,
                        help='Complete patched Proxmark source tree, including common_arm/usb')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--compiler', type=Path, default=Path(
        r'D:\Tools\ProxSpace-PM3\msys2\ucrt64\bin\gcc.exe'))
    args = parser.parse_args()
    source = args.client_source.resolve()/'common_arm/usb'
    native_files = tuple(source/name for name in ('usb_cdc_at91.c', 'usb_read_ng.c', 'usb_read_ng.h'))
    template = HERE/'production_usb_reset_test.c'
    for path in (*native_files, template):
        if not path.is_file():
            parser.error('Actual production/test source was not found: '+str(path))
    text = native_files[0].read_text(encoding='utf-8')
    try:
        pieces = [function(text, signature) for signature in SIGNATURES]
        pieces += [control_case(text, 'STD_SET_CONFIGURATION', 'STD_GET_CONFIGURATION',
                                'static void configure_endpoint_fixture(uint32_t wValue)'),
                   control_case(text, 'STD_CLEAR_FEATURE_ENDPOINT', 'SET_LINE_CODING',
                                'static void clear_endpoint_fixture(uint32_t wValue,uint32_t wIndex)')]
    except (ValueError, IndexError) as exc:
        parser.error('Select the source tree containing the USB reset candidate: '+str(exc))
    fixture = template.read_text(encoding='utf-8')
    if fixture.count(MARKER) != 1:
        parser.error('The production-body insertion marker must occur exactly once')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    generated = output/'production_usb_reset_test.c'
    generated.write_text(fixture.replace(MARKER, '\n\n'.join(pieces)), encoding='utf-8')
    environment = os.environ.copy()
    environment['PATH'] = str(args.compiler.parent)+os.pathsep+environment.get('PATH', '')
    results = []
    for name, optimization in (('usb-reset-debug', '-O0'), ('usb-reset-release', '-O2')):
        executable = output/(name+'.exe')
        command = [str(args.compiler), '-std=c99', '-pedantic-errors', '-Wall', '-Wextra', '-Werror',
                   optimization, '-I'+str(source), str(generated), str(native_files[1]), '-o', str(executable)]
        build = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=30)
        (output/(name+'-build.log')).write_text(build.stdout+build.stderr, encoding='utf-8')
        result = {'name': name, 'optimization': optimization,
                  'build_command': command, 'build_exit_code': build.returncode}
        if build.returncode == 0:
            run = subprocess.run([str(executable)], capture_output=True, text=True,
                                 env=environment, timeout=15)
            (output/(name+'-test.log')).write_text(run.stdout+run.stderr, encoding='utf-8')
            result.update(test_exit_code=run.returncode, output=run.stdout+run.stderr)
        else:
            result.update(output=build.stdout+build.stderr)
        results.append(result)
        if build.returncode:
            break
    passed = len(results) == 2 and all(
        result['build_exit_code'] == 0 and result.get('test_exit_code') == 0 for result in results)
    files = (*native_files, Path(__file__), template, generated)
    report = {
        'generated_utc': datetime.now(timezone.utc).isoformat(), 'passed': passed,
        'hardware_used': False, 'firmware_flashed': False, 'client_source': str(args.client_source.resolve()),
        'production_functions_extracted': list(SIGNATURES),
        'production_control_cases_extracted': ['STD_SET_CONFIGURATION', 'STD_CLEAR_FEATURE_ENDPOINT'],
        'production_module_compiled': str(native_files[1]), 'tests': results,
        'scope': ['cached command bytes discarded on reset/disconnect',
                  'reset generation rejects mixed-session partial reads',
                  'OUT bank and cache reset together; IN reset preserves RX',
                  'exact firmware bus-reset/configuration/clear-halt/detach paths',
                  'unchanged successful reads across 64-byte packet boundaries'],
        'limits': 'FIFO callbacks and register fixtures do not emulate silicon bank timing or prove live hot-replug.',
        'sha256': {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files},
    }
    destination = output/'USB_RESET_TEST_RESULT.json'
    destination.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    for result in results:
        print(result['name']+': '+result['output'], end='')
    print(destination)
    raise SystemExit(0 if passed else 1)


if __name__ == '__main__':
    main()
