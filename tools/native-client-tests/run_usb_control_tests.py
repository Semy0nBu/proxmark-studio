"""Compile actual EP0/status/bulk firmware bodies with instrumented register/FIFO MMIO."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

HERE = Path(__file__).resolve().parent
SIGNATURES = (
    'static const char *getStringDescriptor(uint8_t idx)',
    'static void usb_reset_rx_state(void)',
    'static void usb_reset_endpoint(uint32_t mask)',
    'static bool usb_control_interrupted(AT91PS_UDP pudp)',
    'static void usb_recover_endpoint(AT91PS_UDP pudp, uint8_t endpoint)',
    'static bool usb_wait_endpoint(AT91PS_UDP pudp, uint8_t endpoint, uint32_t flags, bool set)',
    'bool usb_check(void)',
    'bool AT91F_USB_SendData(AT91PS_UDP pudp, const char *pData, uint32_t length, uint32_t wLength)',
    'bool AT91F_USB_SendZlp(AT91PS_UDP pudp)',
    'void AT91F_USB_SendStall(AT91PS_UDP pudp)',
    'void AT91F_CDC_Enumerate(void)',
    'int usb_write(const uint8_t *data, const size_t len)',
)

def function(text, signature):
    match = re.search(re.escape(signature) + r'\s*\{', text)
    if not match:
        raise ValueError('Production function not found: '+signature)
    depth = 1
    for index in range(match.end(), len(text)):
        depth += (text[index] == '{') - (text[index] == '}')
        if not depth:
            return text[match.start():index+1]
    raise ValueError('Unterminated production function')

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--client-source', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--compiler', type=Path, default=Path('D:/Tools/ProxSpace-PM3/msys2/ucrt64/bin/g++.exe'))
    args = p.parse_args()
    source = args.client_source.resolve()/'common_arm/usb/usb_cdc_at91.c'
    text = source.read_text(encoding='utf8')
    constants = text[text.index('#define AT91C_EP_CONTROL'):text.index('// !!!! NOTE:')]
    csr_line = text[text.index('// Bitmap for all status bits'):text.index('#ifndef AS_BOOTROM', text.index('static AT91S_CDC_LINE_CODING line'))]
    wait_defines = '\n'.join(line for line in text.splitlines() if line.startswith('#define USB_TRANSFER_'))
    pieces = [constants, csr_line, wait_defines] + [function(text, signature) for signature in SIGNATURES]
    template = HERE/'production_usb_control_test.cpp'
    fixture = template.read_text(encoding='utf8')
    assert fixture.count('/* @USB_CONTROL_PRODUCTION_BODIES@ */') == 1
    output = args.output.resolve();output.mkdir(parents=True, exist_ok=True)
    generated = output/'production_usb_control_test.cpp'
    generated.write_text(fixture.replace('/* @USB_CONTROL_PRODUCTION_BODIES@ */', '\n\n'.join(pieces)), encoding='utf8')
    environment = dict(os.environ, PATH=str(args.compiler.parent)+os.pathsep+os.environ.get('PATH',''))
    results = []
    for optimization in ('-O0','-O2'):
        for bootrom in (False,True):
            label = ('bootrom' if bootrom else 'main')+optimization
            executable = output/(label+'.exe')
            command = [str(args.compiler), '-std=c++17', '-Wall', '-Wextra', '-Werror', optimization,
                       *(['-DAS_BOOTROM'] if bootrom else []), str(generated), '-o', str(executable)]
            build = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=45)
            (output/(label+'-build.log')).write_text(build.stdout+build.stderr, encoding='utf8')
            result = {'label':label,'build_exit_code':build.returncode,'command':command}
            if build.returncode == 0:
                run = subprocess.run([str(executable)], capture_output=True, text=True, env=environment, timeout=45)
                result.update(test_exit_code=run.returncode, output=run.stdout+run.stderr)
                (output/(label+'-test.log')).write_text(run.stdout+run.stderr, encoding='utf8')
            else:
                result['output'] = build.stdout+build.stderr
            results.append(result)
            print(label+': '+result.get('output',''), flush=True)
            if result['build_exit_code'] or result.get('test_exit_code'):
                break
        if results[-1]['build_exit_code'] or results[-1].get('test_exit_code'):
            break
    report = {'passed':len(results)==4 and all(r['build_exit_code']==r.get('test_exit_code')==0 for r in results),
              'generated_utc':datetime.now(timezone.utc).isoformat(),'device_io':False,'firmware_flashed':False,
              'production_functions':list(SIGNATURES),'critical_send_zlp_stubbed':False,
              'limits':'Instrumented MMIO/FIFO callbacks do not model silicon timing or prove live hotplug.',
              'sha256':{str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in (source,Path(__file__),template,generated)},
              'results':results}
    (output/'USB_CONTROL_TEST_RESULT.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    raise SystemExit(0 if report['passed'] else 1)

if __name__ == '__main__':
    main()
