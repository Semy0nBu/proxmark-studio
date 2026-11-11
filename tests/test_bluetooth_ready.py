"""Readiness contract and Windows ABI with mock APIs; no Bluetooth/COM I/O."""
import ctypes as c
import io
import json
import os
from pathlib import Path
import tempfile
import re
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pm3gui import bluetooth_ready as bt


ADDRESS = 0x112233445566


def port(address=ADDRESS, device='COM5', hwid=None):
    return SimpleNamespace(device=device, hwid=hwid or
        'BTHENUM\\{'+bt._SPP_UUID+'}_LOCALMFG&0002\\7&ABC&0&'+f'{address:012X}'+'_C00000000')


def result(**changes):
    value=dict(proto=1,port='COM5',status='ready',queries=1,elapsed_ms=100,last_error=None,channel=1)
    value.update(changes);return value


class Clock:
    def __init__(self):self.value=0
    def now(self):return self.value
    def sleep(self,seconds):self.value+=seconds


class Backend:
    def __init__(self, answers=None):
        self.answers=list(answers or [(1,None)]);self.queries=0;self.closed=False
        self.peer=(0,True,True);self.inventory=[port()]
    def ports(self):return self.inventory
    def peer_info(self,address):
        assert address==ADDRESS;return self.peer
    def query(self,address):
        assert address==ADDRESS;self.queries+=1
        return self.answers[min(self.queries-1,len(self.answers)-1)]
    def close(self):self.closed=True


