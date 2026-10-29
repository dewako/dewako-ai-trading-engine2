"""Launch the local Dewako dashboard prototype on loopback only.

Development helper. It creates a persistent, per-user Fernet key outside the
repository when no key is configured. Do not use this helper for production.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = pathlib.Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
PORT = int(os.environ.get("DEWAKO_PROTOTYPE_PORT", "8000"))
URL = f"http://{HOST}:{PORT}/ui/"


def ensure_development_key() -> None:
    if os.environ.get("ENCRYPTION_KEY") or os.environ.get("ENCRYPTION_KEYS"):
        return
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise SystemExit(
            "Missing backend dependencies. Install backend requirements first; "
            "the cryptography package is required for local startup."
        ) from exc

    if os.name == "nt":
        base = pathlib.Path(os.environ.get("LOCALAPPDATA", pathlib.Path.home() / "AppData/Local"))
    else:
        base = pathlib.Path(os.environ.get("XDG_CONFIG_HOME", pathlib.Path.home() / ".config"))
    key_dir = base / "Dewako" / "development"
    key_path = key_dir / "prototype-fernet.key"
    key_dir.mkdir(parents=True, exist_ok=True)
    if not key_path.exists():
        try:
            with key_path.open("xb") as stream:
                stream.write(Fernet.generate_key())
            try:
                key_path.chmod(0o600)
            except OSError:
                pass
        except FileExistsError:
            pass
    key = key_path.read_bytes().strip()
    # Validate key before passing it to the application.
    try:
        Fernet(key)
    except Exception as exc:
        raise SystemExit(f"The local development encryption key is invalid: {key_path}") from exc
    os.environ["ENCRYPTION_KEY"] = key.decode("ascii")
    print(f"Using local development encryption key: {key_path}")


def main() -> int:
    if not (ROOT / "backend" / "dewako" / "api" / "main.py").is_file():
        raise SystemExit("Could not find backend/dewako/api/main.py. Extract the complete project ZIP first.")

    try:
        import uvicorn  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "Uvicorn is not installed. Activate the project's virtual environment and install backend requirements first."
        ) from exc

    ensure_development_key()
    os.environ["PYTHONPATH"] = str(ROOT / "backend") + os.pathsep + os.environ.get("PYTHONPATH", "")
    os.environ["DEWAKO_ENV"] = "development"
    os.environ["DEWAKO_UI_DIR"] = str(ROOT / "frontend" / "prototype")

    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/health", timeout=1):
            print(f"A server is already responding on port {PORT}; opening {URL}")
            webbrowser.open(URL)
            return 0
    except (urllib.error.URLError, TimeoutError, OSError):
        pass

    print(f"Starting local Dewako dashboard: {URL}")
    print("This is a development prototype. Keep this terminal open; press Ctrl+C to stop.")
    print("Live trading remains locked. This launcher binds only to 127.0.0.1.")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "dewako.api.main:app", "--host", HOST, "--port", str(PORT)],
        cwd=ROOT / "backend",
        env=os.environ.copy(),
    )
    try:
        for _ in range(80):
            if proc.poll() is not None:
                return int(proc.returncode or 1)
            try:
                with urllib.request.urlopen(f"http://{HOST}:{PORT}/health", timeout=1) as response:
                    if response.status == 200:
                        webbrowser.open(URL)
                        break
            except (urllib.error.URLError, TimeoutError, OSError):
                time.sleep(0.25)
        else:
            print("The backend did not become ready in time. Check the startup errors above.")
        return proc.wait()
    except KeyboardInterrupt:
        print("\nStopping Dewako prototype server…")
        proc.terminate()
        try:
            return proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            return proc.wait()


if __name__ == "__main__":
    raise SystemExit(main())
