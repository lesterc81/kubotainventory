"""Desktop launcher for the IT Asset System.

Starts the Flask server bound to the machine's IP, opens the system in a native
pywebview window, enforces a single running instance, and shuts down cleanly.

Run from source:   python desktop_launcher.py
Run as frozen exe: AssetSystem.exe
"""
import ctypes
import json
import logging
import os
import socket
import sys
import threading
import time

APP_TITLE = "IT Asset System"


def _lan_ip():
    """Best-effort LAN address of this machine (used to show peers the URL)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _app_base_dir():
    """Directory treated as the app home (beside the exe, else beside this script)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _acquire_single_instance_lock():
    """Windows named mutex. Returns an open handle or None if another instance runs."""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, "AssetSystem_IT_SingleInstance")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


def _ensure_config(base_dir):
    """Load config.json beside the app; guarantee the env vars app.py requires."""
    cfg_path = os.path.join(base_dir, "config.json")
    cfg = {}
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as fh:
                cfg = json.load(fh)
        except Exception:
            cfg = {}

    secret = cfg.get("secret_key") or os.environ.get("SECRET_KEY") or os.urandom(32).hex()
    cfg.setdefault("secret_key", secret)
    cfg.setdefault("mongo_uri", "mongodb://127.0.0.1:27017/itsystem")
    cfg.setdefault("port", 5000)

    # Persist a freshly generated secret once so sessions survive restarts.
    try:
        if not os.path.exists(cfg_path):
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2)
    except Exception:
        pass

    os.environ["SECRET_KEY"] = cfg["secret_key"]
    os.environ.setdefault("MONGO_URI", cfg["mongo_uri"])
    os.environ.setdefault("FLASK_ENV", "production")
    os.environ.setdefault("APP_BASE_URL", "")
    os.environ.setdefault("PORT", str(cfg["port"]))
    return cfg


def _wait_for_server(port, tries=50):
    for _ in range(tries):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def main():
    lock = _acquire_single_instance_lock()
    if lock is None:
        return 0

    base_dir = _app_base_dir()
    _ensure_config(base_dir)

    log_dir = os.path.join(base_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    logging.basicConfig(
        filename=os.path.join(log_dir, "desktop.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    import app as appmod
    flask_app = appmod.create_app()

    if getattr(sys, "frozen", False):
        bundle = sys._MEIPASS
        flask_app.template_folder = os.path.join(bundle, "templates")
        flask_app.static_folder = os.path.join(bundle, "static")

    host = "0.0.0.0"
    port = int(os.environ["PORT"])

    threading.Thread(
        target=flask_app.run,
        kwargs={"host": host, "port": port, "debug": False, "use_reloader": False},
        daemon=True,
    ).start()

    if not _wait_for_server(port):
        logging.error("Server did not start on port %s", port)
        return 1

    lan = _lan_ip()
    local_url = f"http://127.0.0.1:{port}"
    logging.info("App: %s (LAN http://%s:%s)", local_url, lan, port)

    # Headless/service mode: no native window, keep serving until process ends.
    headless = os.environ.get("ASSETSYS_HEADLESS") == "1" or "--server" in sys.argv
    if headless:
        print(f"IT Asset System serving on 127.0.0.1:{port} / http://{lan}:{port}")
        threading.Event().wait()
        return 0

    import webview

    window = webview.create_window(APP_TITLE, local_url, width=1360, height=860,
                                   min_size=(960, 640))
    window.events.closed += lambda: _shutdown(flask_app, appmod)
    webview.start(debug=False)
    return 0


def _shutdown(flask_app, appmod):
    """Best-effort graceful stop: close Mongo client and stop the scheduler."""
    try:
        appmod.mongo.cx.close()
    except Exception:
        pass
    try:
        from ai.scheduler import _scheduler
        if _scheduler:
            _scheduler.shutdown(wait=False)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        import traceback
        try:
            logging.getLogger(__name__).error("Launcher crashed:\n%s",
                                               traceback.format_exc())
        except Exception:
            pass
        raise