from app.services.chat import build_sse_event


def test_build_sse_event_encodes_json_line():
    line = build_sse_event({"event": "delta", "text": "你好"})
    assert line == 'data: {"event": "delta", "text": "你好"}\n\n'.encode("utf-8")
