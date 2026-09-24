"""Best-effort TrafficLight lifecycle reporting for the native Tigerose runtime."""
import os, shutil, subprocess, threading

class ActivityReporter:
    def __init__(self):
        self.active = set(); self.lock = threading.Lock(); self.cli = os.getenv("TRAFFICLIGHT_BIN") or shutil.which("trafficlight")
    def _call(self, *args):
        if not self.cli: return
        try: subprocess.run([self.cli, *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
        except OSError: pass
    def start(self, run_id, name):
        with self.lock:
            self.active.add(run_id)
        self._call("start", "--id", "tigerose:" + run_id, "--name", name)
    def finish(self, run_id, status):
        with self.lock:
            self.active.discard(run_id)
        result = "success" if status == "completed" else "fail"
        self._call("done", "--id", "tigerose:" + run_id, "--result", result)

reporter = ActivityReporter()
