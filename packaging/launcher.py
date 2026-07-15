import faulthandler
import http.cookiejar
import json
import os
import sys
import traceback
import urllib.request
from pathlib import Path


log_dir = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "DaListener" / "Logs"
log_dir.mkdir(parents=True, exist_ok=True)
log = (log_dir / "frozen-startup.log").open("a", encoding="utf-8", buffering=1)
sys.stdout = log
sys.stderr = log
faulthandler.enable(log)
faulthandler.dump_traceback_later(20, repeat=True, file=log)
log.write("DaListener frozen launcher: importing dashboard server\n")

try:
    from dalistener.dashboard.server import main
    log.write("DaListener frozen launcher: import complete\n")
except Exception:
    traceback.print_exc(file=log)
    raise


def stop_running_instance() -> bool:
    from dalistener.dashboard.auth import DashboardLaunchStore
    data_dir = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "DaListener" / "DaListener"
    token = DashboardLaunchStore(data_dir / "dashboard-auth.json").token()
    cookies = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
    ports = [8765]
    try:
        active_port = int(json.loads((data_dir / "dashboard-runtime.json").read_text(encoding="utf-8"))["port"])
        ports = list(dict.fromkeys([active_port, *ports]))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        pass
    for port in ports:
        try:
            opener.open(f"http://127.0.0.1:{port}/auth/exchange?token={token}", timeout=3).read()
            request = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/application/stop", data=b"", method="POST")
            opener.open(request, timeout=3).read()
            return True
        except OSError:
            continue
    return False


if __name__ == "__main__":
    try:
        if "--stop" in sys.argv:
            log.write("DaListener stop request sent\n" if stop_running_instance() else "DaListener is not running on port 8765\n")
            raise SystemExit(0)
        log.write("DaListener frozen launcher: starting dashboard\n")
        main()
    except Exception:
        traceback.print_exc(file=log)
        raise
