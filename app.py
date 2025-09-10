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

# Image utilities for owner icon
try:
    from PIL import Image, ImageOps, ImageDraw
except Exception:
    Image = None
    ImageOps = None
    ImageDraw = None

# Kept for backward compatibility, but embedded web view is disabled in current build
try:
    from tkinterweb import HtmlFrame  # lightweight embedded browser
except Exception:
    HtmlFrame = None

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
        self.geometry("1100x800")

        # Utility to ensure UI work runs on Tk main thread
        self._main_thread = threading.current_thread()

        self.settings = load_settings()
        self.repos = load_repos()
        self.selected_files = []
        self.pass_as_args_var = ctk.BooleanVar(value=True)
        self.export_env_var = ctk.BooleanVar(value=True)
        self.detected_urls = []
        self.last_url = None
        self.opened_urls = set()

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

        # Owner button (right-aligned) - text only (no image)
        top_right = ctk.CTkFrame(top)
        top_right.pack(side="right", padx=(10, 0))
        self.owner_btn = ctk.CTkButton(top_right, width=80, height=28, text="Owner", command=self.show_owner_popup)
        self.owner_btn.pack(side="right")

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

        self.files_box = ctk.CTkTextbox(files_frame, height=80, wrap="none", state="disabled")
        self.files_box.pack(side="top", fill="x", expand=False, pady=(5, 0))

        # Popular packages installer
        pkgs_frame = ctk.CTkFrame(self)
        pkgs_frame.pack(side="top", fill="x", padx=10, pady=(0, 10))

        pkgs_header = ctk.CTkFrame(pkgs_frame)
        pkgs_header.pack(side="top", fill="x")

        pkgs_label = ctk.CTkLabel(pkgs_header, text="Popular Python packages (click to select, then Install Selected):")
        pkgs_label.pack(side="left")

        self.install_btn = ctk.CTkButton(pkgs_header, text="Install Selected", command=self.install_selected_packages)
        self.install_btn.pack(side="right")

        self.select_all_btn = ctk.CTkButton(pkgs_header, text="Select All", width=100, command=self.select_all_packages)
        self.select_all_btn.pack(side="right", padx=(0, 8))

        # Scrollable list of checkboxes
        try:
            self.pkgs_list = ctk.CTkScrollableFrame(pkgs_frame, height=120)
        except Exception:
            self.pkgs_list = ctk.CTkFrame(pkgs_frame)
        self.pkgs_list.pack(side="top", fill="x", padx=0, pady=(6, 0))

        self.common_packages = [
            ("requests", "HTTP for Humans"),
            ("numpy", "Numerical computing"),
            ("pandas", "Data analysis"),
            ("matplotlib", "Plotting"),
            ("scipy", "Scientific computing"),
            ("scikit-learn", "Machine learning"),
            ("flask", "Web microframework"),
            ("fastapi", "High-performance APIs"),
            ("uvicorn", "ASGI server"),
            ("django", "Web framework"),
            ("streamlit", "Data apps"),
            ("jupyter", "Interactive computing"),
            ("notebook", "Jupyter Notebook"),
            ("beautifulsoup4", "HTML parsing"),
            ("lxml", "XML/HTML parser"),
            ("selenium", "Browser automation"),
            ("httpx", "Async HTTP client"),
            ("pydantic", "Data validation"),
            ("rich", "Rich text/formatting"),
            ("loguru", "Logging"),
            ("tqdm", "Progress bars"),
            ("pillow", "Imaging"),
            ("opencv-python", "Computer vision"),
            ("paramiko", "SSH2 for Python"),
            ("pyngrok", "ngrok tunnel controller"),
        ]
        self.pkg_vars = {}
        # Build columns (2 columns)
        col = 0
        row = 0
        for name, desc in self.common_packages:
            var = ctk.BooleanVar(value=False)
            cb = ctk.CTkCheckBox(self.pkgs_list, text=f"{name}  –  {desc}", variable=var)
            cb.grid(row=row, column=col, sticky="w", padx=6, pady=2)
            self.pkg_vars[name] = var
            col += 1
            if col >= 2:
                col = 0
                row += 1

        # Middle split: left console, right web preview
        split = ctk.CTkFrame(self)
        split.pack(side="top", fill="both", expand=True, padx=10, pady=(0, 10))

        left = ctk.CTkFrame(split)
        left.pack(side="left", fill="both", expand=True, padx=(0, 10))

        right = ctk.CTkFrame(split, width=400)
        right.pack(side="left", fill="both", expand=False)

        # Output console
        self.output_box = ctk.CTkTextbox(left, wrap="word")
        self.output_box.pack(side="top", fill="both", expand=True)

        # Web controls (external browser only)
        web_controls = ctk.CTkFrame(right)
        web_controls.pack(side="top", fill="x", padx=8, pady=8)

        self.url_label = ctk.CTkEntry(web_controls, placeholder_text="Detected local URL will appear here")
        self.url_label.pack(side="left", fill="x", expand=True, padx=(0, 8))

        open_btn = ctk.CTkButton(web_controls, text="Open in Browser", width=130, command=self.open_last_url)
        open_btn.pack(side="left")

        # Placeholder panel (no embedded browser)
        self.web_container = ctk.CTkFrame(right)
        self.web_container.pack(side="top", fill="both", expand=True, padx=8, pady=(0, 8))
        info = ctk.CTkLabel(self.web_container, text="Web preview disabled.\nLinks will open in your default browser.")
        info.pack(side="top", pady=20)

        # Input to send to running process (activity runner)
        input_frame = ctk.CTkFrame(self)
        input_frame.pack(side="bottom", fill="x", padx=10, pady=(0, 10))

        self.input_entry = ctk.CTkEntry(input_frame, placeholder_text="Type input for the running process and press Enter")
        self.input_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.input_entry.bind("<Return>", self.on_send_input)

        self.stop_button = ctk.CTkButton(input_frame, text="Stop Process", command=self.stop_process)
        self.stop_button.pack(side="left")

        # Terminals panel host (can detach to a separate window)
        self.terminal_container = ctk.CTkFrame(self)
        self.terminal_container.pack(side="bottom", fill="both", expand=True, padx=10, pady=(0, 10))
        self.term_window = None  # detached window handle

        # State
        self.runner = ProcessRunner(self.append_output)
        self.current_repo_dir = None
        self.terminals = []

        # Build terminal UI in main view
        self.build_terminal_ui(self.terminal_container, detached=Fa_codel)
        self.close_term_btn.pack(side="left")

        self.term_tabs = ctk.CTkTabview(terms_panel)
        self.term_tabs.pack(side="top", fill="both", expand=True)

        # State
        self.runner = ProcessRunner(self.append_output)
        self.current_repo_dir = None
        self.terminals = []
        self.new_terminal(initial=True)

    # ==== Popular packages helpers ====

    def select_all_packages(self):
        try:
            current = any(v.get() for v in self.pkg_vars.values())
            # Toggle behavior: if some are selected, clear all; else select all
            target = not current
            for v in self.pkg_vars.values():
                v.set(target)
        except Exception:
            pass

    def install_selected_packages(self):
        pkgs = [name for name, var in self.pkg_vars.items() if var.get()]
        if not pkgs:
            messagebox.showinfo("No selection", "Please select one or more packages to install.")
            return

        def _worker():
            try:
                self.install_btn.configure(state="disabled")
                py = str(APP_DIR / "venv" / "Scripts" / "python.exe")
                args = [py, "-m", "pip", "install"] + pkgs
                self.append_output(f"[INFO] Installing selected packages: {' '.join(pkgs)}")
                self.run_and_wait(args, cwd=str(APP_DIR))
                self.append_output("[INFO] Package installation finished.")
            finally:
                try:
                    self.install_btn.configure(state="normal")
                except Exception:
                    pass

        threading.Thread(target=_worker, daemon=True).start()

    
    def open_last_url(self):
        url = self.last_url or self.url_label.get().strip()
        if not url:
            messagebox.showinfo("No URL", "No local URL detected yet.")
            return
        import webbrowser
        try:
            webbrowser.open(url, new=2)
            self.append_output(f"[INFO] Opened in browser: {url}")
        except Exception as e:
            self.append_output(f"[WARN] Could not open browser: {e}")

    # ===== Terminal-like support =====

    def new_terminal(self, initial=False):
        idx = len(self.terminals) + 1
        tab_name = f"term-{idx}"
        tab = self.term_tabs.add(tab_name)

        # Create widgets
        text = ctk.CTkTextbox(tab, wrap="word")
        text.pack(side="top", fill="both", expand=True, padx=6, pady=(6, 3))

        entry = ctk.CTkEntry(tab, placeholder_text="Type a command (supports: clear, cd, pip install ..., ngrok ...) and press Enter")
        entry.pack(side="top", fill="x", padx=6, pady=(0, 6))
        # Bind handler with this session context
        entry.bind("<Return>", lambda e, tname=tab_name: self.on_terminal_enter(tname))

        # Session state
        session = {
            "name": tab_name,
            "text": text,
            "entry": entry,
            "cwd": str(self.current_repo_dir or APP_DIR),
            "runner": None,  # ProcessRunner for long/interactive commands
        }

        # Per-terminal runner with its own callback
        def term_append(msg):
            def _do(m=msg):
                self._ansi_to_tags_insert(text, m + "\n")
                text.see("end")
                self.update_idletasks()
            if threading.current_thread() is threading.main_thread():
                _do()
            else:
                self.after(0, _do)
        session["runner"] = ProcessRunner(term_append)

        # Prompt
        self._ansi_to_tags_insert(text, f"\x1b[92m[{tab_name}]\x1b[0m CWD: {session['cwd']}\n")
        self.terminals.append(session)
        self.term_tabs.set(tab_name)
        if not initial:
            self._ansi_to_tags_insert(text, f"\x1b[90m(type 'help' for hints)\x1b[0m\n")

    def close_current_terminal(self):
        current = self.term_tabs.get()
        if not current:
            return
        # Find session
        for i, s in enumerate(self.terminals):
            if s["name"] == current:
                try:
                    self.term_tabs.delete(current)
                except Exception:
                    pass
                # Terminate any running process
                try:
                    s["runner"].terminate()
                except Exception:
                    pass
                del self.terminals[i]
                break
        # Select another tab if exists
        if self.terminals:
            self.term_tabs.set(self.terminals[-1]["name"])

    # ==== Terminal container management (detach/attach) ====

    def build_terminal_ui(self, host, detached: bool = False):
        # Destroy any existing children of host
        try:
            for w in host.winfo_children():
                w.destroy()
        except Exception:
            pass

        terms_panel = ctk.CTkFrame(host)
        terms_panel.pack(side="top", fill="both", expand=True)

        controls = ctk.CTkFrame(terms_panel)
        controls.pack(side="top", fill="x", pady=(0, 6))

        self.new_term_btn = ctk.CTkButton(controls, text="New Terminal", command=self.new_terminal)
        self.new_term_btn.pack(side="left", padx=(0, 8))

        self.close_term_btn = ctk.CTkButton(controls, text="Close Terminal", command=self.close_current_terminal)
        self.close_term_btn.pack(side="left", padx=(0, 8))

        # Toggle button: detach or attach
        if detached:
            toggle_btn = ctk.CTkButton(controls, text="Attach to Main", command=self.attach_terminal)
        else:
            toggle_btn = ctk.CTkButton(controls, text="Pop-out Terminal", command=self.detach_terminal)
        toggle_btn.pack(side="left")

        self.term_tabs = ctk.CTkTabview(terms_panel)
        self.term_tabs.pack(side="top", fill="both", expand=True)

        # Reset sessions
        self.terminals = []
        self.new_terminal(initial=True)

    def detach_terminal(self):
        if self.term_window is not None:
            try:
                self.term_window.lift()
            except Exception:
                pass
            return
        # Create detachable window and rebuild terminals there
        win = ctk.CTkToplevel(self)
        win.title("Terminal")
        win.geometry("900x400")
        win.resizable(True, True)
        self.term_window = win
        self.build_terminal_ui(win, detached=True)

        def on_close():
            # When closed, attach back to main automatically
            try:
                self.attach_terminal()
            except Exception:
                pass
        try:
            win.protocol("WM_DELETE_WINDOW", on_close)
        except Exception:
            pass

    def attach_terminal(self):
        # Destroy detachable window if exists
        if self.term_window is not None:
            try:
                self.term_window.destroy()
            except Exception:
                pass
            self.term_window = None
        # Rebuild in main container
        self.build_terminal_ui(self.terminal_container, detached=False)

    def get_session(self, tab_name):
        for s in self.terminals:
            if s["name"] == tab_name:
                return s
        return None

    def term_print(self, session, msg):
        text = session["text"]
        self._ansi_to_tags_insert(text, msg + "\n")
        text.see("end")
        self.update_idletasks()

    def on_terminal_enter(self, tab_name):
        session = self.get_session(tab_name)
        if not session:
            return
        entry = session["entry"]
        cmd = entry.get().strip()
        entry.delete(0, "end")

        # If an interactive process is running, send input
        runner = session["runner"]
        if runner and runner.proc:
            self.term_print(session, f">> {cmd}")
            runner.send_input(cmd)
            return

        if not cmd:
            return

        # Echo prompt-like line
        self.term_print(session, f"\x1b[96m{session['cwd']}\x1b[0m> {cmd}")

        # Builtins
        if cmd.lower() in ("clear", "cls"):
            try:
                session["text"].delete("1.0", "end")
            except Exception:
                pass
            return

        if cmd.lower() == "help":
            self.term_print(session, "Built-ins: clear, cd <dir>, pip install <pkg>, ngrok <args>")
            self.term_print(session, "General commands are executed via system shell.")
            return

        if cmd.lower().startswith("cd"):
            parts = cmd.split(maxsplit=1)
            if len(parts) == 1:
                # show cwd
                self.term_print(session, session["cwd"])
            else:
                target = parts[1].strip().strip('"')
                new_cwd = Path(session["cwd"]).joinpath(target).resolve() if not Path(target).is_absolute() else Path(target)
                if new_cwd.exists() and new_cwd.is_dir():
                    session["cwd"] = str(new_cwd)
                    self.term_print(session, f"Changed directory to {session['cwd']}")
                else:
                    self.term_print(session, f"\x1b[31mDirectory not found:\x1b[0m {new_cwd}")
            return

        if cmd.lower().startswith("pip "):
            # Route to venv pip
            args = cmd.split()[1:]
            py = str((APP_DIR / "venv" / "Scripts" / "python.exe"))
            full = [py, "-m", "pip"] + args
            self._terminal_run_process(session, full)
            return

        if cmd.lower().startswith("ngrok"):
            ngrok_path = self._ensure_ngrok()
            if not ngrok_path:
                self.term_print(session, "\x1b[31mngrok not available.\x1b[0m Set it in settings or install manually.")
                return
            args = cmd.split()[1:]
            full = [ngrok_path] + args
            self._terminal_run_process(session, full)
            return

        # Aliases for Linux-like commands on Windows
        shell_cmd = cmd
        if os.name == "nt":
            aliases = {
                "ls": "dir",
                "pwd": "cd",
                "cat": "type",
                "clear": "cls",
            }
            head = cmd.split()[0]
            if head in aliases:
                shell_cmd = cmd.replace(head, aliases[head], 1)

        # Run generic command via shell
        if os.name == "nt":
            full = ["cmd", "/c", shell_cmd]
        else:
            full = ["bash", "-lc", shell_cmd]
        self._terminal_run_process(session, full)

    def _terminal_run_process(self, session, args):
        # Run and stream in this terminal; if long-lived, you can still type to stdin
        try:
            cwd = session["cwd"]
            session["runner"].run(args, cwd=cwd)
        except Exception as e:
            self.term_print(session, f"\x1b[31mERROR:\x1b[0m {e}")

    def _ensure_ngrok(self):
        # Check saved path
        tool_paths = self.settings.get("tool_paths") or {}
        path = tool_paths.get("ngrok")
        if path and Path(path).exists():
            return path
        # PATH
        found = shutil.which("ngrok")
        if found:
            return found
        # Ask user to select ngrok.exe manually (no auto-download for safety)
        try:
            if messagebox.askyesno("ngrok required", "ngrok not found. Select ngrok executable manually?"):
                p = filedialog.askopenfilename(title="Select ngrok executable", filetypes=[("ngrok.exe", "ngrok.exe"), ("All files", "*.*")])
                if p and Path(p).exists():
                    tool_paths["ngrok"] = p
                    self.settings["tool_paths"] = tool_paths
                    save_settings(self.settings)
                    return p
        except Exception:
            pass
        return None

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

    def strip_ansi(self, s: str) -> str:
        try:
            import re
            ansi_re = re.compile(r"\x1B\[([0-?]*[ -/]*[@-~])")
            return ansi_re.sub("", s)
        except Exception:
            return s

    def detect_url(self, s: str):
        try:
            import re
            m = re.search(r"(https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0):\d+(?:/\S*)?)", s, re.I)
            if m:
                url = m.group(1)
                def _apply():
                    if url not in self.detected_urls:
                        self.detected_urls.append(url)
                    self.last_url = url
                    try:
                        self.url_label.delete(0, "end")
                        self.url_label.insert(0, url)
                    except Exception:
                        pass
                    # Open externally once per URL for safety
                    if url not in self.opened_urls:
                        self.opened_urls.add(url)
                        self.open_last_url()
                if threading.current_thread() is threading.main_thread():
                    _apply()
                else:
                    self.after(0, _apply)
        except Exception:
            pass

    def clear_console(self):
        try:
            self.output_box.delete("1.0", "end")
        except Exception:
            pass

    # ==== Owner icon helpers ====

    def _load_owner_icon(self, path: Path, size=32):
        # Return a CTkImage with circular-cropped owner photo if Pillow is available
        if Image is None:
            raise RuntimeError("Pillow not available")
        img = Image.open(str(path)).convert("RGBA")
        img = ImageOps.fit(img, (size, size), Image.LANCZOS)
        # circular mask
        mask = Image.new("L", (size, size), 0)
        draw = ImageDraw.Draw(mask)
        draw.ellipse((0, 0, size, size), fill=255)
        img.putalpha(mask)
        return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))

    def show_owner_popup(self):
        import webbrowser
        win = ctk.CTkToplevel(self)
        win.title("Owner")
        win.geometry("300x360")
        win.resizable(False, False)
        # Keep always on top and modal-like
        try:
            win.transient(self)
            win.attributes("-topmost", True)
            win.focus_force()
            win.grab_set()
        except Exception:
            pass

        # Image
        try:
            avatar = self._load_owner_icon(APP_DIR / "pro.jpg", size=120)
            img_label = ctk.CTkLabel(win, image=avatar, text="")
            # keep reference to avoid GC
            img_label.image = avatar
            img_label.pack(pady=(20, 10))
        except Exception:
            ctk.CTkLabel(win, text="(owner image not found: pro.jpg)").pack(pady=(20, 10))

        # Name
        ctk.CTkLabel(win, text="Janith Manodya", font=("Arial", 16, "bold")).pack(pady=(0, 6))

        # Facebook link button
        def open_fb():
            try:
                webbrowser.open("https://web.facebook.com/janith.manodaya.3", new=2)
            except Exception:
                pass
        fb_btn = ctk.CTkButton(win, text="Open Facebook", command=open_fb, width=160)
        fb_btn.pack(pady=(6, 12))

        # Close button
        close_btn = ctk.CTkButton(win, text="Close", command=win.destroy, width=100)
        close_btn.pack(pady=(0, 12))

    def _init_text_tags(self, widget):
        # Foreground colors
        fg_colors = {
            "fg_black": "#000000",
            "fg_red": "#cc0000",
            "fg_green": "#00aa00",
            "fg_yellow": "#bb8800",
            "fg_blue": "#0066cc",
            "fg_magenta": "#aa00aa",
            "fg_cyan": "#008888",
            "fg_white": "#dddddd",
            "fg_bblack": "#555555",   # bright black (gray)
            "fg_bred": "#ff5555",
            "fg_bgreen": "#55ff55",
            "fg_byellow": "#ffff55",
            "fg_bblue": "#5599ff",
            "fg_bmagenta": "#ff55ff",
            "fg_bcyan": "#55ffff",
            "fg_bwhite": "#ffffff",
        }
        for tag, color in fg_colors.items():
            try:
                widget.tag_configure(tag, foreground=color)
            except Exception:
                pass

        # Backgrounds (limited set to avoid visual clutter)
        bg_colors = {
            "bg_red": "#440000",
            "bg_green": "#003300",
            "bg_yellow": "#3a2e00",
            "bg_blue": "#001a33",
            "bg_magenta": "#2a0033",
            "bg_cyan": "#003333",
            "bg_white": "#666666",
        }
        for tag, color in bg_colors.items():
            try:
                widget.tag_configure(tag, background=color)
            except Exception:
                pass

        try:
            widget.tag_configure("bold", font=("Consolas", 11, "bold"))
        except Exception:
            pass

    def _ansi_to_tags_insert(self, widget, text: str):
        """
        Parse ANSI SGR sequences and insert into Text with tags for colors/bold.
        """
        try:
            import re
            # Ensure tags initialized per widget
            if not hasattr(widget, "_tags_inited"):
                self._init_text_tags(widget)
                widget._tags_inited = True  # type: ignore[attr-defined]

            # Map SGR to tags
            fg_map = {
                30: "fg_black", 31: "fg_red", 32: "fg_green", 33: "fg_yellow",
                34: "fg_blue", 35: "fg_magenta", 36: "fg_cyan", 37: "fg_white",
                90: "fg_bblack", 91: "fg_bred", 92: "fg_bgreen", 93: "fg_byellow",
                94: "fg_bblue", 95: "fg_bmagenta", 96: "fg_bcyan", 97: "fg_bwhite",
            }
            bg_map = {
                41: "bg_red", 42: "bg_green", 43: "bg_yellow",
                44: "bg_blue", 45: "bg_magenta", 46: "bg_cyan", 47: "bg_white",
            }

            ansi_re = re.compile(r"\x1b\[([0-9;]*)m")
            pos = 0
            current_tags = set()
            for m in ansi_re.finditer(text):
                chunk = text[pos:m.start()]
                if chunk:
                    widget.insert("end", chunk, tuple(current_tags) if current_tags else ())
                codes = m.group(1)
                if codes == "" or codes == "0":
                    current_tags.clear()
                else:
                    for c in codes.split(";"):
                        try:
                            n = int(c)
                        except ValueError:
                            continue
                        if n == 0:
                            current_tags.clear()
                        elif n == 1:
                            current_tags.add("bold")
                        elif n in fg_map:
                            # remove existing fg tags
                            for t in list(current_tags):
                                if t.startswith("fg_"):
                                    current_tags.discard(t)
                            current_tags.add(fg_map[n])
                        elif n in bg_map:
                            for t in list(current_tags):
                                if t.startswith("bg_"):
                                    current_tags.discard(t)
                            current_tags.add(bg_map[n])
                        elif n == 39:
                            # default foreground
                            for t in list(current_tags):
                                if t.startswith("fg_"):
                                    current_tags.discard(t)
                        elif n == 49:
                            for t in list(current_tags):
                                if t.startswith("bg_"):
                                    current_tags.discard(t)
                        else:
                            # ignore unsupported attributes
                            pass
                pos = m.end()
            # Tail
            tail = text[pos:]
            if tail:
                widget.insert("end", tail, tuple(current_tags) if current_tags else ())
        except Exception:
            # Fallback: plain insert
            widget.insert("end", self.strip_ansi(text))

    def _append_output_ui(self, text):
        # Insert with ANSI color support
        self._ansi_to_tags_insert(self.output_box, text + "\n")
        self.output_box.see("end")
        # URL detection based on cleaned text
        clean = self.strip_ansi(text)
        self.detect_url(clean)
        self.update_idletasks()

    def append_output(self, text):
        if threading.current_thread() is threading.main_thread():
            self._append_output_ui(text)
        else:
            self.after(0, lambda t=text: self._append_output_ui(t))

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

        # Clear previous logs for a clean session
        self.clear_console()
        self.run_button.configure(state="disabled")
        self.append_output(f"[INFO] Starting workflow for {url} ...")

        t = threading.Thread(target=self.run_repo, args=(url,), daemon=True)
        t.start()

    def run_repo(self, url):
        try:
            repo_dir = self.clone_repo(url)
            self.current_repo_dir = repo_dir
            env = os.environ.copy()
            self.install_dependencies(repo_dir, env)
            self.find_and_run(repo_dir)
        except Exception as e:
            self.append_output(f"[ERROR] {e}")
        finally:
            self.run_button.configure(state="norm_codealnew"</)
)

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

    def install_dependencies(self, repo_dir: Path, env: dict | None = None):
        """
        Best-effort dependency setup:
          - pip install -r requirements.txt if present
          - pip install . if pyproject.toml or setup.py exists
          - run install/setup/bootstrap scripts if present (sh/bat/ps1)
        """
        # 1) Python requirements
        req = repo_dir / "requirements.txt"
        if req.exists():
            self.append_output("[INFO] Installing repository requirements (requirements.txt)...")
            args = [sys.executable, "-m", "pip", "install", "-r", str(req)]
            self.run_and_wait(args, cwd=str(repo_dir))
        else:
            # Try pyproject.toml or setup.py
            if (repo_dir / "pyproject.toml").exists() or (repo_dir / "setup.py").exists():
                self.append_output("[INFO] Installing project as a package (pip install .)...")
                args = [sys.executable, "-m", "pip", "install", "."]
                self.run_and_wait(args, cwd=str(repo_dir))
            else:
                self.append_output("[INFO] No requirements.txt / pyproject.toml / setup.py found.")

        # 2) Generic installer scripts
        sh_scripts = ["install.sh", "setup.sh", "bootstrap.sh"]
        bat_scripts = ["install.bat", "setup.bat"]
        ps1_scripts = ["install.ps1", "setup.ps1"]

        # Choose first matching script that exists
        chosen = None
        chosen_type = None
        for name in sh_scripts:
            p = repo_dir / name
            if p.exists():
                chosen, chosen_type = p, "sh"
                break
        if chosen is None:
            for name in bat_scripts:
                p = repo_dir / name
                if p.exists():
                    chosen, chosen_type = p, "bat"
                    break
        if chosen is None:
            for name in ps1_scripts:
                p = repo_dir / name
                if p.exists():
                    chosen, chosen_type = p, "ps1"
                    break

        if chosen:
            self.append_output(f"[INFO] Running installer script: {chosen.name}")
            try:
                if chosen_type == "sh":
                    bash = shutil.which("bash")
                    if not bash:
                        cand = Path("C:/Program Files/Git/bin/bash.exe")
                        if cand.exists():
                            bash = str(cand)
                    if not bash:
                        self.append_output("[WARN] Bash not found. Skipping shell installer.")
                    else:
                        self.run_and_wait([bash, str(chosen)], cwd=str(repo_dir))
                elif chosen_type == "bat":
                    if os.name == "nt":
                        self.run_and_wait(["cmd", "/c", str(chosen)], cwd=str(repo_dir))
                    else:
                        self.append_output("[WARN] .bat installer found but not on Windows. Skipping.")
                elif chosen_type == "ps1":
                    pwsh = shutil.which("powershell") or shutil.which("pwsh")
                    if not pwsh:
                        self.append_output("[WARN] PowerShell not found. Skipping PS installer.")
                    else:
                        self.run_and_wait([pwsh, "-ExecutionPolicy", "Bypass", "-File", str(chosen)], cwd=str(repo_dir))
            except Exception as e:
                self.append_output(f"[WARN] Installer script failed: {e}")

    # ==== Manual selection popup for installer and runner ====

    def _run_installer_file(self, file_path: Path, repo_dir: Path):
        ext = file_path.suffix.lower()
        try:
            if ext == ".sh":
                bash = shutil.which("bash")
                if not bash:
                    cand = Path("C:/Program Files/Git/bin/bash.exe")
                    if cand.exists():
                        bash = str(cand)
                if not bash:
                    self.append_output("[WARN] bash not found. Cannot run shell installer.")
                    return
                self.run_and_wait([bash, str(file_path)], cwd=str(repo_dir))
            elif ext == ".bat" and os.name == "nt":
                self.run_and_wait(["cmd", "/c", str(file_path)], cwd=str(repo_dir))
            elif ext == ".ps1":
                pwsh = shutil.which("powershell") or shutil.which("pwsh")
                if not pwsh:
                    self.append_output("[WARN] PowerShell not found. Cannot run PS installer.")
                    return
                self.run_and_wait([pwsh, "-ExecutionPolicy", "Bypass", "-File", str(file_path)], cwd=str(repo_dir))
            elif ext == ".py":
                self.run_and_wait([sys.executable, str(file_path)], cwd=str(repo_dir))
            else:
                self.append_output(f"[WARN] Unsupported installer type: {file_path.name}")
        except Exception as e:
            self.append_output(f"[WARN] Manual installer failed: {e}")

    def _run_selected_file(self, file_path: Path, env: dict):
        p = file_path
        ext = p.suffix.lower()
        if ext == ".py":
            self.run_streaming([sys.executable, str(p)], cwd=str(p.parent), env=env)
            return
        if ext == ".sh":
            bash = shutil.which("bash")
            if not bash:
                cand = Path("C:/Program Files/Git/bin/bash.exe")
                if cand.exists():
                    bash = str(cand)
            if bash and Path(str(bash)).exists():
                self.run_streaming([bash, str(p)], cwd=str(p.parent), env=env)
                return
            self.append_output("[ERROR] bash not found to run .sh file.")
            return
        if ext == ".bat" and os.name == "nt":
            self.run_streaming(["cmd", "/c", str(p)], cwd=str(p.parent), env=env)
            return
        if ext == ".ps1":
            pwsh = shutil.which("powershell") or shutil.which("pwsh")
            if pwsh:
                self.run_streaming([pwsh, "-ExecutionPolicy", "Bypass", "-File", str(p)], cwd=str(p.parent), env=env)
                return
            self.append_output("[ERROR] PowerShell not found to run .ps1 file.")
            return
        if ext == ".php":
            php_dir_to_prepend = self.ensure_php_available()
            if php_dir_to_prepend:
                env["PATH"] = php_dir_to_prepend + os.pathsep + env.get("PATH", "")
            if shutil.which("php"):
                self.run_streaming(["php", str(p)], cwd=str(p.parent), env=env)
                return
            self.append_output("[ERROR] php not found to run .php file.")
            return
        self.append_output(f"[ERROR] Unsupported file to run: {p.name}")

    def _guess_install_script(self, repo_dir: Path) -> Path | None:
        for name in ("install.sh", "setup.sh", "bootstrap.sh", "install.bat", "setup.bat", "install.ps1", "setup.ps1"):
            p = repo_dir / name
            if p.exists():
                return p
        return None

    def _guess_run_script(self, repo_dir: Path) -> Path | None:
        # Prefer common python entries
        for root, _, files in os.walk(repo_dir):
            for c in ("main.py", "app.py", "run.py", "start.py", "st.py"):
                if c in files:
                    return Path(root) / c
        # Then .sh/.bat/.ps1 in root
        for pattern in ("*.sh", "*.bat", "*.ps1"):
            match = next(repo_dir.glob(pattern), None)
            if match:
                return match
        # Then any python in root
        p = next(repo_dir.glob("*.py"), None)
        return p

    def prompt_manual_selection(self, repo_dir: Path, env: dict):
        def _open():
            win = ctk.CTkToplevel(self)
            win.title("Select installer (optional) and runner")
            win.geometry("600x220")
            win.resizable(True, False)
            try:
                win.transient(self)
                win.grab_set()
            except Exception:
                pass

            # Rows
            row_install = ctk.CTkFrame(win)
            row_install.pack(fill="x", padx=10, pady=(10, 6))
            ctk.CTkLabel(row_install, text="Installer (optional):").pack(side="left")
            inst_entry = ctk.CTkEntry(row_install, width=380)
            inst_entry.pack(side="left", padx=6, fill="x", expand=True)
            def browse_inst():
                p = filedialog.askopenfilename(
                    title="Select installer script (optional)",
                    initialdir=str(repo_dir),
                    filetypes=[("Scripts", "*.sh;*.bat;*.ps1;*.py"), ("All files", "*.*")]
                )
                if p:
                    inst_entry.delete(0, "end")
                    inst_entry.insert(0, p)
            ctk.CTkButton(row_install, text="Browse", width=80, command=browse_inst).pack(side="left")

            row_run = ctk.CTkFrame(win)
            row_run.pack(fill="x", padx=10, pady=(0, 6))
            ctk.CTkLabel(row_run, text="Runner (required):").pack(side="left")
            run_entry = ctk.CTkEntry(row_run, width=380)
            run_entry.pack(side="left", padx=6, fill="x", expand=True)
            def browse_run():
                p = filedialog.askopenfilename(
                    title="Select file to run",
                    initialdir=str(repo_dir),
                    filetypes=[("Runnable", "*.py;*.sh;*.bat;*.ps1;*.php"), ("All files", "*.*")]
                )
                if p:
                    run_entry.delete(0, "end")
                    run_entry.insert(0, p)
            ctk.CTkButton(row_run, text="Browse", width=80, command=browse_run).pack(side="left")

            # Pre-fill guesses
            try:
                gi = self._guess_install_script(repo_dir)
                if gi:
                    inst_entry.insert(0, str(gi))
                gr = self._guess_run_script(repo_dir)
                if gr:
                    run_entry.insert(0, str(gr))
            except Exception:
                pass

            # Buttons
            btns = ctk.CTkFrame(win)
            btns.pack(fill="x", padx=10, pady=(10, 10))
            skip_var = ctk.BooleanVar(value=True)
            ctk.CTkCheckBox(btns, text="Skip installer (if specified)", variable=skip_var).pack(side="left")

            def start_now():
                inst = inst_entry.get().strip()
                runf = run_entry.get().strip()
                if not runf:
                    messagebox.showerror("Missing runner", "Please choose a file to run.")
                    return

                def worker():
                    # optional installer
                    if inst and not skip_var.get():
                        self._run_installer_file(Path(inst), repo_dir)
                    # runner
                    self._run_selected_file(Path(runf), env)
                threading.Thread(target=worker, daemon=True).start()
                try:
                    win.destroy()
                except Exception:
                    pass

            ctk.CTkButton(btns, text="Run", command=start_now, width=120).pack(side="right")
            ctk.CTkButton(btns, text="Cancel", command=win.destroy, width=80).pack(side="right", padx=(0, 8))

        if threading.current_thread() is threading.main_thread():
            _open()
        else:
            self.after(0, _open)

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

    def _run_with_node_if_possible(self, repo_dir: Path, env: dict) -> bool:
        pkg = repo_dir / "package.json"
        if not pkg.exists():
            return False
        try:
            import json as _json
            data = _json.loads(pkg.read_text(encoding="utf-8"))
            scripts = (data.get("scripts") or {}) if isinstance(data, dict) else {}
            # Choose a common start script
            script = None
            for key in ("dev", "start", "serve"):
                if key in scripts:
                    script = key
                    break
            if script is None:
                self.append_output("[INFO] package.json found but no 'start'/'dev'/'serve' script.")
                return False
        except Exception as e:
            self.append_output(f"[WARN] Could not parse package.json: {e}")
            return False

        # Check node and npm availability
        node = shutil.which("node")
        npm = shutil.which("npm")
        if not node or not npm:
            self.append_output("[WARN] Node.js/NPM not found on PATH. Install Node.js to run this repo.")
            return False

        # Install and run
        self.append_output("[INFO] Detected Node project. Running 'npm install' then 'npm run {script}'...")
        self.run_and_wait([npm, "install"], cwd=str(repo_dir))
        self.run_streaming([npm, "run", script], cwd=str(repo_dir), env=env)
        return True

    def _run_with_bash_if_possible(self, repo_dir: Path, env: dict) -> bool:
        # Find a bash script that looks like an entry
        sh_candidates = [
            "start.sh", "run.sh", "serve.sh", "launch.sh", "bootstrap.sh",
            "zphisher.sh"
        ]
        target = None
        for c in sh_candidates:
            p = repo_dir / c
            if p.exists():
                target = p
                break
        if target is None:
            # last resort: any top-level .sh
            for p in repo_dir.glob("*.sh"):
                target = p
                break
        if target is None:
            return False

        # Locate bash (Git Bash or WSL bash or any bash)
        bash = shutil.which("bash")
        if not bash:
            # Common Git Bash location
            cand = Path("C:/Program Files/Git/bin/bash.exe")
            if cand.exists():
                bash = str(cand)
        if not bash:
            self.append_output("[WARN] Bash script detected but 'bash' not found. Install Git for Windows or enable WSL.")
            return False

        self.append_output(f"[INFO] Detected bash script: {target.name}. Running via bash...")
        self.run_streaming([bash, str(target)], cwd=str(repo_dir), env=env)
        return True

    def _run_with_php_server_if_possible(self, repo_dir: Path, env: dict) -> bool:
        # Simple heuristic: index.php present
        idx = repo_dir / "index.php"
        if not idx.exists():
            return False

        php_dir_to_prepend = self.ensure_php_available()
        if not shutil.which("php") and php_dir_to_prepend is None:
            return False
        if php_dir_to_prepend:
            env["PATH"] = php_dir_to_prepend + os.pathsep + env.get("PATH", "")

        # Choose port
        port = "8000"
        self.append_output(f"[INFO] Detected PHP app. Running: php -S localhost:{port} -t .")
        args = ["php", "-S", f"localhost:{port}", "-t", "."]
        self.run_streaming(args, cwd=str(repo_dir), env=env)
        return True

    def _run_with_bat_if_possible(self, repo_dir: Path, env: dict) -> bool:
        if os.name != "nt":
            return False
        # Typical batch starters
        bat_candidates = ["start.bat", "run.bat", "serve.bat", "launch.bat"]
        target = None
        for c in bat_candidates:
            p = repo_dir / c
            if p.exists():
                target = p
                break
        if target is None:
            for p in repo_dir.glob("*.bat"):
                target = p
                break
        if target is None:
            return False
        self.append_output(f"[INFO] Detected batch script: {target.name}. Running via cmd ...")
        self.run_streaming(["cmd", "/c", str(target)], cwd=str(repo_dir), env=env)
        return True

    def _run_with_powershell_if_possible(self, repo_dir: Path, env: dict) -> bool:
        # Look for ps1 scripts that look like starters
        ps1_candidates = ["start.ps1", "run.ps1", "serve.ps1", "launch.ps1"]
        target = None
        for c in ps1_candidates:
            p = repo_dir / c
            if p.exists():
                target = p
                break
        if target is None:
            for p in repo_dir.glob("*.ps1"):
                target = p
                break
        if target is None:
            return False
        pwsh = shutil.which("powershell") or shutil.which("pwsh")
        if not pwsh:
            self.append_output("[WARN] PowerShell not found on PATH.")
            return False
        self.append_output(f"[INFO] Detected PowerShell script: {target.name}. Running via PowerShell ...")
        self.run_streaming([pwsh, "-ExecutionPolicy", "Bypass", "-File", str(target)], cwd=str(repo_dir), env=env)
        return True

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

        env = os.environ.copy()

        if entry:
            # Preflight: detect if the repository likely needs PHP and ensure it's present
            needs_php = any(repo_dir.rglob("*.php"))
            php_dir_to_prepend = None
            if needs_php:
                php_dir_to_prepend = self.ensure_php_available()
                if not shutil.which("php") and php_dir_to_prepend is None:
                    # Could not ensure php
                    return
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
            return

        # If no Python entry found, try broader heuristics
        self.append_output("[WARN] No common Python entry-point found. Trying other strategies...")

        # 1) Node.js project via package.json
        if self._run_with_node_if_possible(repo_dir, env):
            return

        # 2) Bash script (e.g. zphisher.sh)
        if self._run_with_bash_if_possible(repo_dir, env):
            return

        # 3) Batch script (.bat) on Windows
        if self._run_with_bat_if_possible(repo_dir, env):
            return

        # 4) PowerShell script (.ps1)
        if self._run_with_powershell_if_possible(repo_dir, env):
            return

        # 5) Simple PHP app via built-in server (index.php)
        if self._run_with_php_server_if_possible(repo_dir, env):
            return

        # 6) Generic Python script fallback: common names anywhere, else any top-level .py
        common_py_names = ("main", "app", "run", "start", "server", "manage", "cli", "tool", "index")
        chosen_py = None
        for root, dirs, files in os.walk(repo_dir):
            for f in files:
                if f.endswith(".py"):
                    stem = Path(f).stem.lower()
                    if stem in common_py_names or any(stem.startswith(p) for p in common_py_names):
                        chosen_py = Path(root) / f
                        break
            if chosen_py:
                break
        if not chosen_py:
            for f in repo_dir.glob("*.py"):
                chosen_py = f
                break
        if chosen_py:
            self.append_output(f"[INFO] Fallback: running Python script {chosen_py.name}")
            args = [sys.executable, str(chosen_py)]
            if self.pass_as_args_var.get() and self.selected_files:
                args.extend(self.selected_files)
            self.run_streaming(args, cwd=str(chosen_py.parent), env=env)
            return

        # 7) Ask the user to select installer (optional) and runner
        self.prompt_manual_selection(repo_dir, env)
        return

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