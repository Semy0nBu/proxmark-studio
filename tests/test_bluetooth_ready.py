"""Cached endpoint contract and Windows ABI; no Bluetooth/COM/socket I/O."""
import ctypes as c
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pm3gui import bluetooth_ready as bt


ADDRESS = 0x112233445566


def port(address=ADDRESS, device='COM5', hwid=None):
    return SimpleNamespace(device=device, hwid=hwid or
        'BTHENUM\\{'+bt._SPP_UUID+'}_LOCALMFG&0002\\7&ABC&0&'+f'{address:012X}'+'_C00000000')


def result(**changes):
    value=dict(proto=2,port='COM5',status='resolved',queries=0,elapsed_ms=100,last_error=None,channel=1,endpoint='bt:112233445566')
    value.update(changes);return value


class Clock:
    def __init__(self):self.value=0
    def now(self):return self.value
    def sleep(self,seconds):self.value+=seconds


class Backend:
    def __init__(self):
        self.closed=False;self.peer=(0,True,True);self.inventory=[port()]
        self.peer_calls=0
    def ports(self):return self.inventory
    def peer_info(self,address):
        assert address==ADDRESS;self.peer_calls+=1;return self.peer
    def close(self):self.closed=True


class ReadinessTests(unittest.TestCase):
    def run_probe(self,backend,budget=7000,clock=None):
        clock=clock or Clock()
        return bt._probe('COM5',budget,backend,clock=clock.now)

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
        mutations=[dict(proto=True),dict(proto=1),dict(port='COM6'),dict(queries=True),dict(queries=1),
                   dict(elapsed_ms=False),dict(elapsed_ms=-1),dict(elapsed_ms=1.5),dict(channel=True),
                   dict(channel=0),dict(channel=2),dict(last_error=0),dict(last_error=True),
                   dict(last_error=121),dict(status='ready'),dict(extra='value'),dict(status='unavailable'),
                   dict(endpoint=None),dict(endpoint='bt:000000000000'),dict(endpoint='bt:FFFFFFFFFFFF'),
                   dict(endpoint='bt:11223344556g'),dict(endpoint='bt:11:22:33:44:55:66'),
                   dict(endpoint='bt:112233445566\n'),dict(endpoint='other:112233445566'),
                   dict(endpoint='bt:aabbccddeeff'),dict(endpoint='BT:112233445566'),
                   dict(endpoint='bt:112233445566;quit')]
        for change in mutations:
            with self.subTest(change=change),self.assertRaises(ValueError):
                bt.parse_probe_result(json.dumps(result(**change)).encode(),'COM5')
        for raw in (b'',b'x'*8193,b'\xff',b'{}',b'[]',b'null',raw[:-1],raw+b' {}',
                    raw.replace(b'"proto": 2',b'"proto": 2,"proto": 2'),
                    raw.replace(b'100',b'NaN')):
            with self.subTest(raw=raw[:40]),self.assertRaises(ValueError):bt.parse_probe_result(raw,'COM5')
        for state in ('unavailable','error','not_bluetooth'):
            value=result(status=state,channel=None,queries=0,endpoint=None)
            self.assertEqual(bt.parse_probe_result(json.dumps(value).encode(),'COM5'),value)

    def test_exact_spp_mapping_rejects_usb_incoming_duplicate_and_ambiguous(self):
        self.assertEqual(bt._resolve_port('COM5',[port()]),('candidate',ADDRESS,None))
        for inventory,status in (([],'unavailable'),([port(),port()],'error'),
                                 ([port(hwid='USB\\VID_9AC4')],'not_bluetooth'),
                                 ([port(0)],'not_bluetooth'),([port(0xffffffffffff)],'not_bluetooth'),
                                 ([port(hwid='BTHENUM\\{'+bt._SPP_UUID+'} NO_ADDRESS')],'error'),
                                 ([port(hwid=port().hwid+' '+port().hwid)],'error')):
            with self.subTest(status=status):self.assertEqual(bt._resolve_port('COM5',inventory)[0],status)

    def test_cached_resolution_rechecks_selected_com_mapping_without_discovery(self):
        backend=Backend();value=self.run_probe(backend)
        self.assertEqual(value['status'],'resolved');self.assertEqual(value['queries'],0)
        self.assertEqual(value['endpoint'],'bt:112233445566');self.assertEqual(value['channel'],1)
        self.assertEqual(backend.peer_calls,1)
        backend=Backend();backend.ports=Mock(side_effect=[[port()],[port(ADDRESS+1)]])
        value=self.run_probe(backend)
        self.assertEqual(value['status'],'error');self.assertIsNone(value['endpoint'])

    def test_cached_metadata_budget_expiration_cannot_claim_resolution(self):
        clock=Clock();backend=Backend()
        def slow(address):clock.value+=1;return 0,True,True
        backend.peer_info=slow
        value=self.run_probe(backend,1,clock)
        self.assertEqual(value['status'],'unavailable');self.assertIsNone(value['endpoint'])
        self.assertEqual(value['queries'],0)

    def test_pairing_is_read_only_and_must_match_authenticated_cached_peer(self):
        for peer in ((1168,False,False),(0,True,False),(0,False,True)):
            backend=Backend();backend.peer=peer
            self.assertEqual(self.run_probe(backend)['status'],'unavailable')
            self.assertEqual(backend.peer_calls,1)
        backend=Backend();backend.inventory=[port(hwid='USB\\VID_9AC4')]
        backend.peer_info=Mock(side_effect=AssertionError('No Bluetooth access for USB'))
        self.assertEqual(self.run_probe(backend)['status'],'not_bluetooth')
        backend.peer_info.assert_not_called()

    def test_windowed_worker_writes_atomic_new_json_without_stdout_or_profile(self):
        with tempfile.TemporaryDirectory(prefix='bt-ready-') as folder:
            output=Path(folder)/'result.json';backend=Backend()
            with patch.object(bt.sys,'platform','win32'),patch.object(bt,'_WindowsBackend',return_value=backend),patch.object(bt.sys,'stdout',None):
                self.assertEqual(bt.probe_main(['--port','COM5','--result',str(output)]),0)
            self.assertEqual(bt.parse_probe_result(output.read_bytes(),'COM5')['status'],'resolved')
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
    def test_cached_device_layout_matches_reviewed_mingw_x64_header(self):
        self.assertEqual(c.sizeof(bt._SystemTime),16)
        self.assertEqual(c.sizeof(bt._DeviceInfo),560)
        self.assertEqual(bt._DeviceInfo.address.offset,8)
        self.assertEqual(bt._DeviceInfo.name.offset,64)

    def test_backend_loads_cached_api_only_and_never_winsock_or_radio(self):
        dll=SimpleNamespace(BluetoothGetDeviceInfo=Mock(return_value=0))
        with patch.object(bt.sys,'platform','win32'),patch.object(bt.c,'WinDLL',return_value=dll,create=True) as loader:
            backend=bt._WindowsBackend()
        loader.assert_called_once_with('bthprops.cpl',use_last_error=True)
        def metadata(_radio,info):
            self.assertIsNone(_radio);self.assertEqual(info._obj.size,560)
            self.assertEqual(info._obj.address,ADDRESS)
            info._obj.remembered=1;info._obj.authenticated=1;return 0
        dll.BluetoothGetDeviceInfo.side_effect=metadata
        self.assertEqual(backend.peer_info(ADDRESS),(0,True,True))
        backend.close()
        self.assertFalse(hasattr(backend,'query'));self.assertFalse(hasattr(backend,'ws'))

    def test_resolved_endpoint_is_private_file_only_even_with_console_stdout(self):
        with tempfile.TemporaryDirectory(prefix='bt-endpoint-') as folder:
            path=Path(folder)/'private.json';capture=io.StringIO()
            with patch.object(bt.sys,'platform','win32'),patch.object(bt,'_WindowsBackend',return_value=Backend()),patch.object(bt.sys,'stdout',capture):
                self.assertEqual(bt.probe_main(['--port','COM5','--result',str(path)]),0)
            self.assertEqual(capture.getvalue(),'')
            self.assertEqual(bt.parse_probe_result(path.read_bytes(),'COM5')['endpoint'],'bt:112233445566')
            capture=io.StringIO()
            with patch.object(bt.sys,'platform','win32'),patch.object(bt,'_WindowsBackend',return_value=Backend()),patch.object(bt.sys,'stdout',capture):
                self.assertEqual(bt.probe_main(['--port','COM5']),1)
            self.assertEqual(json.loads(capture.getvalue())['status'],'error')
            self.assertNotIn('112233445566',capture.getvalue())
            self.assertNotIn('bt:',capture.getvalue())


if __name__=='__main__':unittest.main()
