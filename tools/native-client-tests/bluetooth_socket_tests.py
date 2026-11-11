"""Run the actual Windows RFCOMM helper with mocked WinSock APIs, no hardware.

The test binaries intentionally do not link ws2_32. Their PE import tables are
checked as well, so an unmocked socket/WSA call cannot silently use the radio.
"""
import argparse
from datetime import datetime,timezone
import hashlib,json,os,re,subprocess
from pathlib import Path

HERE=Path(__file__).resolve().parent

def adapter_checks(path):
 text=path.read_text(encoding='utf8')
 def body(name):
  code=re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"',lambda m:' '*len(m[0]),text)
  match=re.search(r'\b'+name+r'\s*\([^)]*\)\s*\{',code)
  if not match:return ''
  start=match.end();depth=1
  for i in range(start,len(code)):
   depth+=(code[i]=='{')-(code[i]=='}')
   if depth==0:return code[start:i]
  return ''
 opening=body('uart_open');closing=body('uart_close')
 return {
  'production_includes_tested_header':'#include "uart_win32_bluetooth.h"' in text,
  'production_open_calls_tested_helper_once':opening.count('pm3_bth_open(')==1,
  'direct_bt_open_precedes_serial_CreateFile':0<=opening.find('pm3_bth_open(')<opening.find('CreateFileA('),
  'bounded_production_timeout_used':'PM3_BTH_CONNECT_TIMEOUT_MS' in opening,
  'socket_close_releases_socket_and_wsa':bool(re.search(r'if\s*\(\s*spw->hSocket\s*!=\s*INVALID_SOCKET\s*\)\s*\{[^}]*closesocket\s*\(\s*spw->hSocket\s*\)',closing,re.S))
      and bool(re.search(r'if\s*\(\s*spw->hSocket\s*!=\s*INVALID_SOCKET\s*\)\s*\{\s*WSACleanup\s*\(\s*\)',closing,re.S))
      and 0<=closing.find('closesocket(')<closing.find('WSACloseEvent(')<closing.find('WSACleanup('),
  'socket_event_released_before_final_wsa_cleanup':bool(re.search(r'if\s*\(\s*spw->hSocketEvent\s*!=\s*WSA_INVALID_EVENT\s*\)\s*\{\s*WSACloseEvent\s*\(\s*spw->hSocketEvent\s*\)',closing,re.S)),
 }

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--client-source',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
 p.add_argument('--compiler',type=Path,default=Path('D:/Tools/ProxSpace-PM3/msys2/ucrt64/bin/gcc.exe'))
 a=p.parse_args();uart=a.client_source.resolve()/'client/src/uart'
 header=uart/'uart_win32_bluetooth.h';native=uart/'uart_win32.c';source=HERE/'test_bluetooth_socket.c'
 objdump=a.compiler.parent/'objdump.exe'
 for item in (a.compiler,objdump,header,native,source):
  if not item.is_file():p.error('Required actual production/test tool or source missing: '+str(item))
 output=a.output.resolve();output.mkdir(parents=True,exist_ok=True)
 env=os.environ.copy();env['PATH']=str(a.compiler.parent)+os.pathsep+env.get('PATH','')
 results=[]
 for name,optimization in (('bluetooth-socket-debug','-O0'),('bluetooth-socket-release','-O2')):
  executable=output/(name+'.exe')
  command=[str(a.compiler),'-std=c11','-Wall','-Wextra','-Wswitch-enum','-Werror',optimization,'-I'+str(uart),str(source),'-o',str(executable)]
  build=subprocess.run(command,capture_output=True,text=True,env=env,timeout=30)
  (output/(name+'-build.log')).write_text(build.stdout+build.stderr,encoding='utf8')
  record={'name':name,'optimization':optimization,'build_command':command,'build_exit_code':build.returncode}
  if build.returncode:
   record.update(output=build.stdout+build.stderr);results.append(record);break
  run=subprocess.run([str(executable)],capture_output=True,text=True,env=env,timeout=15)
  (output/(name+'-test.log')).write_text(run.stdout+run.stderr,encoding='utf8')
  imports=subprocess.run([str(objdump),'-p',str(executable)],capture_output=True,text=True,env=env,timeout=15)
  (output/(name+'-pe-imports.log')).write_text(imports.stdout+imports.stderr,encoding='utf8')
  dlls=re.findall(r'DLL Name:\s*(\S+)',imports.stdout)
  no_network=imports.returncode==0 and not any(d.lower() in {'ws2_32.dll','wsock32.dll','bthprops.cpl','wininet.dll','winhttp.dll'} for d in dlls)
  match=re.search(r'(\d+) scenarios, (\d+) assertions passed',run.stdout)
  record.update(test_exit_code=run.returncode,output=run.stdout+run.stderr,network_imports_absent=no_network,
                scenarios=int(match[1]) if match else 0,assertions=int(match[2]) if match else 0,import_dlls=dlls)
  results.append(record)
 bindings=adapter_checks(native)
 passed=len(results)==2 and all(bindings.values()) and all(r['build_exit_code']==0 and r.get('test_exit_code')==0 and r.get('network_imports_absent') and r.get('scenarios',0)>=30 for r in results)
 files=(header,native,source,Path(__file__))
 report={'generated_utc':datetime.now(timezone.utc).isoformat(),'passed':passed,'hardware_used':False,'win32_apis_mocked':True,
         'production_helper_executed':str(header),'tests':results,'adapter_source_checks':bindings,
         'scope':['strict compact/colon endpoint parser and invalid/broadcast rejection','channel1 GUID-zero SOCKADDR_BTH construction','immediate and bounded async connect with all three progress errors','SO_ERROR status/exact length and writable/exception flags','timeout and fatal API failures preserve original error','blocking-mode restoration before publishing socket','exact WSA reference/socket cleanup on every failure','successful socket lifetime is handed to caller'],
         'limits':'No real RFCOMM service, paired device, Bluetooth radio or COM driver was exercised. This does not prove radio recovery or transport throughput.',
         'sha256':{str(f):hashlib.sha256(f.read_bytes()).hexdigest() for f in files}}
 destination=output/'BLUETOOTH_SOCKET_TEST_RESULT.json';destination.write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
 for result in results:print(result['name']+': '+result.get('output',''),end='')
 print(json.dumps({'passed':passed,'adapter_source_checks':bindings,'report':str(destination)},indent=2))
 return 0 if passed else 1
if __name__=='__main__':raise SystemExit(main())
