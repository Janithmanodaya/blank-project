import os
import sys
import threading
import queue
import subprocess
import time
import json
import shutil
import zipfile
import urllib.request
from pathlib import Path

import customtkinter as ctk
from tkinter import filedialog, messagebox

from git import Repo, GitCommandError

APP_DIR = Path(__file__).resolve().parent
CLONES_DIR = APP_DIR / "cloned_repos"
CLONES_DIR.mkdir(exist_ok=True)
REPOS_JSON = APP_DIR / "repos.json"
SETTINGS_JSON = APP_DIR / "settings.json"

DEFAULT_REPOS = [
    "https://github.com/psf/requests",
    "https://github.com/pallets/flask",
    "https://github.com/streamlit/streamlit-example",
]

def load_settings():
    if SETTINGS_JSON.exists():
        try:
            with open(SETTINGS_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}

def save_settings(d):
    try:
        with open(SETTINGS_JSON, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass

def load_repos():
    if REPOS_JSON.exists():
        try:
            with open(REPOS_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
    return DEFAULT_REPOS.copy()

def save_repos(repos):
    try:
        with open(REPOS_JSON, "w", encoding="utf-8") as f:
            json.dump(repos, f, indent=2)
    except Exception:
        pass

class ProcessRunner:
    def __init__(self, output_callback):
        self.proc = None
        self.thread = None
        self.output_callback = output_callback
        self.stop_flag = threading.Event()

    def run(self, args, cwd=None, env=None, shell=False):
        if self.proc is not None:
            raise RuntimeError("A process is already running")

        self.stop_flag.clear()
        self.proc = subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            shell=shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            bufsize=1,
            universal_newlines=True,
        )

        def reader():
            try:
                for line in self.proc.stdout:
                    if line:
                        self.output_callback(line.rstrip("\n"))
                    if self.stop_flag.is_set():
                        break
            finally:
                # Drain remaining output if any
                rem = self.proc.stdout.read()
                if rem:
                    self.output_callback(rem)
                self.proc.stdout.close()
                code = self.proc.wait()
                self.output_callback(f"\n[process exited with code {code}]")
                self.proc = None

        self.thread = threading.Thread(target=reader, daemon=True)
        self.thread.start()

    def send_input(self, text):
        if self.proc and self.proc.stdin:
            try:
                self.proc.stdin.write(text + "\n")
                self.proc.stdin.flush()
            except Exception:
                pass

    def terminate(self):
        self.stop_flag.set()
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_default_color_theme("blue")
        self.title("Repo Runner")
        self.geometry("1000x700")

        self.settings = load_settings()
        self.repos = load_repos()
        self.selected_files = []
        self.pass_as_args_var = ctk.BooleanVar(value=True)
        self.export_env_var = ctk.BooleanVar(value=True)

        # Where we place auto-downloaded tools (e.g., PHP)
        self.tools_dir = APP_DIR / "tools"
        self.tools_dir.mkdir(exist_ok=True)

        # Top frame for input controls
        top = ctk.CTkFrame(self)
        top.pack(side="top", fill="x", padx=10, pady=10)

        self.url_entry = ctk.CTkEntry(top, placeholder_text="Paste Git repository URL here")
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))

        self.repo_dropdown = ctk.CTkOptionMenu(
            top,
            values=self.repos,
            command=self.on_dropdown_select
        )
        self.repo_dropdown.set("Select a saved repository")
        self.repo_dropdown.pack(side="left", padx=(0, 10))

        self.run_button = ctk.CTkButton(top, text="Run", command=self.on_run_clicked)
        self.run_button.pack(side="left")

        # Files frame for selecting input files required by the target repo
        files_frame = ctk.CTkFrame(self)
        files_frame.pack(side="top", fill="x", padx=10, pady=(0, 10))

        files_controls = ctk.CTkFrame(files_frame)
        files_controls.pack(side="top", fill="x")

        add_btn = ctk.CTkButton(files_controls, text="Add File(s)", command=self.add_files)
        add_btn.pack(side="left", padx=(0, 10))

        clear_btn = ctk.CTkButton(files_controls, text="Clear List", command=self.clear_files)
        clear_btn.pack(side="left")

        self.pass_args_cb = ctk.CTkCheckBox(files_controls, text="Pass selected files as CLI args", variable=self.pass_as_args_var)
        self.pass_args_cb.pack(side="left", padx=(20, 10))

        self.export_env_cb = ctk.CTkCheckBox(files_controls, text="Expose as APP_SELECTED_FILES env var", variable=self.export_env_var)
        self.export_env_cb.pack(side="left")

        self.files_box = ctk.CTkTextbox(files_frame, height=100, wrap="none", state="disabled")
        self.files_box.pack(side="top", fill="x", expand=False, pady=(5, 0))

        # Output frame
        middle = ctk.CTkFrame(self)
        middle.pack(side="top", fill="both", expand=True, padx=10, pady=(0, 10))

        self.output_box = ctk.CTkTextbox(middle, wrap="word")
        self.output_box.pack(side="top", fill="both", expand=True)

        # Input to send to running process
        input_frame = ctk.CTkFrame(self)
        input_frame.pack(side="bottom", fill="x", padx=10, pady=(0, 10))

        self.input_entry = ctk.CTkEntry(input_frame, placeholder_text="Type input for the running process and press Enter")
        self.input_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.input_entry.bind("<Return>", self.on_send_input)

        self.stop_button = ctk.CTkButton(input_frame, text="Stop Process", command=self.stop_process)
        self.stop_button.pack(side="left")

        # State
        self.runner = ProcessRunner(self.append_output)
        self.current_repo_dir = None

    def refresh_files_box(self):
        self.files_box.configure(state="normal")
        self.files_box.delete("1.0", "end")
        if self.selected_files:
            for p in self.selected_files:
                self.files_box.insert("end", p + "\n")
        else:
            self.files_box.insert("end", "(no files selected)\n")
        self.files_box.configure(state="disabled")

    def add_files(self):
        paths = filedialog.askopenfilenames(title="Select file(s) for the target repository")
        if paths:
            for p in paths:
                if p not in self.selected_files:
                    self.selected_files.append(p)
            self.refresh_files_box()

    def clear_files(self):
        self.selected_files = []
        self.refresh_files_box()

    def on_dropdown_select(self, value):
        if value and value != "Select a saved repository":
            self.url_entry.delete(0, "end")
            self.url_entry.insert(0, value)

    def on_send_input(self, event=None):
        text = self.input_entry.get()
        if text.strip():
            self.append_output(f">> {text}")
            self.runner.send_input(text)
            self.input_entry.delete(0, "end")

    def stop_process(self):
        self.runner.terminate()
        self.append_output("[INFO] Termination requested...")

    def append_output(self, text):
        self.output_box.insert("end", text + "\n")
        self.output_box.see("end")
        self.update_idletasks()

    def on_run_clicked(self):
        url = self.url_entry.get().strip()
        if not url:
            messagebox.showerror("Error", "Please enter or select a repository URL.")
            return

        # Save repo to list if new
        if url not in self.repos:
            self.repos.insert(0, url)
            save_repos(self.repos)
            self.repo_dropdown.configure(values=self.repos)

        self.run_button.configure(state="disabled")
        self.append_output(f"[INFO] Starting workflow for {url} ...")

        t = threading.Thread(target=self.run_repo, args=(url,), daemon=True)
        t.start()

    def run_repo(self, url):
        try:
            repo_dir = self.clone_repo(url)
            self.current_repo_dir = repo_dir
            self.install_requirements(repo_dir)
            self.find_and_run(repo_dir)
        except Exception as e:
            self.append_output(f"[ERROR] {e}")
        finally:
            self.run_button.configure(state="normal")

    def clone_repo(self, url) -> Path:
        repo_name = url.rstrip("/").split("/")[-1]
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]
        target_dir = CLONES_DIR / repo_name

        if target_dir.exists():
            self.append_output(f"[INFO] Repository already cloned at {target_dir}. Pulling latest changes...")
            try:
                repo = Repo(str(target_dir))
                self.append_output(repo.git.pull())
            except GitCommandError as ge:
                self.append_output(f"[WARN] git pull failed: {ge}. Re-cloning repository.")
                # reclone into a new dir with timestamp
                ts = time.strftime("%Y%m%d-%H%M%S")
                target_dir = CLONES_DIR / f"{repo_name}-{ts}"
                Repo.clone_from(url, str(target_dir))
        else:
            self.append_output(f"[INFO] Cloning into {target_dir} ...")
            Repo.clone_from(url, str(target_dir))
        self.append_output("[INFO] Clone complete.")
        return target_dir

    def install_requirements(self, repo_dir: Path):
        req = repo_dir / "requirements.txt"
        if req.exists():
            self.append_output("[INFO] Installing repository requirements...")
            args = [sys.executable, "-m", "pip", "install", "-r", str(req)]
            self.run_and_wait(args, cwd=str(repo_dir))
        else:
            self.append_output("[INFO] No requirements.txt found. Skipping dependency installation.")

    def _run_tool_with_output(self, args, timeout=120):
        """
        Run a command, stream its output to the GUI, and enforce a timeout.
        Returns process return code or None if killed by timeout.
        """
        try:
            self.append_output(f"[TOOL] {' '.join(args)}")
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                creationflags=0
            )
            start = time.time()
            while True:
                line = proc.stdout.readline()
                if line:
                    self.append_output(line.rstrip("\n"))
                if proc.poll() is not None:
                    break
                if time.time() - start > timeout:
                    self.append_output(f"[WARN] Command timed out after {timeout}s, terminating...")
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    return None
            return proc.returncode
        except Exception as e:
            self.append_output(f"[WARN] Failed to run command: {e}")
            return None

    def ensure_php_available(self) -> str | None:
        """
        Ensures a php.exe is available.
        Returns a directory path to prepend to PATH if a bundled PHP is set up, else None.
        Strategy:
          1) Check PATH and bundled locations.
          2) Try system package managers (winget, then choco) with timeouts and streamed logs.
          3) If still missing, guide manual placement.
        """
        # Already available?
        if shutil.which("php"):
            return None

        # Existing bundled location?
        for cand in [APP_DIR / "php", APP_DIR / "bin" / "php", self.tools_dir / "php"]:
            if (cand / "php.exe").exists():
                return str(cand)

        # Check user-configured php.exe first
        php_cfg = (self.settings.get("tool_paths", {}) or {}).get("php")
        if php_cfg and Path(php_cfg).exists():
            php_dir = str(Path(php_cfg).parent)
            self.append_output(f"[INFO] Using PHP from settings: {php_cfg}")
            return php_dir

        # Try winget if available
        if shutil.which("winget"):
            self.append_output("[INFO] php.exe not found. Trying to install via winget (requires Windows Apps Installer)...")
            code = self._run_tool_with_output([
                "winget", "install", "-e", "--id", "PHP.PHP",
                "--accept-package-agreements", "--accept-source-agreements", "--silent"
            ], timeout=180)
            if code == 0 and shutil.which("php"):
                self.append_output("[INFO] PHP installed via winget.")
                return None
            else:
                self.append_output("[WARN] winget did not complete PHP installation.")
        else:
            self.append_output("[INFO] winget not available on this system. Skipping.")

        # Try Scoop (user-mode, no admin)
        scoop = shutil.which("scoop")
        if scoop:
            self.append_output("[INFO] Trying to install PHP via Scoop (no admin required).")
            code = self._run_tool_with_output(["scoop", "install", "php"], timeout=300)
            # Check typical scoop shim location
            userprofile = os.environ.get("USERPROFILE") or str(Path.home())
            scoop_php = Path(userprofile) / "scoop" / "shims" / "php.exe"
            if code == 0 and (shutil.which("php") or scoop_php.exists()):
                self.append_output("[INFO] PHP installed via Scoop.")
                if scoop_php.exists():
                    return str(scoop_php.parent)
                return None
            else:
                self.append_output("[WARN] Scoop did not complete PHP installation or shims not found.")
        else:
            self.append_output("[INFO] Scoop not available on this system. Skipping.")

        # Try Chocolatey if available and we appear to be elevated
        is_admin = False
        try:
            import ctypes
            is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            pass

        if shutil.which("choco"):
            if is_admin:
                self.append_output("[INFO] Trying to install PHP via Chocolatey (admin detected).")
                code = self._run_tool_with_output([
                    "choco", "install", "php", "-y", "--no-progress"
                ], timeout=300)
                if code == 0 and shutil.which("php"):
                    self.append_output("[INFO] PHP installed via Chocolatey.")
                    return None
                else:
                    self.append_output("[WARN] Chocolatey did not complete PHP installation.")
            else:
                self.append_output("[INFO] Chocolatey present but no admin rights. Skipping non-elevated install.")
        else:
            self.append_output("[INFO] Chocolatey not available on this system. Skipping.")

        # Offer manual selection of php.exe
        self.append_output("[INFO] Unable to install PHP automatically. Select php.exe manually?")
        try:
            resp = messagebox.askyesno("PHP required", "Automatic PHP setup failed.\nDo you want to select an existing php.exe manually?")
        except Exception:
            resp = False
        if resp:
            path = filedialog.askopenfilename(title="Select php.exe", filetypes=[("php.exe", "php.exe"), ("All files", "*.*")])
            if path and Path(path).exists():
                self.append_output(f"[INFO] Using manually selected php.exe: {path}")
                # Persist to settings
                tool_paths = self.settings.get("tool_paths") or {}
                tool_paths["php"] = path
                self.settings["tool_paths"] = tool_paths
                save_settings(self.settings)
                return str(Path(path).parent)

        # Give up with clear instructions
        self.append_output("[ERROR] Could not set up PHP automatically.")
        self.append_output("        Install PHP via one of:")
        self.append_output("        - winget: winget install -e --id PHP.PHP")
        self.append_output("        - Scoop (no admin): scoop install php")
        self.append_output("        - Chocolatey (admin): choco install php -y")
        self.append_output("        Or place a portable php.exe under one of:")
        self.append_output("        - ./php")
        self.append_output("        - ./bin/php")
        self.append_output("        - ./tools/php")
        self.append_output("        Or set a custom path via manual selection when prompted.")
        return None

    def find_and_run(self, repo_dir: Path):
        candidates = ["main.py", "app.py", "run.py", "start.py", "st.py"]
        entry = None
        for root, dirs, files in os.walk(repo_dir):
            for c in candidates:
                if c in files:
                    entry = Path(root) / c
                    break
            if entry:
                break

        if not entry:
            self.append_output("[WARN] No common entry-point (main.py/app.py/run.py/start.py/st.py) found.")
            return

        # Preflight: detect if the repository likely needs PHP and ensure it's present
        needs_php = any(repo_dir.rglob("*.php"))
        php_dir_to_prepend = None
        if needs_php:
            php_dir_to_prepend = self.ensure_php_available()
            if not shutil.which("php") and php_dir_to_prepend is None:
                # Could not ensure php
                return

        env = os.environ.copy()
        if php_dir_to_prepend:
            env["PATH"] = php_dir_to_prepend + os.pathsep + env.get("PATH", "")

        # Provide selected files to the target script
        if self.export_env_var.get() and self.selected_files:
            env["APP_SELECTED_FILES"] = ";".join(self.selected_files)
            env["APP_SELECTED_FILE"] = self.selected_files[0]

        self.append_output(f"[INFO] Running: {entry}")
        args = [sys.executable, str(entry)]
        if self.pass_as_args_var.get() and self.selected_files:
            args.extend(self.selected_files)
        self.run_streaming(args, cwd=str(entry.parent), env=env)

    def run_and_wait(self, args, cwd=None):
        # For short tasks we can just run and stream output synchronously
        self.append_output(f"[CMD] {' '.join(args)}")
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        )
        for line in proc.stdout:
            self.append_output(line.rstrip("\n"))
        proc.wait()
        self.append_output(f"[INFO] Command exited with code {proc.returncode}")

    def run_streaming(self, args, cwd=None, env=None):
        self.append_output(f"[CMD] {' '.join(args)}")
        self.runner.run(args, cwd=cwd, env=env)

if __name__ == "__main__":
    try:
        app = App()
        # Initialize files box text with placeholder
        try:
            app.refresh_files_box()
        except Exception:
            pass
        app.mainloop()
    except Exception as e:
        # Best-effort error visibility if the GUI fails very early
        try:
            with open(str(APP_DIR / "app_error.log"), "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {repr(e)}\n")
        except Exception:
            pass
        try:
            import tkinter as tk
            from tkinter import messagebox as mb
            root = tk.Tk()
            root.withdraw()
            mb.showerror("Application Error", f"An error prevented the UI from starting:\n{e}")
            root.destroy()
        except Exception:
            pass
        raise