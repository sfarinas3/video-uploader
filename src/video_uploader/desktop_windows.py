from __future__ import annotations

# Tracks the app's pywebview windows so server-side code (route handlers,
# running on a background thread -- see web/app.py's main()) can open/close
# a second native window for OAuth connect flows, instead of navigating the
# main window away to an external site. pywebview supports creating and
# controlling windows from a non-GUI thread after webview.start() has begun
# the GUI loop on the main thread; that's the intended usage here.

_main_window = None
_popup_window = None


def set_main_window(window) -> None:
    global _main_window
    _main_window = window


def open_popup(title: str, url: str, width: int = 500, height: int = 720) -> None:
    import webview

    global _popup_window
    close_popup()
    _popup_window = webview.create_window(title, url, width=width, height=height)


def close_popup() -> None:
    global _popup_window
    if _popup_window is not None:
        try:
            _popup_window.destroy()
        except Exception:  # noqa: BLE001 - best-effort cleanup, never fail the caller
            pass
        _popup_window = None


def refresh_main_window(url: str) -> None:
    if _main_window is not None:
        try:
            _main_window.load_url(url)
        except Exception:  # noqa: BLE001 - best-effort; the user can still refresh manually
            pass
