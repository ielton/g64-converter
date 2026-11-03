"""Minimal graphical front-end for g64_to_mp4 (tkinter, standard library only).

Opened automatically when the program is started without arguments.
"""
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, scrolledtext, ttk

import g64_to_mp4 as core

AUTHOR = "Elton Pinto"
AUTHOR_GITHUB = "https://github.com/ielton"
PROJECT_URL = "https://github.com/ielton/g64-converter"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("G64 to MP4 converter %s" % core.__version__)
        self.minsize(640, 420)
        self.queue = queue.Queue()
        self.worker = None
        self.source = tk.StringVar()
        self.output = tk.StringVar()
        self.status = tk.StringVar(value="Pick a .zip export, a folder of .g64 files, or one .g64 file.")
        self._build()
        self.after(100, self._drain)

    def _build(self):
        pad = {"padx": 8, "pady": 4}
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Source").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(frame, textvariable=self.source).grid(row=0, column=1, sticky="ew", **pad)
        buttons = ttk.Frame(frame)
        buttons.grid(row=0, column=2, sticky="e", **pad)
        ttk.Button(buttons, text="Zip...", command=self._pick_zip).pack(side="left")
        ttk.Button(buttons, text="Folder...", command=self._pick_folder).pack(side="left")
        ttk.Button(buttons, text="File...", command=self._pick_file).pack(side="left")

        ttk.Label(frame, text="Output").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(frame, textvariable=self.output).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(frame, text="Browse...", command=self._pick_output).grid(row=1, column=2, sticky="e", **pad)

        actions = ttk.Frame(frame)
        actions.grid(row=2, column=0, columnspan=3, sticky="w", **pad)
        self.convert_btn = ttk.Button(actions, text="Convert", command=self._start)
        self.convert_btn.pack(side="left")
        ttk.Button(actions, text="Open output folder", command=self._open_output).pack(side="left", padx=8)
        ttk.Button(actions, text="About", command=self._about).pack(side="left")

        self.log = scrolledtext.ScrolledText(frame, height=16, state="disabled", wrap="word")
        self.log.grid(row=3, column=0, columnspan=3, sticky="nsew", **pad)
        frame.rowconfigure(3, weight=1)

        ttk.Label(frame, textvariable=self.status, anchor="w").grid(row=4, column=0, columnspan=3, sticky="ew", **pad)
        self._log_line("ffmpeg: %s" % (core.ffmpeg_path() or "NOT FOUND - conversion will fail"))
        self._log_line("7-Zip: %s" % (core.sevenzip_path() or "not found (only needed for cut-off zips)"))

    # --- pickers ---
    def _set_source(self, path):
        if path:
            self.source.set(path)
            if not self.output.get():
                self.output.set(core.default_output(path))

    def _pick_zip(self):
        self._set_source(filedialog.askopenfilename(title="Zip export", filetypes=[("Zip", "*.zip")]))

    def _pick_folder(self):
        self._set_source(filedialog.askdirectory(title="Folder with .g64 files"))

    def _pick_file(self):
        self._set_source(filedialog.askopenfilename(title="G64 file", filetypes=[("G64", "*.g64"), ("All", "*.*")]))

    def _pick_output(self):
        path = filedialog.askdirectory(title="Output folder")
        if path:
            self.output.set(path)

    def _open_output(self):
        path = self.output.get()
        if not path or not os.path.isdir(path):
            self.status.set("Output folder does not exist yet.")
            return
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: attribute exists on Windows only
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    # --- about ---
    def _about(self):
        win = tk.Toplevel(self)
        win.title("About")
        win.resizable(False, False)
        win.transient(self)
        box = ttk.Frame(win, padding=16)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text="G64 to MP4 converter %s" % core.__version__,
                  font=("TkDefaultFont", 12, "bold")).pack(anchor="w")
        ttk.Label(box, text="Converts Genetec G64 video exports to MP4 without re-encoding.").pack(anchor="w", pady=(2, 10))
        ttk.Label(box, text="Created by %s" % AUTHOR).pack(anchor="w")
        self._link(box, "GitHub profile: " + AUTHOR_GITHUB, AUTHOR_GITHUB)
        self._link(box, "Project: " + PROJECT_URL, PROJECT_URL)
        ttk.Button(box, text="Close", command=win.destroy).pack(anchor="e", pady=(12, 0))
        win.grab_set()

    def _link(self, parent, text, url):
        label = tk.Label(parent, text=text, fg="#1a5fb4", cursor="hand2")
        label.pack(anchor="w")
        label.bind("<Button-1>", lambda _e, u=url: webbrowser.open(u))

    # --- conversion ---
    def _start(self):
        src = self.source.get().strip()
        if not src or not os.path.exists(src):
            self.status.set("Choose an existing source first.")
            return
        out = self.output.get().strip() or core.default_output(src)
        self.output.set(out)
        self.convert_btn.config(state="disabled")
        self.status.set("Converting...")
        core.log = self.queue.put  # route core messages into the window
        self.worker = threading.Thread(target=self._work, args=(src, out), daemon=True)
        self.worker.start()

    def _work(self, src, out):
        try:
            summary = core.run([src], out)
            self.queue.put(("__done__", summary))
        except Exception as e:
            self.queue.put("ERROR: %s" % e)
            self.queue.put(("__done__", None))

    def _drain(self):
        try:
            while True:
                item = self.queue.get_nowait()
                if isinstance(item, tuple) and item[0] == "__done__":
                    summary = item[1]
                    self.convert_btn.config(state="normal")
                    if summary is None:
                        self.status.set("Failed.")
                    else:
                        self.status.set("Done: %d converted, %d skipped, %d failed."
                                        % (summary.converted, summary.skipped, summary.failed))
                else:
                    self._log_line(str(item))
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _log_line(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")


def main():
    App().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
