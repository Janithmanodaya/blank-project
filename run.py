#!/usr/bin/env python3
"""
Bootstrap runner:
- Creates a local virtual environment in .venv if missing
- Installs requirements
- Runs the FastAPI app with uvicorn

Usage: python run.py
"""
import os
import subprocess
import sys
from pathlib import Path
import venv


ROOT = Path(__file__).parent.resolve()
VENV_DIR = ROOT / ".venv"


def venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def ensure_venv():
    if not VENV_DIR.exists():
        print("Creating virtual environment at .venv ...")
        venv.EnvBuilder(with_pip=True).create(str(VENV_DIR))
    else:
        print("Virtual environment exists.")


def pip_install():
    print("Installing dependencies ...")
    req = ROOT / "requirements.txt"
    if not req.exists():
        print("requirements.txt not found.")
        sys.exit(1)
    cmd = [str(venv_python()), "-m", "pip", "install", "-U", "pip", "wheel", "setuptools"]
    subprocess.check_call(cmd)
    subprocess.check_call([str(venv_python()), "-m", "pip", "install", "-r", str(req)])


def run_server():
    host = os.getenv("HOST", "127.0.0.1")
    port = os.getenv("PORT", "8080")
    print(f"Starting server at http://{host}:{port}/ui ...")
    cmd = [str(venv_python()), "-m", "uvicorn", "app.main:app", "--host", host, "--port", port]
    subprocess.check_call(cmd)


if __name__ == "__main__":
    ensure_venv()
    pip_install()
    run_server()