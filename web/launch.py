"""One localhost server, verified PID ownership, and a macOS browser launcher."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import threading
import time
from urllib.request import urlopen
import uuid
import webbrowser

ROOT=Path(__file__).resolve().parents[1]
URL='http://127.0.0.1:8000'


def health():
    try:
        with urlopen(URL+'/api/health',timeout=1) as response:
            data=json.load(response)
        return data if data.get('application')=='subtitle-translator-wp3' else None
    except (OSError,ValueError):
        return None


def browser_when_ready(instance):
    for _ in range(100):
        data=health()
        if data and data.get('instance')==instance:
            webbrowser.open(URL)
            return
        time.sleep(.1)


def run(action='start',root=ROOT):
    from .service import safe_path,save
    directory=safe_path(Path(root).resolve(),'jobs');directory.mkdir(mode=0o700,exist_ok=True)
    lock_path=directory/'server.lock';pid_path=directory/'server.pid.json'
    if lock_path.is_symlink() or pid_path.is_symlink():
        print('Unsafe server state path. No process started or stopped.')
        return 1
    with lock_path.open('a') as lock:
        lock_path.chmod(0o600)
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            acquired=True
        except BlockingIOError:
            acquired=False
        if action=='stop':
            if acquired:
                print('Server is already stopped.')
                return 0
            data=health()
            try:
                state=json.loads(pid_path.read_text())
                pid=state['pid']
                if not data or data.get('instance')!=state['instance'] or type(pid) is not int or pid<=1 or data.get('pid')!=pid:
                    raise ValueError
                os.kill(pid,signal.SIGTERM)
            except (OSError,ValueError,KeyError,TypeError):
                print('Could not verify a running Subtitle Translator process. No unverified process was stopped.')
                return 1
            print('Stop requested. An active request may finish before safe shutdown.')
            return 0
        if not acquired:
            for _ in range(50):
                if health():
                    webbrowser.open(URL)
                    print('Opened the existing Subtitle Translator server.')
                    return 0
                time.sleep(.1)
            print('A server is starting or stopping. No duplicate was started; try again shortly.')
            return 1
        # Fail before starting if another application owns this port.
        probe=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
        try:
            probe.bind(('127.0.0.1',8000))
        except OSError:
            print('Port 8000 is occupied by another application. Nothing was started.')
            return 1
        finally:
            probe.close()
        import uvicorn
        from .app import create_app
        instance=uuid.uuid4().hex
        app=create_app(root);app.state.instance=instance
        save(pid_path,dict(pid=os.getpid(),instance=instance))
        threading.Thread(target=browser_when_ready,args=(instance,),daemon=True).start()
        print('Subtitle Translator: '+URL+'\nKeep this Terminal open. Ctrl+C or Stop Subtitle Translator stops the server.')
        try:
            uvicorn.run(app,host='127.0.0.1',port=8000,workers=1,access_log=False,log_level='warning')
        finally:
            if pid_path.exists():pid_path.unlink()
        return 0


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['start','stop'],nargs='?',default='start')
    raise SystemExit(run(parser.parse_args().action))
