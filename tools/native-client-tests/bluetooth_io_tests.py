"""Execute actual event-driven Windows Bluetooth IO helpers with mocked APIs.
No ws2_32 library is linked; PE imports are checked before reporting success.
"""
import argparse
from datetime import datetime,timezone
import hashlib,json,os,re,subprocess
from pathlib import Path
from bluetooth_socket_tests import adapter_checks as connect_adapter_checks
HERE=Path(__file__).resolve().parent

def io_bindings(native):
 text=native.read_text(encoding='utf8')
 code=re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"',lambda m:' '*len(m[0]),text)
 def body(name):
  m=re.search(r'\b'+name+r'\s*\([^)]*\)\s*\{',code)
  if not m:return ''
  depth=1;start=m.end()
  for i in range(start,len(code)):
   depth+=(code[i]=='{')-(code[i]=='}')
   if depth==0:return code[start:i]
  return ''
 return {'bt_event_initialized_by_tested_helper':'pm3_bth_prepare_io(' in body('uart_open'),
         'bt_read_uses_tested_nonblocking_helper':'pm3_bth_read_some(' in body('uart_receive'),
         'bt_send_uses_tested_absolute_budget_helper':'pm3_bth_send_exact(' in body('uart_send'),
         'native_wake_signals_manual_event':'SetEvent(' in body('uart_wakeup')}

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--client-source',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
 p.add_argument('--compiler',type=Path,default=Path('D:/Tools/ProxSpace-PM3/msys2/ucrt64/bin/gcc.exe'))
 a=p.parse_args();uart=a.client_source.resolve()/'client/src/uart';header=uart/'uart_win32_bluetooth.h';native=uart/'uart_win32.c';source=HERE/'test_bluetooth_io.c';objdump=a.compiler.parent/'objdump.exe'
 for file in (a.compiler,objdump,header,native,source):
  if not file.is_file():p.error('Required actual source/tool missing: '+str(file))
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=True);env=os.environ.copy();env['PATH']=str(a.compiler.parent)+os.pathsep+env.get('PATH','')
 results=[]
 for name,opt in (('bluetooth-io-debug','-O0'),('bluetooth-io-release','-O2')):
  exe=out/(name+'.exe');command=[str(a.compiler),'-std=c11','-Wall','-Wextra','-Wswitch-enum','-Werror',opt,'-I'+str(uart),str(source),'-o',str(exe)]
  build=subprocess.run(command,capture_output=True,text=True,env=env,timeout=30)
  (out/(name+'-build.log')).write_text(build.stdout+build.stderr,encoding='utf8')
  row={'name':name,'optimization':opt,'build_command':command,'build_exit_code':build.returncode}
  if build.returncode:row['output']=build.stdout+build.stderr;results.append(row);break
  run=subprocess.run([str(exe)],capture_output=True,text=True,env=env,timeout=15)
  (out/(name+'-test.log')).write_text(run.stdout+run.stderr,encoding='utf8')
  imports=subprocess.run([str(objdump),'-p',str(exe)],capture_output=True,text=True,env=env,timeout=15)
  (out/(name+'-pe-imports.log')).write_text(imports.stdout+imports.stderr,encoding='utf8')
  dlls=re.findall(r'DLL Name:\s*(\S+)',imports.stdout)
  no_network=imports.returncode==0 and not any(d.lower() in {'ws2_32.dll','wsock32.dll','bthprops.cpl','wininet.dll','winhttp.dll'} for d in dlls)
  match=re.search(r'(\d+) scenarios, (\d+) assertions passed',run.stdout)
  row.update(test_exit_code=run.returncode,output=run.stdout+run.stderr,network_imports_absent=no_network,scenarios=int(match[1]) if match else 0,assertions=int(match[2]) if match else 0,import_dlls=dlls);results.append(row)
 bindings={**connect_adapter_checks(native),**io_bindings(native)}
 passed=len(results)==2 and all(bindings.values()) and all(row['build_exit_code']==0 and row.get('test_exit_code')==0 and row.get('network_imports_absent') and row.get('scenarios',0)>=40 for row in results)
 report={'passed':passed,'generated_utc':datetime.now(timezone.utc).isoformat(),'hardware_used':False,'win32_apis_mocked':True,'production_helper_executed':str(header),'tests':results,'adapter_source_checks':bindings,
         'scope':['event creation/failure cleanup and READ/CLOSE registration','immediate short/split reads without filling or purging','available data with pending wake preserves wake for next empty read','clean close/network errors/wake reset/invalid handles','stale network events consume one original deadline','INT_MAX socket API clamp','partial sends andWOULDBLOCK resume at correct offset without replay','single send deadline and failure/zero-return propagation'],
         'limits':'Mocked WinSock/time/event APIs; no real Bluetooth throughput, radio wakeup or recovery proof.',
         'sha256':{str(file):hashlib.sha256(file.read_bytes()).hexdigest() for file in (header,native,source,Path(__file__),HERE/'bluetooth_socket_tests.py')}}
 target=out/'BLUETOOTH_IO_TEST_RESULT.json';target.write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
 for row in results:print(row['name']+': '+row.get('output',''),end='')
 print(json.dumps({'passed':passed,'adapter_source_checks':bindings,'report':str(target)},indent=2));return 0 if passed else 1
if __name__=='__main__':raise SystemExit(main())
