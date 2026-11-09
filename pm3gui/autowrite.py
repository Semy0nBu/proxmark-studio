"""One-card HF autowrite CLI using the same plan/runner as the GUI.

Without --execute this prepares a backup/review only. Execution requires an
explicit destination UID, supplied in the scenario or with --target-uid.
"""
from .i18n import tr
from dataclasses import replace
from datetime import datetime,timezone
import argparse
import json
from pathlib import Path
import signal
import sqlite3
from uuid import uuid4
from PySide6.QtCore import QCoreApplication,QEventLoop,QTimer
from .core import parse_dump
from .device import ClientRunner,DeviceSettings,discover_client
from .operations import _uid,OPERATION_TIMEOUT_MS
from .writing import HFWritePlan,WriteOptions,source_family
from .write_scenario import load_scenario,save_scenario,validate_options
from .paths import application_root, default_data_dir, is_frozen

ROOT=application_root()


def run_transaction(plan,perform,execute=False,progress=lambda _:None,verified_keys=lambda uid,keys:None):
    if plan.approved or plan.done:
        raise ValueError(tr('Для нового запуска подготовьте новую транзакцию; продолжение старой записи запрещено.'))
    if execute and not plan.expected_uid:
        raise ValueError(tr('Для автозаписи укажите UID назначения (--target-uid).'))
    approved=False
    recorded=0
    try:
        while not plan.done:
            op=plan.next_operation()
            if op is None:
                progress(plan.review())
                if not execute:return False
                if approved:raise ValueError(tr('Повторное подтверждение одной транзакции запрещено.'))
                plan.approve();approved=True
                continue
            progress(tr('Подготовка карты…') if not plan.approved else tr('Запись и проверка: {0} / {1}', f'{len(plan.verified)}', f'{len(plan.entries)}'))
            output,code=perform(op)
            plan.consume(output,code)
            if len(plan.expected_keys)>recorded:
                verified_keys(plan.target.uid,plan.expected_keys[recorded:]);recorded=len(plan.expected_keys)
        return True
    except Exception as exc:
        plan.fail(str(exc))
        raise


def library_keys(path,uid):
    if not path.is_file():return []
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        db.row_factory=sqlite3.Row
        return [dict(row) for row in db.execute(
            'SELECT c.sector,c.key_type AS type,k.key_hex FROM mf_card_keys c JOIN mf_keys k ON k.id=c.key_id '
            'WHERE c.uid=? ORDER BY c.verified_at DESC,c.created_at DESC',(uid,))]
    finally:db.close()


class CommandExecutor:
    def __init__(self,settings,directory,runner=None):
        self.settings=settings;self.directory=directory;self.runner=runner or ClientRunner();self.counter=0;self.cancelled=False

    def cancel(self,*_):
        self.cancelled=True
        if self.runner.busy:self.runner.cancel()

    def __call__(self,operation):
        if self.cancelled:raise ValueError(tr('Автозапись остановлена пользователем.'))
        loop=QEventLoop();chunks=[];results=[]
        def finished(code,status):results.append((code,status));loop.quit()
        on_output=chunks.append
        self.runner.output.connect(on_output);self.runner.finished.connect(finished)
        timeout=90000 if isinstance(operation,str) else min(30000,OPERATION_TIMEOUT_MS.get(operation.kind,30000))
        watchdog=QTimer();watchdog.setSingleShot(True)
        watchdog.timeout.connect(loop.quit)
        tick=QTimer();tick.setInterval(100);tick.timeout.connect(lambda:None)
        try:
            if isinstance(operation,str):self.runner.run(self.settings,operation,timeout_ms=timeout)
            else:self.runner.run_operation(self.settings,operation,timeout_ms=timeout)
            watchdog.start(timeout+15000);tick.start()
            if not results:loop.exec()
            if not results:
                self.runner.cancel();raise ValueError(tr('Клиент не завершил шаг за отведённое время.'))
            self.counter+=1
            text=''.join(chunks)
            (self.directory/f'{self.counter:03d}.log').write_text(text,encoding='utf-8')
            if self.cancelled:raise ValueError(tr('Автозапись остановлена пользователем; отправленные записи могли изменить карту.'))
            return text,results[-1][0]
        finally:
            watchdog.stop();tick.stop()
            self.runner.output.disconnect(on_output);self.runner.finished.disconnect(finished)

    def close(self):self.runner.shutdown()


