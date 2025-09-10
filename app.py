import os
import sys
import threading
import queue
import subprocess
import time
import json
import shutil
from pathlib import Path

import customtkinter as ctk
from tkinter import filedialog, messagebox

from git import Repo, GitCommandError

APP_DIR = Path(__file__).resolve().parent
CLONES_DIR = APP_DIR / "cloned_repos"
CLONES_DIR.mkdir(exist_ok=True)
REPOS_JSON = APP_DIR / "repos.json"

DEFAULT_REPOS = [
    "https://github.com/psf/requests",
    "https://github.com/pallets/flask",
    "https://github.com/streamlit/streamlit-example",
]

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

        self.repos = load_repos()

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
        php_in_path = shutil.which("php") is not None
        php_bundle_dir = None
        if not php_in_path:
            # Check for a bundled php.exe under common folders
            for cand in [APP_DIR / "php", APP_DIR / "tools" / "php", APP_DIR / "bin" / "php"]:
                if (cand / "php.exe").exists():
                    php_bundle_dir = str(cand)
                    php_in_path = True
                    break

        if needs_php and not php_in_path:
            self.append_output("[ERROR] This repository appears to require PHP (found .php files), but php.exe was not found.")
            self.append_output("        Install PHP and ensure php.exe is on your PATH, or place a portable PHP under one of:")
            self.append_output("        - ./php")
            self.append_output("        - ./tools/php")
            self.append_output("        - ./bin/php")
            self.append_output("        Then click Run again.")
            return

        env = os.environ.copy()
        if php_bundle_dir:
            env["PATH"] = php_bundle_dir + os.pathsep + env.get("PATH", "")

        self.append_output(f"[INFO] Running: {entry}")
        args = [sys.executable, str(entry)]
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
        self.runner.run(args, cwd
if __name__ == "__main__":
    try:
        app = App()
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
            mb.showerror("Application Error", f"An error prevented the UI from starting:\\n{e}")
            root.destroy()
        except Exception:
            pass
        raise