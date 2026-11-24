"""Start the web server and, when configured, supervise the balance worker."""
import logging
import os
import signal
import subprocess
import sys
import time

log = logging.getLogger(__name__)
WEB_COMMAND = ['gunicorn', '--bind', '0.0.0.0:5000', '--workers', '1', '--threads', '4', 'app:app']


def main():
    if os.getenv('BALANCE_WORKER_IN_WEB') != '1':
        os.execvp(WEB_COMMAND[0], WEB_COMMAND)

    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    processes = []
    try:
        processes.append(subprocess.Popen(WEB_COMMAND))
        processes.append(subprocess.Popen([sys.executable, 'balance_worker.py']))
        while not stopping:
            for process in processes:
                status = process.poll()
                if status is not None:
                    log.error('A service process exited unexpectedly with status %s', status)
                    return 1
            time.sleep(2)
        return 0
    finally:
        for process in processes:
            if process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
        for process in processes:
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
