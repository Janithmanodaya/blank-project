import os
import sys
from pathlib import Path

def set_working_directory():
    """
    Choose a sensible working directory before starting the GUI.
    By default we just chdir to the user's home to avoid write issues
    in protected locations when running as a bundled exe.
    """
    # Prefer user's home directory
    home = Path.home()
    try:
        os.chdir(str(home))
    except Exception:
        # Fallback to current directory
        pass

def ensure_sys_path():
    """
    Make sure the bundled app package path is importable both when running
    as a script and when running from a PyInstaller onefile build.
    """
    # When bundled with PyInstaller, extracted files are in _MEIPASS
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

def main():
    set_working_directory()
    ensure_sys_path()

    # Import and launch the existing app
    try:
        import app  # uses CustomTkinter GUI defined in app.py
    except Exception as e:
        # Provide a basic visible error if import fails
        import traceback
        tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        print("[ERROR] Failed to import app.py")
        print(tb)
        # Try a safe message box without importing customtkinter
        try:
            import tkinter as tk
            from tkinter import messagebox as mb
            root = tk.Tk()
            root.withdraw()
            mb.showerror("Launcher Error", f"Failed to import app.py:\n{e}")
            root.destroy()
        except Exception:
            pass
        sys.exit(1)

    # Run the App as in app.py's __main__ section
    try:
        # If app.py declares an App class and runs in __main__, we can replicate that here
        # by creating the App and starting mainloop.
        if hasattr(app, "App"):
            app_instance = app.App()
            try:
                # initialize files box if available
                if hasattr(app_instance, "refresh_files_box"):
                    app_instance.refresh_files_box()
            except Exception:
                pass
            app_instance.mainloop()
        else:
            # Fallback: execute app as a module if App not found
            if hasattr(app, "main"):
                app.main()
            else:
                print("[ERROR] No App class or main() found in app.py")
                sys.exit(2)
    except Exception as e:
        # Mirror app.py's early error handling for visibility
        try:
            import time as _time
            APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
            with open(str(APP_DIR / "app_error.log"), "a", encoding="utf-8") as f:
                f.write(f"{_time.strftime('%Y-%m-%d %H:%M:%S')} - {repr(e)}\n")
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

if __name__ == "__main__":
    main()