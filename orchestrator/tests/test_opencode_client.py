from unittest.mock import MagicMock, patch

import requests

import opencode_client

BASE = "http://127.0.0.1:8901"
WEB = "http://127.0.0.1:8901"


def _ok_response(json_data):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = json_data
    return resp


@patch("opencode_client.requests.post")
def test_create_session_returns_id(mock_post):
    mock_post.return_value = _ok_response({"id": "ses_abc123"})

    session_id = opencode_client.create_session(BASE)

    assert session_id == "ses_abc123"
    mock_post.assert_called_once_with(f"{BASE}/session", json={}, timeout=opencode_client.TIMEOUT)


@patch("opencode_client.requests.post")
def test_send_prompt_async_posts_text_part(mock_post):
    mock_post.return_value = _ok_response({})

    opencode_client.send_prompt_async(BASE, "ses_abc123", "arregla el bug X")

    mock_post.assert_called_once_with(
        f"{BASE}/session/ses_abc123/prompt_async",
        json={"parts": [{"type": "text", "text": "arregla el bug X"}]},
        timeout=opencode_client.TIMEOUT,
    )


@patch("opencode_client.send_prompt_async")
@patch("opencode_client.create_session")
def test_delegate_returns_tracking_link_on_success(mock_create, mock_send):
    mock_create.return_value = "ses_abc123"

    result = opencode_client.delegate(BASE, WEB, "arregla el bug X")

    mock_send.assert_called_once_with(BASE, "ses_abc123", "arregla el bug X")
    assert "ses_abc123" in result
    assert WEB in result


@patch("opencode_client.create_session")
def test_delegate_reports_connection_failure_without_raising(mock_create):
    mock_create.side_effect = requests.ConnectionError("no se pudo conectar")

    result = opencode_client.delegate(BASE, WEB, "arregla el bug X")

    assert "no se pudo contactar" in result.lower()