def parser():
    p=argparse.ArgumentParser(description=tr('Автозапись одной HF-карты. Без --execute выполняется только подготовка.'))
    source=p.add_mutually_exclusive_group(required=True)
    source.add_argument('--dump',type=Path);source.add_argument('--scenario',type=Path)
    p.add_argument('--data-dir',type=Path,default=default_data_dir())
    p.add_argument('--client');p.add_argument('--port');p.add_argument('--transport',choices=('usb','blueshark'))
    p.add_argument('--target-uid');p.add_argument('--family',choices=('auto','mfc','mfu'),default='auto')
    p.add_argument('--first',type=int);p.add_argument('--last',type=int)
    p.add_argument('--key-a');p.add_argument('--key-b');p.add_argument('--mfu-key')
    p.add_argument('--with-keys',action='store_true');p.add_argument('--include-unknown',action='store_true')
    p.add_argument('--execute',action='store_true');p.add_argument('--output-dir',type=Path)
    return p


def main(argv=None):
    args=parser().parse_args(argv)
    executor=None
    try:
        if args.scenario:dump,options,name=load_scenario(args.scenario)
        else:
            dump=parse_dump(args.dump);options=WriteOptions(source_family(dump));name=args.dump.stem
        changes={}
        for option in ('first','last','key_a','key_b','mfu_key','target_uid'):
            value=getattr(args,option)
            if value is not None:changes[option]=value
        if args.family!='auto':changes['family']=args.family
        if args.with_keys:changes['include_keys']=True
        if args.include_unknown:changes['include_unknown']=True
        options=replace(options,**changes);validate_options(options)
        if args.execute and not options.target_uid:raise ValueError(tr('Запись требует --target-uid или UID в сохранённом сценарии.'))
        settings_path=args.data_dir/'settings.json'
        config=json.loads(settings_path.read_text(encoding='utf-8')) if settings_path.is_file() else {}
        if not isinstance(config,dict):raise ValueError(tr('Неверный файл настроек подключения.'))
        transport=args.transport or config.get('transport','usb')
        port=args.port or config.get('usb_port' if transport=='usb' else 'bluetooth_port') or (
            config.get('port','') if config.get('transport','usb')==transport else '')
        client=args.client or config.get('client_path','')
        if is_frozen() and not args.client and (not client or not Path(str(client)).is_file()):
            client=discover_client(ROOT)
        if not client or not port:raise ValueError(tr('Укажите --client и --port либо настройте подключение в GUI.'))
        parent=args.output_dir or args.data_dir/'sessions'
        directory=parent/(datetime.now(timezone.utc).strftime('autowrite-%Y%m%dT%H%M%S')+'-'+uuid4().hex[:8])
        directory.mkdir(parents=True)
        app=QCoreApplication.instance() or QCoreApplication([])
        settings=DeviceSettings(str(client),str(port),str(directory.resolve()),transport,True)
        plan=HFWritePlan(dump,options,directory,lambda uid:library_keys(args.data_dir/'library.sqlite3',uid))
        save_scenario(directory/'scenario.pm3write.json',dump,options,name)
        executor=CommandExecutor(settings,directory)
        previous=signal.signal(signal.SIGINT,executor.cancel)
        last=['']
        def progress(value):
            if value!=last[0]:print(value,flush=True);last[0]=value
        def store_keys(uid,keys):
            from .keys import KeyLibrary
            library=KeyLibrary(args.data_dir/'library.sqlite3')
            try:library.record_verified_keys(uid,keys,source=tr('Proxmark3: автозапись A/B и проверка на карте'))
            finally:library.close()
        try:written=run_transaction(plan,executor,args.execute,progress,store_keys)
        finally:signal.signal(signal.SIGINT,previous)
        print((tr('Запись завершена и проверена.') if written else tr('Подготовка завершена. Запись не выполнялась.'))+tr(' Отчёт: ')+str(directory/'write-report.json'))
        return 0
    except (OSError,ValueError,sqlite3.Error,TypeError) as exc:
        print(tr('Автозапись остановлена: ')+str(exc))
        return 130 if executor and executor.cancelled else 1
    finally:
        if executor:executor.close()


if __name__=='__main__':raise SystemExit(main())
