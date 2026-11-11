"""Compile/run actual Hardnested scalar/SIMD code with no device implementation."""
from pathlib import Path
import argparse,hashlib,json,os,re,subprocess
from datetime import datetime,timezone

HERE=Path(__file__).resolve().parent
STUBS={
 'common.h':'#include <stdint.h>\n#define MIN(a,b) ((a)<(b)?(a):(b))\n#define BSWAP_32(x) __builtin_bswap32(x)\n',
 'proxmark3.h':'',
 'cmdhfmfhard.h':'#include <stdint.h>\nvoid hardnested_print_progress(uint32_t,const char*,float,uint64_t);\nvoid hardnested_print_key_found_progress(uint32_t,const char*);\n',
 'ui.h':'#include "common.h"\n#define INFO 1\n#define ERR 2\n#define WARNING 3\n#define NORMAL 4\n#define _YELLOW_(x) x\nvoid PrintAndLogEx(int,const char*,...);\n',
 'util.h':'int num_CPUs(void);\n',
 'util_posix.h':'#include <stdint.h>\nuint64_t msclock(void);\nvoid test_msleep(uint32_t);\n#define msleep test_msleep\n',
 'fileutils.h':'#include <stdbool.h>\n#define RESOURCES_SUBDIR "resources"\nint searchFile(char**,const char*,const char*,const char*,bool);\n',
 'pm3_cmd.h':'#define PM3_SUCCESS 0\n',
 'test_alloc.h':'#include <stddef.h>\n#include <malloc.h>\nvoid *test_aligned_malloc(size_t,size_t);\nvoid test_aligned_free(void*);\n#define _aligned_malloc test_aligned_malloc\n#define _aligned_free test_aligned_free\n',
}

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--client-source',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
 p.add_argument('--compiler',type=Path,default=Path('D:/Tools/ProxSpace-PM3/msys2/ucrt64/bin/gcc.exe'))
 a=p.parse_args();native=a.client_source.resolve();out=a.output.resolve();out.mkdir(parents=True,exist_ok=True)
 stubs=out/'stubs';stubs.mkdir(exist_ok=True)
 for name,content in STUBS.items():(stubs/name).write_text(content,encoding='utf8')
 source=native/'client/deps/hardnested';env=os.environ.copy();env['PATH']=str(a.compiler.parent)+os.pathsep+env.get('PATH','')
 results=[]
 variants=[('scalar',['-DNOSIMD_BUILD','-mno-mmx','-mno-sse2','-mno-avx','-mno-avx2','-mno-avx512f']),('mmx',['-mmmx','-mno-sse2','-mno-avx','-mno-avx2','-mno-avx512f']),('sse2',['-msse2','-mno-avx','-mno-avx2','-mno-avx512f']),('avx',['-mavx','-mno-avx2','-mno-avx512f']),('avx2',['-mavx2','-mno-avx512f']),('avx512',['-mavx512f'])]
 for label,opt in [('debug','-O0'),('release','-O2')]:
  build_dir=out/label;build_dir.mkdir(exist_ok=True);logs=[];objects=[]
  base=[str(a.compiler),'-std=gnu11','-Wall','-Wextra','-Werror',opt,'-pthread','-ffunction-sections','-fdata-sections','-I'+str(stubs),'-I'+str(source),'-I'+str(native/'common'),'-I'+str(native/'include')]
  def compile(input,output,flags=()):
   command=base+list(flags)+['-c',str(input),'-o',str(output)]
   run=subprocess.run(command,capture_output=True,text=True,env=env,timeout=90)
   logs.append({'command':command,'code':run.returncode,'output':run.stdout+run.stderr})
   if run.returncode:raise RuntimeError(run.stdout+run.stderr)
   objects.append(output)
  try:
   for name,flags in variants:compile(source/'hardnested_bf_core.c',build_dir/(name+'.o'),flags+['-include',str(stubs/'test_alloc.h')])
   compile(source/'hardnested_bruteforce.c',build_dir/'bruteforce.o',['-Dpthread_create=test_pthread_create','-Dpthread_join=test_pthread_join'])
   for name in ('crypto1','crapto1'):compile(native/f'common/crapto1/{name}.c',build_dir/(name+'.o'))
   compile(native/'common/bucketsort.c',build_dir/'bucketsort.o')
   compile(HERE/'test_hardnested_cancel.c',build_dir/'fixture.o')
   exe=build_dir/'hardnested-cancel.exe';command=[str(a.compiler),'-pthread','-Wl,--gc-sections',*[str(o) for o in objects],'-o',str(exe)]
   linked=subprocess.run(command,capture_output=True,text=True,env=env,timeout=60);logs.append({'command':command,'code':linked.returncode,'output':linked.stdout+linked.stderr})
   if linked.returncode:raise RuntimeError(linked.stdout+linked.stderr)
   run=subprocess.run([str(exe)],capture_output=True,text=True,env=env,timeout=90)
   imports=subprocess.run([str(a.compiler.parent/'objdump.exe'),'-p',str(exe)],capture_output=True,text=True,env=env,timeout=15)
   dlls=re.findall(r'DLL Name:\s*(\S+)',imports.stdout);no_device_imports=not any(x.lower() in {'ws2_32.dll','setupapi.dll','winusb.dll','bthprops.cpl'} for x in dlls)
   match=re.search(r'(\d+) scenarios, (\d+) assertions passed',run.stdout)
   row={'name':label,'passed':run.returncode==0 and bool(match) and no_device_imports,'run_code':run.returncode,'output':run.stdout+run.stderr,'scenarios':int(match[1]) if match else 0,'assertions':int(match[2]) if match else 0,'device_imports_absent':no_device_imports,'imports':dlls}
  except Exception as exc:row={'name':label,'passed':False,'error':str(exc)}
  (build_dir/'build-log.json').write_text(json.dumps(logs,indent=2)+'\n',encoding='utf8');(build_dir/'test-log.json').write_text(json.dumps(row,indent=2)+'\n',encoding='utf8');results.append(row);print(json.dumps(row),flush=True)
  if not row['passed']:break
 files=[source/'hardnested_bruteforce.h',source/'hardnested_bruteforce.c',source/'hardnested_bf_core.c',native/'client/src/cmdhfmfhard.c',HERE/'test_hardnested_cancel.c',Path(__file__)]
 report={'passed':len(results)==2 and all(r['passed'] for r in results),'generated_utc':datetime.now(timezone.utc).isoformat(),'hardware_used':False,'actual_native_SIMD_code_executed':True,'tests':results,'scope':['pre-start cancellation','cancellation during partial even-state allocation and complete freeing','actual scalar/SIMD hot-loop cancellation with active/observed worker counters','sticky cancellation and fresh-command reset','worker creation failure joins only created threads','uncancelled no-key and successful candidate','late cancellation after join suppresses candidate publication'],'limits':['Device/USB/Bluetooth not implemented or called by fixture','Synthetic states/nonces, not RF or new secret recovery proof','Full native command status/cleanup also requires build and live controller validation'],'sha256':{str(f.relative_to(native)) if f.is_relative_to(native) else f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in files}}
 (out/'HARDNESTED_CANCEL_TEST_RESULT.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8');return 0 if report['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
