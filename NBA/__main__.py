"""python3 -m NBA: refresh → train → scan/watch; API served by Signal Desk."""
import argparse
import json
import sys
import time
from pathlib import Path
from http.client import HTTPException

from .data import DATA, atomic_json, load_dataset, refresh, utcnow


def main():
    parser = argparse.ArgumentParser(description='NBA pregame fair-value research; public data only, no orders')
    parser.add_argument('--data-dir', type=Path, default=DATA)
    commands = parser.add_subparsers(dest='command', required=True)
    refresh_parser = commands.add_parser('refresh', help='Download real team box scores and schedules')
    refresh_parser.add_argument('--start', type=int, default=2017, help='First season END year')
    refresh_parser.add_argument('--end', type=int, default=utcnow().year + int(utcnow().month >= 9))
    commands.add_parser('exit-trial',help='Evaluate 1/3/5-minute bid ranges, partial exits and a game-state baseline; no promotion')
    commands.add_parser('scalp-trial',help='Replay recorded target/stop windows and evaluate an experimental model; no promotion')
    commands.add_parser('train', help='Chronological evaluation and serving refit')
    commands.add_parser('report', help='Print the existing model report')
    commands.add_parser('backtest', help='Current-season probability buckets from frozen historical predictions')
    commands.add_parser('context', help='Download player/referee data and build past-only context')
    commands.add_parser('service-install',help='Install automatic macOS NBA updates with crash restart and login start')
    commands.add_parser('record-service-install',help='Install supervised local NBA score/price recording on macOS')
    optimize_parser=commands.add_parser('optimize',help='Run bounded factor/weight trials on prior validation seasons')
    optimize_parser.add_argument('--no-promote',action='store_true',help='Keep the existing live model after the experiment')
    record_parser=commands.add_parser('record',help='Continuously archive live NBA scores and public Kalshi books locally')
    record_parser.add_argument('--once',action='store_true')
    record_parser.add_argument('--interval',type=int,default=10,help='Active polling seconds (minimum 5)')
    record_parser.add_argument('--idle-interval',type=int,default=60,help='Idle polling seconds')
    export_parser=commands.add_parser('record-export',help='Export local recordings as compressed JSONL')
    export_parser.add_argument('--output',type=Path,required=True)
    export_parser.add_argument('--since',help='Optional UTC ISO timestamp with timezone')
    commands.add_parser('paper-prepare',help='Fit the separate research-only quarter checkpoint models')
    paper_parser=commands.add_parser('paper',help='Run the fixed paper exit comparison on local recordings')
    paper_parser.add_argument('--watch',action='store_true')
    paper_parser.add_argument('--interval',type=int,default=30)
    for name in ('scan', 'watch'):
        command = commands.add_parser(name, help='Compare pregame fair estimates with public Kalshi books')
        command.add_argument('--quantity', type=int, default=10)
        command.add_argument('--slippage', type=float, default=.005, help='Cost buffer in dollars per contract')
        command.add_argument('--min-edge', type=float, default=.03, help='Minimum net probability difference')
        if name == 'watch':
            command.add_argument('--interval', type=int, default=30)
            command.add_argument('--refresh-hours', type=float, default=6)
    args = parser.parse_args()
    try:
        if args.command in ('service-install','record-service-install'):
            from .watch import install_service
            print(json.dumps(install_service(args.data_dir,'watch' if args.command=='service-install' else 'record')))
        elif args.command=='exit-trial':
            from .exit_forecast import run_exit_trial
            print(json.dumps(run_exit_trial(args.data_dir),indent=2))
        elif args.command=='scalp-trial':
            from .scalp_trial import run_trial
            print(json.dumps(run_trial(args.data_dir),indent=2))
        elif args.command=='watch':
            if args.interval<30 or not .5<=args.refresh_hours<=24 or not 1<=args.quantity<=1000 or not 0<=args.slippage<=.25 or not 0<=args.min_edge<=1:
                raise ValueError('Invalid watch interval, refresh period, quantity or cost policy')
            from .watch import run_watch
            run_watch(args)
        elif args.command=='record-export':
            from .recording import export_recordings
            print(json.dumps(export_recordings(args.data_dir,args.output,args.since)))
        elif args.command=='paper-prepare':
            from .paper import prepare_checkpoints
            print(json.dumps(prepare_checkpoints(args.data_dir),indent=2))
        elif args.command=='paper':
            import fcntl
            import pickle
            from .paper import PaperStudy
            if args.interval<10:raise ValueError('Paper interval must be >=10 seconds')
            with (args.data_dir/'paper'/'study.lock').open('a') as lock:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise ValueError('NBA paper study is already running')
                path=args.data_dir/'paper'/'checkpoint-model.pkl'
                bundle=pickle.loads(path.read_bytes()) if path.exists() else None
                study=PaperStudy(args.data_dir,bundle)
                try:
                    while True:
                        report=study.step();print(json.dumps({'updated_at':report['updated_at'],'entries':report['entries'],'status':report['status']}),flush=True)
                        if not args.watch:break
                        time.sleep(args.interval)
                finally:study.close()
        elif args.command == 'record':
            import fcntl
            import os
            from .recording import Collector
            if not 5<=args.interval<=300 or not args.interval<=args.idle_interval<=3600:raise ValueError('Invalid collector intervals')
            args.data_dir.mkdir(parents=True,exist_ok=True)
            with (args.data_dir/'collector.lock').open('a') as lock:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise ValueError('NBA collector is already running for this data directory')
                collector=Collector(args.data_dir,args.interval,args.idle_interval)
                (args.data_dir/'collector.pid').write_text(str(os.getpid())+'\n')
                try:
                    while True:
                        result=collector.cycle()
                        print(json.dumps({k:result[k] for k in ['last_cycle_at','today_states','books_this_cycle','counts','errors']}),flush=True)
                        if args.once:break
                        time.sleep(max(0,result['next_interval_seconds']-result['cycle_seconds']))
                finally:
                    collector.store.close()
                    path=args.data_dir/'collector-status.json'
                    if path.exists():
                        status=json.loads(path.read_text());status['running']=False;atomic_json(path,status)
        elif args.command == 'refresh':
            print(json.dumps(refresh(args.start, args.end, args.data_dir), indent=2))
        elif args.command in ('context','optimize'):
            from .sources import refresh_extra
            from .context import prepare
            from .live_context import collect
            dataset=load_dataset(args.data_dir)
            if args.command=='context' or not (args.data_dir/'context'/'sources.json').exists():
                refresh_extra(dataset,args.data_dir)
            collect(dataset,args.data_dir)
            history=prepare(dataset,args.data_dir)
            if args.command=='optimize':
                from .experiments import optimize
                report=optimize(dataset,history,args.data_dir,promote=not args.no_promote)
                print(json.dumps({'status':report['status'],'trials':report['completed_trials'],
                                  'probability_winner':report['probability_winner'],
                                  'accuracy_winner':report['accuracy_winner'],'test':report['test'],
                                  'promoted_model_id':report.get('promoted_model_id')},indent=2))
            else:print(json.dumps({'context_rows':len(history['rows']),'player_games':history['player_games']}))
        elif args.command == 'train':
            from .model import train
            report = train(load_dataset(args.data_dir), args.data_dir)
            print(json.dumps({'model_id': report['model_id'], 'split': report['split'],
                              'held_out': {k: v for k, v in report['held_out'].items() if k.startswith('vs_') or k == 'model'},
                              'deployment': report['deployment']}, indent=2))
        elif args.command == 'backtest':
            from .backtest import season_backtest
            print(json.dumps(season_backtest(args.data_dir), indent=2))
        elif args.command == 'report':
            from .service import model_report
            print(json.dumps(model_report(args.data_dir), indent=2))
        else:
            from .watch import scan_once
            from .live_context import collect
            if not 1 <= args.quantity <= 1000 or not 0 <= args.slippage <= .25 or not 0 <= args.min_edge <= 1:
                raise ValueError('Invalid quantity, slippage or minimum edge')
            collect(load_dataset(args.data_dir),args.data_dir)
            result=scan_once(args)
            (args.data_dir/'scan-error.json').unlink(missing_ok=True)
            print(json.dumps({'generated_at':result['generated_at'],'model_id':result['model_id'],
                              'games':len(result['games']),'estimates':sum(g['prediction'] is not None for g in result['games'])}),flush=True)
    except KeyboardInterrupt:
        return
    except (OSError,HTTPException, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, 'NBA: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
