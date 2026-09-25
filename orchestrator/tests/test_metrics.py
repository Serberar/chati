import metrics


def test_log_event_and_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "LOG_PATH", tmp_path / "metrics_test.jsonl")

    metrics.log_event("text", 120.0, verifier_gated=False)
    metrics.log_event("text", 80.0, verifier_gated=True)
    metrics.log_event("code", 200.0, verifier_gated=False)
    metrics.log_event("image", 5000.0, verifier_gated=False, error="fallo de prueba")

    summary = metrics.summary()

    assert summary["total_requests"] == 4
    assert summary["verifier_gated"] == 1
    assert summary["errors"] == 1

    assert summary["by_agent"]["text"]["count"] == 2
    assert summary["by_agent"]["text"]["avg_latency_ms"] == 100.0
    assert summary["by_agent"]["text"]["gated"] == 1

    assert summary["by_agent"]["image"]["errors"] == 1


def test_summary_empty_when_no_events(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "LOG_PATH", tmp_path / "does_not_exist.jsonl")
    summary = metrics.summary()
    assert summary["total_requests"] == 0
    assert summary["by_agent"] == {}


def test_summary_respects_last_n_window(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "LOG_PATH", tmp_path / "metrics_window.jsonl")
    for _ in range(10):
        metrics.log_event("text", 100.0)

    summary = metrics.summary(last_n=3)
    assert summary["total_requests"] == 3