class ReadinessTests(unittest.TestCase):
    def run_probe(self,backend,budget=7000,clock=None):
        clock=clock or Clock()
        return bt._probe('COM5',budget,backend,clock=clock.now,sleep=clock.sleep)

    def test_source_and_frozen_invocation_preserve_argument_boundaries(self):
        with tempfile.TemporaryDirectory(prefix='bt-ready-') as folder:
            target=Path(folder)/'new result.json'
            with patch.object(bt.sys,'frozen',False,create=True):
                program,args,cwd=bt.preparation_invocation('com5',1,target)
                expected=bt.sys._base_executable if bt.sys.platform=='win32' else bt.sys.executable
                self.assertEqual(program,str(Path(expected).resolve()))
                self.assertEqual(Path(args[0]),Path(cwd)/'run_gui.py')
                self.assertEqual(args[1:],['--bluetooth-service-probe','--port','COM5','--budget-ms','1','--result',str(target)])
            with patch.object(bt.sys,'frozen',True,create=True):
                frozen_program,args,cwd=bt.preparation_invocation('COM5')
                self.assertEqual(args[0],'--bluetooth-service-probe')
                self.assertEqual(Path(cwd),Path(frozen_program).parent)
                self.assertEqual(frozen_program,str(Path(bt.sys.executable).resolve()))
            with patch.object(bt.sys,'frozen',False,create=True),patch.object(bt.sys,'platform','win32'),patch.object(bt.sys,'_base_executable',str(Path(folder)/'missing-python.exe'),create=True):
                with self.assertRaises(ValueError):bt.preparation_invocation('COM5')
            for bad in ('COM0','COM65536','COM5;quit','COM5\n','COM５',None):
                with self.subTest(port=bad),self.assertRaises(ValueError):bt.preparation_invocation(bad)
            for bad in (0,7001,True,'7000'):
                with self.subTest(budget=bad),self.assertRaises(ValueError):bt.preparation_invocation('COM5',bad)
            with self.assertRaises(ValueError):bt.preparation_invocation('COM5',result_path='relative.json')

    def test_strict_machine_result_validates_identity_and_truth_types(self):
        raw=json.dumps(result()).encode()
        self.assertEqual(bt.parse_probe_result(raw,'com5'),result())
        mutations=[dict(proto=True),dict(port='COM6'),dict(queries=True),dict(queries=0),dict(queries=17),
                   dict(elapsed_ms=False),dict(elapsed_ms=-1),dict(elapsed_ms=1.5),dict(channel=True),
                   dict(channel=0),dict(channel=31),dict(last_error=0),dict(last_error=True),
                   dict(last_error=121),dict(status='maybe'),dict(extra='value'),dict(status='unavailable')]
        for change in mutations:
            with self.subTest(change=change),self.assertRaises(ValueError):
                bt.parse_probe_result(json.dumps(result(**change)).encode(),'COM5')
        for raw in (b'',b'x'*8193,b'\xff',b'{}',b'[]',b'null',raw[:-1],raw+b' {}',
                    raw.replace(b'"proto": 1',b'"proto": 1,"proto": 1'),
                    raw.replace(b'100',b'NaN')):
            with self.subTest(raw=raw[:40]),self.assertRaises(ValueError):bt.parse_probe_result(raw,'COM5')
        for state in ('unavailable','error','not_bluetooth'):
            value=result(status=state,channel=None,queries=0)
            self.assertEqual(bt.parse_probe_result(json.dumps(value).encode(),'COM5'),value)

    def test_exact_spp_mapping_rejects_usb_incoming_duplicate_and_ambiguous(self):
        self.assertEqual(bt._resolve_port('COM5',[port()]),('candidate',ADDRESS,None))
        for inventory,status in (([],'unavailable'),([port(),port()],'error'),
                                 ([port(hwid='USB\\VID_9AC4')],'not_bluetooth'),
                                 ([port(0)],'not_bluetooth'),([port(0xffffffffffff)],'not_bluetooth'),
                                 ([port(hwid='BTHENUM\\{'+bt._SPP_UUID+'} NO_ADDRESS')],'error')):
            with self.subTest(status=status):self.assertEqual(bt._resolve_port('COM5',inventory)[0],status)

    def test_fresh_service_query_retries_discovery_then_rechecks_mapping(self):
        backend=Backend([(None,10110),(1,None)])
        value=self.run_probe(backend)
        self.assertEqual(value['status'],'ready');self.assertEqual(value['queries'],2)
        self.assertEqual(value['elapsed_ms'],650);self.assertIsNone(value['last_error'])
        backend=Backend();backend.ports=Mock(side_effect=[[port()],[port(ADDRESS+1)]])
        self.assertEqual(self.run_probe(backend)['status'],'error')

    def test_budget_exhaustion_and_fatal_errors_never_claim_ready(self):
        backend=Backend([(None,10110)])
        value=self.run_probe(backend,700)
        self.assertEqual(value['status'],'unavailable');self.assertEqual(value['queries'],2)
        self.assertEqual(value['elapsed_ms'],700);self.assertIsNone(value['channel'])
        backend=Backend([(None,10022)])
        self.assertEqual(self.run_probe(backend)['status'],'error');self.assertEqual(backend.queries,1)
        clock=Clock();backend=Backend()
        def late(_):clock.value+=1;return 1,None
        backend.query=late
        value=self.run_probe(backend,1,clock)
        self.assertEqual(value['status'],'unavailable');self.assertEqual(value['last_error'],10060)

    def test_pairing_is_read_only_and_must_match_authenticated_cached_peer(self):
        for peer in ((1168,False,False),(0,True,False),(0,False,True)):
            backend=Backend();backend.peer=peer
            self.assertEqual(self.run_probe(backend)['status'],'unavailable')
            self.assertEqual(backend.queries,0)
        backend=Backend();backend.inventory=[port(hwid='USB\\VID_9AC4')]
        backend.peer_info=Mock(side_effect=AssertionError('No Bluetooth access for USB'))
        self.assertEqual(self.run_probe(backend)['status'],'not_bluetooth')
        backend.peer_info.assert_not_called()

    def test_windowed_worker_writes_atomic_new_json_without_stdout_or_profile(self):
        with tempfile.TemporaryDirectory(prefix='bt-ready-') as folder:
            output=Path(folder)/'result.json';backend=Backend()
            with patch.object(bt.sys,'platform','win32'),patch.object(bt,'_WindowsBackend',return_value=backend),patch.object(bt.sys,'stdout',None):
                self.assertEqual(bt.probe_main(['--port','COM5','--result',str(output)]),0)
            self.assertEqual(bt.parse_probe_result(output.read_bytes(),'COM5')['status'],'ready')
            self.assertTrue(backend.closed);self.assertEqual([p.name for p in Path(folder).iterdir()],['result.json'])
            old=output.read_bytes();capture=io.StringIO()
            with patch.object(bt.sys,'platform','win32'),patch.object(bt,'_WindowsBackend',return_value=Backend()),patch.object(bt.sys,'stdout',capture):
                self.assertEqual(bt.probe_main(['--port','COM5','--result',str(output)]),1)
            self.assertEqual(output.read_bytes(),old)
            self.assertNotIn(str(output),capture.getvalue())

    def test_cli_errors_and_unsupported_platform_are_machine_only(self):
        for arguments in ([],['--port','COM5','--bad'],['--port','secret/path'],['--port','COM5','--budget-ms','bad']):
            capture=io.StringIO()
            with patch.object(bt.sys,'stdout',capture):self.assertEqual(bt.probe_main(arguments),1)
            value=json.loads(capture.getvalue());self.assertEqual(value['status'],'error')
            self.assertNotIn('secret',capture.getvalue());self.assertEqual(set(value),bt._FIELDS)
        capture=io.StringIO()
        with patch.object(bt.sys,'platform','linux'),patch.object(bt.sys,'stdout',capture),patch.object(bt,'_WindowsBackend') as factory:
            self.assertEqual(bt.probe_main(['--port','COM5']),1)
            factory.assert_not_called()
        self.assertEqual(bt.parse_probe_result(capture.getvalue().encode(),'COM5')['status'],'not_bluetooth')


