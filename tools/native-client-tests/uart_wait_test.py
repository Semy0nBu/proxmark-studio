"""Compile and run production Win32 wait helper against deterministic API mocks."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
DEFAULT_CLIENT = Path(r'D:\Tools\Proxmark3-RDV4\v4.23346-gui-blueshark-stable-client\client')

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--client',type=Path,default=DEFAULT_CLIENT)
    p.add_argument('--compiler',type=Path,default=Path(r'D:\Tools\ProxSpace-PM3\msys2\ucrt64\bin\gcc.exe'))
    p.add_argument('--out',type=Path,required=True)
    args=p.parse_args()
    args.out.mkdir(parents=True,exist_ok=True)
    source=HERE/'test_uart_wait.c'
    header=args.client/'src/uart/uart_win32_wait.h'
    report={'generated_utc':datetime.now(timezone.utc).isoformat(),
            'scope':'Actual production Win32 wait helper with mocked completion APIs',
            'passed':False,'hardware_opened':False,'source_sha256':{}}
    for path in (source,header,args.client/'src/uart/uart_win32.c'):
        report['source_sha256'][str(path)]=hashlib.sha256(path.read_bytes()).hexdigest()
    env=os.environ.copy()
    env['PATH']=str(args.compiler.parent)+os.pathsep+env['PATH']
    with tempfile.TemporaryDirectory(prefix='pm3-uart-wait-') as tmp:
        exe=Path(tmp)/'test_uart_wait.exe'
        cmd=[str(args.compiler),'-std=c11','-Wall','-Wextra','-Werror','-I'+str(header.parent),str(source),'-o',str(exe)]
        build=subprocess.run(cmd,capture_output=True,text=True,timeout=30,env=env)
        report.update(compiler_command=cmd,build_exit_code=build.returncode,build_output=build.stdout+build.stderr)
        if build.returncode==0:
            run=subprocess.run([str(exe)],capture_output=True,text=True,timeout=10,env=env)
            report.update(test_exit_code=run.returncode,test_output=run.stdout+run.stderr,passed=run.returncode==0)
    (args.out/'UART_WAIT_VALIDATION.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(report.get('test_output',report['build_output']),end='')
    if not report['passed']: raise SystemExit(1)

if __name__=='__main__': main()
