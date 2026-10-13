"""A small always-on "rpmap is running" window (Tkinter).

After a self-update the AppImage relaunches *detached*, and a peer server can run
with no terminal at all - so there is no obvious sign that rpmap is up. This is
that sign: a tiny window that shows the current slot and offers Open UI /
Restore stock / Quit.

It is best-effort and purely additive:

* It is **never required**. If Tk or a display is missing, :func:`available`
  returns False and the caller keeps the plain headless server loop. So a
  source checkout on a machine without ``tkinter`` still works.
* **Packaged builds bundle Tcl/Tk** via PyInstaller, so this also works on
  systems whose system Python has no ``tkinter`` (e.g. Bazzite's ``python3``).

Tk must own the process's main thread, so :func:`run` blocks there; the HTTP
server runs in a background thread that the caller starts, and the window polls
``quit_event`` to close itself when asked (Quit button, ``/api/quit``, or a
signal).
"""

from __future__ import annotations

import os

# Background/foreground colours, matched to the web UI.
_BG = "#161b22"
_FG = "#c9d1d9"
_MUTED = "#8b949e"
_WHITE = "#f0f6fc"
_ACCENT = "#2ea043"


def available() -> bool:
    """Can we actually show a window here?

    Validates the real failure modes up front (no ``tkinter``, no X display, a
    broken Tcl/Tk install) by building *and destroying* a withdrawn root, which
    never maps a window on screen. When this returns True, :func:`run` is safe.
    """
    if os.name != "nt" and not os.environ.get("DISPLAY"):
        return False          # Tk is X11; Wayland needs XWayland (DISPLAY set)
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()       # created but never mapped -> no flash
        root.destroy()
    except Exception:
        return False
    return True


def run(*, version: str, status_fn, on_open, on_restore, on_quit,
        quit_event) -> None:
    """Show the status window until it is asked to quit. Blocks (Tk mainloop).

    ``status_fn`` returns ``(arena, what[, where])`` for the current slot, where
    the optional ``where`` names the connection mode shown as a hint; ``on_quit``
    must stop the server *and* set ``quit_event``. ``on_open``/``on_restore``
    are the button actions.
    """
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.title(f"rpmap {version}")
    root.resizable(False, False)
    root.configure(bg=_BG)

    def label(text, size=10, weight="normal", colour=_FG, wrap=0):
        return tk.Label(root, text=text, bg=_BG, fg=colour, justify="left",
                        anchor="w", wraplength=wrap,
                        font=("TkDefaultFont", size, weight))

    head = tk.Frame(root, bg=_BG)
    head.pack(fill="x", padx=16, pady=(14, 2))
    tk.Label(head, text="\u25cf", bg=_BG, fg=_ACCENT,
             font=("TkDefaultFont", 16)).pack(side="left")
    tk.Label(head, text=" rpmap is running", bg=_BG, fg=_WHITE,
             font=("TkDefaultFont", 14, "bold")).pack(side="left")

    status = label("...", wrap=360)
    status.pack(fill="x", padx=16, pady=(6, 2))
    hint = label("", colour=_MUTED, wrap=360)
    hint.pack(fill="x", padx=16, pady=(0, 10))

    def refresh():
        try:
            res = status_fn()
            arena, what = res[0], res[1]
            where = res[2] if len(res) > 2 else None
            status.config(text=f"{arena}  \u2014  {what}")
            prefix = f"Connection: {where}.  " if where else ""
            hint.config(text=f'{prefix}In game: Free Play \u2192 arena "{arena}".')
        except Exception:
            status.config(text="(could not read the slot)")
            hint.config(text="")

    def do_restore():
        if messagebox.askyesno("rpmap", "Restore the stock map?\n\n"
                               "Do this before playing online."):
            on_restore()
            refresh()

    buttons = tk.Frame(root, bg=_BG)
    buttons.pack(fill="x", padx=16, pady=(0, 8))
    tk.Button(buttons, text="Open UI", command=on_open).pack(side="left")
    tk.Button(buttons, text="Restore stock", command=do_restore).pack(side="left", padx=6)
    tk.Button(buttons, text="Quit", command=on_quit).pack(side="right")

    topmost = tk.BooleanVar(value=False)
    tk.Checkbutton(
        root, text="Keep on top", variable=topmost,
        command=lambda: root.attributes("-topmost", bool(topmost.get())),
        bg=_BG, fg=_FG, activebackground=_BG, activeforeground=_FG,
        selectcolor=_BG, highlightthickness=0, bd=0,
    ).pack(anchor="w", padx=12, pady=(0, 12))

    def poll():
        if quit_event.is_set():
            root.destroy()
            return
        refresh()
        root.after(1000, poll)

    def on_close():
        if messagebox.askyesno("rpmap", "Quit rpmap?\n\nThe map server will stop."):
            on_quit()

    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh()
    root.after(300, poll)
    root.mainloop()