class WindowsAbiTests(unittest.TestCase):
    def test_lookup_flags_match_sdk_values_and_never_flush_previous(self):
        expected={'LUP_RETURN_NAME':0x0010,'LUP_RETURN_ADDR':0x0100,'LUP_FLUSHCACHE':0x1000}
        # An optional SDK header permits a local independent cross-check without
        # imposing a Windows SDK/compiler dependency on normal GUI tests.
        header=os.environ.get('PM3_WINSDK_WINSOCK2_HEADER')
        if header:
            source=Path(header).read_text(encoding='utf8')
            for name,value in expected.items():
                found=re.search(r'^#define\s+'+name+r'\s+(0x[0-9a-fA-F]+)\b',source,re.M)
                self.assertIsNotNone(found,name);self.assertEqual(int(found[1],16),value)
            found=re.search(r'^#define\s+LUP_FLUSHPREVIOUS\s+(0x[0-9a-fA-F]+)\b',source,re.M)
            self.assertIsNotNone(found);self.assertEqual(int(found[1],16),0x2000)
        self.assertEqual(bt._LUP_RETURN_NAME,expected['LUP_RETURN_NAME'])
        self.assertEqual(bt._LUP_RETURN_ADDR,expected['LUP_RETURN_ADDR'])
        self.assertEqual(bt._LUP_FLUSHCACHE,expected['LUP_FLUSHCACHE'])
        self.assertEqual(bt._SDP_FLAGS,0x1110);self.assertFalse(bt._SDP_FLAGS&0x2000)

    def test_fixed_width_layout_matches_reviewed_mingw_x64_headers(self):
        self.assertEqual(c.sizeof(bt._SockaddrBth),30)
        self.assertEqual([getattr(bt._SockaddrBth,n).offset for n in ('address','service','port')],[2,10,26])
        self.assertEqual(c.sizeof(bt._DeviceInfo),560)
        if c.sizeof(c.c_void_p)==8:
            self.assertEqual(c.sizeof(bt._QuerySet),120)
            self.assertEqual(c.sizeof(bt._SocketAddress),16)
            self.assertEqual(c.sizeof(bt._CsAddr),40)

    def backend(self):
        ws=SimpleNamespace(**{name:Mock(return_value=0) for name in ('WSAStartup','WSACleanup','WSAGetLastError',
            'WSAAddressToStringW','WSALookupServiceBeginW','WSALookupServiceNextW','WSALookupServiceEnd')})
        dll=SimpleNamespace(BluetoothGetDeviceInfo=Mock(return_value=0))
        with patch.object(bt.sys,'platform','win32'),patch.object(bt.c,'WinDLL',side_effect=[ws,dll],create=True):
            backend=bt._WindowsBackend()
        def begin(query,flags,handle):
            self.assertEqual(flags,0x1110);self.assertEqual(query._obj.namespace,16)
            handle._obj.value=123;return 0
        ws.WSALookupServiceBeginW.side_effect=begin
        ws.WSAAddressToStringW.side_effect=lambda _a,_s,_p,context,_n:setattr(context,'value','(target)') or 0
        return backend,ws,dll

    def fill(self,buffer,returned,*,address=ADDRESS,channel=1,family=32):
        low=c.addressof(buffer);query=bt._QuerySet.from_buffer(buffer)
        array_offset=128 if c.sizeof(c.c_void_p)==8 else 80
        remote_offset=array_offset+c.sizeof(bt._CsAddr)
        entry=bt._CsAddr.from_buffer(buffer,array_offset)
        entry.remote.address=low+remote_offset;entry.remote.length=c.sizeof(bt._SockaddrBth)
        remote=bt._SockaddrBth.from_buffer(buffer,remote_offset)
        remote.family=family;remote.address=address;remote.port=channel
        query.address_count=1;query.addresses=c.cast(low+array_offset,c.POINTER(bt._CsAddr))
        returned._obj.value=remote_offset+c.sizeof(remote)

    def test_sdp_result_rejects_wrong_remote_or_out_of_buffer_pointer(self):
        for changes in ({},{'address':ADDRESS+1},{'channel':31},{'family':2}):
            backend,ws,_=self.backend()
            def answer(_handle,flags,size,buffer):
                self.assertEqual(flags,0x1110);self.fill(buffer,size,**changes);return 0
            ws.WSALookupServiceNextW.side_effect=answer
            value=backend.query(ADDRESS)
            self.assertEqual(value,(1,None) if not changes else (None,10022))
            ws.WSALookupServiceEnd.assert_called_once()
            backend.close();backend.close();ws.WSACleanup.assert_called_once()
        buffer=c.create_string_buffer(8192);q=bt._QuerySet.from_buffer(buffer);q.size=c.sizeof(q)
        q.address_count=1;q.addresses=c.cast(1,c.POINTER(bt._CsAddr))
        self.assertEqual(bt._WindowsBackend._channel(buffer,8192,ADDRESS),(None,10022))

    def test_bounded_wsaefault_resize_and_lookup_cleanup(self):
        backend,ws,_=self.backend();attempts=[]
        def answer(_h,_f,size,buffer):
            attempts.append(size._obj.value)
            if len(attempts)==1:size._obj.value=16384;ws.WSAGetLastError.return_value=10014;return -1
            self.fill(buffer,size);return 0
        ws.WSALookupServiceNextW.side_effect=answer
        self.assertEqual(backend.query(ADDRESS),(1,None));self.assertEqual(attempts,[8192,16384])
        ws.WSALookupServiceEnd.assert_called_once();backend.close()
        backend,ws,_=self.backend()
        def excessive(_h,_f,size,_buffer):size._obj.value=65537;return -1
        ws.WSALookupServiceNextW.side_effect=excessive;ws.WSAGetLastError.return_value=10014
        self.assertEqual(backend.query(ADDRESS),(None,10014));ws.WSALookupServiceEnd.assert_called_once();backend.close()


if __name__=='__main__':unittest.main()
