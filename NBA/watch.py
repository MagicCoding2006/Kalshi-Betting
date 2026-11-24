"""Supervised local updates; availability runs independently of scans and ingestion."""
import fcntl
import json
import os
import plistlib
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from .data import DATA, atomic_json, instant, load_dataset, refresh, utcnow

LABEL = 'com.signaldesk.nba.watch'


def scan_once(args):
    from .markets import scan
    from .model import load_model
    from .live_board import load_live_games,merge_schedule
    now=utcnow();dataset=merge_schedule(load_dataset(args.data_dir),load_live_games(args.data_dir,now),now)
    return scan(dataset,load_model(args.data_dir),args.data_dir,args.quantity,args.slippage,args.min_edge)


class UpdateWorker:
    def __init__(self,args):
        self.args=args;self.directory=Path(args.data_dir);self.stop=threading.Event();self.lock=threading.Lock()
        self.status={'pid':os.getpid(),'started_at':utcnow().isoformat(),'scan_interval_seconds':args.interval,
                     'availability_interval_seconds':30,'last_scan_at':None,'last_availability_at':None,'scan_error':None,'availability_error':None}
        self.last_refresh=0.;self.refresh_thread=None

    def publish(self,**values):
        with self.lock:
            self.status.update(values,heartbeat_at=utcnow().isoformat())
            atomic_json(self.directory/'watcher-status.json',self.status)

    def availability_once(self):
        from .live_context import collect
        try:
            from .live_board import load_live_games,merge_schedule
            now=utcnow();dataset=merge_schedule(load_dataset(self.directory),load_live_games(self.directory,now),now)
            result=collect(dataset,self.directory,min_interval=25)
            self.publish(last_availability_at=result['observed_at'],availability_error='; '.join(result['errors']) or None)
            return result
        except Exception as exc:
            # Transport errors such as http.client.IncompleteRead are not
            # OSErrors. A failed source must never permanently stop the worker.
            self.publish(availability_error=type(exc).__name__+': '+str(exc))
            traceback.print_exc()

    def availability_loop(self):
        while not self.stop.is_set():
            start=time.monotonic();self.availability_once()
            self.stop.wait(max(1,30-(time.monotonic()-start)))

    def refresh_data(self):
        try:
            dataset=load_dataset(self.directory);years=[g['season'] for g in dataset['schedule']]
            refresh(min(years),max(years),self.directory)
            if (self.directory/'context/history.json').exists():
                from .context import prepare
                from .sources import refresh_extra
                dataset=load_dataset(self.directory);refresh_extra(dataset,self.directory);prepare(dataset,self.directory)
            self.publish(last_dataset_refresh_at=utcnow().isoformat(),dataset_refresh_error=None)
        except Exception as exc:
            self.publish(dataset_refresh_error=type(exc).__name__+': '+str(exc));traceback.print_exc()

    def step(self):
        start=time.monotonic()
        try:
            dataset=load_dataset(self.directory)
            stale=(utcnow()-instant(dataset['generated_at'])).total_seconds()>self.args.refresh_hours*3600
            if stale and time.monotonic()-self.last_refresh>300 and not (self.refresh_thread and self.refresh_thread.is_alive()):
                self.last_refresh=time.monotonic();self.refresh_thread=threading.Thread(target=self.refresh_data,daemon=True,name='nba-dataset-refresh');self.refresh_thread.start()
            result=scan_once(self.args)
            (self.directory/'scan-error.json').unlink(missing_ok=True)
            self.publish(last_scan_at=result['generated_at'],scan_error=None,scan_seconds=time.monotonic()-start)
            print(json.dumps({'generated_at':result['generated_at'],'model_id':result['model_id'],'games':len(result['games']),
                              'estimates':sum(g['prediction'] is not None for g in result['games']),
                              'entry_review_games':sum(g.get('entry_safety',{}).get('blocked',False) for g in result['games'])}),flush=True)
            return result
        except Exception as exc:
            error={'time':utcnow().isoformat(),'error':type(exc).__name__+': '+str(exc)}
            atomic_json(self.directory/'scan-error.json',error);self.publish(scan_error=error['error']);traceback.print_exc()

    def run(self):
        self.publish(running=True)
        availability=threading.Thread(target=self.availability_loop,daemon=True,name='nba-availability');availability.start()
        try:
            while not self.stop.is_set():
                start=time.monotonic();self.step();self.stop.wait(max(1,self.args.interval-(time.monotonic()-start)))
        finally:
            self.stop.set();availability.join(timeout=25);self.publish(running=False)


def run_watch(args):
    directory=Path(args.data_dir);directory.mkdir(parents=True,exist_ok=True)
    with (directory/'watcher.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('NBA updater is already running for this data directory')
        (directory/'watcher.pid').write_text(str(os.getpid())+'\n')
        worker=UpdateWorker(args)
        try:worker.run()
        except KeyboardInterrupt:worker.stop.set()


def install_service(directory=DATA,kind='watch'):
    """User-level macOS launch agent: restart on failure and start at login."""
    if sys.platform!='darwin':raise ValueError('This installer currently supports macOS launchd only')
    if kind not in ('watch','record'):raise ValueError('Unknown NBA service')
    root=Path(__file__).resolve().parent.parent;directory=Path(directory).resolve()
    python=root/'NBA/.venv/bin/python'
    if not python.exists():raise ValueError('Prepare the NBA virtual environment first')
    directory.mkdir(parents=True,exist_ok=True)
    logs=Path.home()/'Library/Logs/SignalDesk';logs.mkdir(parents=True,exist_ok=True)
    label=LABEL if kind=='watch' else 'com.signaldesk.nba.record'
    logfile=logs/('nba-updater.log' if kind=='watch' else 'nba-recorder.log')
    arguments=['watch','--interval','30'] if kind=='watch' else ['record','--interval','10','--idle-interval','15']
    # launchd can fail at spawn (EX_CONFIG) when its cwd or inherited log
    # handles point into Desktop. A home cwd plus an explicit module path
    # works without moving the user's checkout, environment or databases.
    config={'Label':label,'ProgramArguments':[str(python),'-u','-m','NBA','--data-dir',str(directory),*arguments],
            'WorkingDirectory':str(Path.home()),'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':15,
            'StandardOutPath':str(logfile),'StandardErrorPath':str(logfile),
            'EnvironmentVariables':{'PYTHONUNBUFFERED':'1','PYTHONPATH':str(root)},'ProcessType':'Background'}
    path=Path.home()/'Library/LaunchAgents'/f'{label}.plist';path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(plistlib.dumps(config));path.chmod(0o600)
    domain='gui/'+str(os.getuid())
    subprocess.run(['launchctl','bootout',domain+'/'+label],capture_output=True)
    result=subprocess.run(['launchctl','bootstrap',domain,str(path)],capture_output=True,text=True)
    if result.returncode:raise ValueError('launchd could not start NBA updates: '+result.stderr.strip())
    return {'installed':True,'label':label,'path':str(path),'log':str(logfile),'runs_at_login':True,'requires_awake_computer':True}
