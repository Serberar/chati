"""Si el motor de Edge (WebView2) no arranca, la ventana se quedaba en negro
para siempre (Windows Sandbox, 2026-09-30). Ahora se detecta y Chati se abre
en el navegador normal."""

import logging
from unittest.mock import MagicMock, patch

import desktop_app


def test_a_webview2_failure_opens_chati_in_the_browser():
    desktop_app._webview_failure.failed.clear()
    logging.getLogger("pywebview").error(
        "WebView2 initialization failed with exception:\n Couldn't find a compatible Webview2 Runtime")
    assert desktop_app._webview_failure.failed.is_set()

    window = MagicMock()
    with patch.object(desktop_app.webbrowser, "open") as mock_open:
        desktop_app._fallback_to_browser(window)
    mock_open.assert_called_once_with(desktop_app.URL)
    window.destroy.assert_called_once()
    desktop_app._webview_failure.failed.clear()


def test_other_pywebview_errors_do_not_trigger_the_fallback():
    desktop_app._webview_failure.failed.clear()
    logging.getLogger("pywebview").error("otro error cualquiera")
    assert not desktop_app._webview_failure.failed.is_set()
