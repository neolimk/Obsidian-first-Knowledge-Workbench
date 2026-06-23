from app.services.sse import (
    build_delta_event,
    build_done_event,
    build_error_event,
    build_log_event,
    build_refs_event,
)


def test_build_delta_event():
    expected = 'data: {"event": "delta", "text": "你好"}\n\n'.encode("utf-8")
    assert build_delta_event("你好") == expected


def test_build_done_event():
    assert build_done_event() == b"data: [DONE]\n\n"


def test_build_log_event():
    expected = 'data: {"event": "log", "text": "fallback"}\n\n'.encode("utf-8")
    assert build_log_event("fallback") == expected


def test_build_error_event_with_extra():
    assert build_error_event("boom", {"code": 500}) == (
        'data: {"event": "error", "text": "boom", "code": 500}\n\n'.encode("utf-8")
    )


def test_build_refs_event():
    refs = [{"path": "a.md", "title": "A"}]
    expected = (
        'data: {"event": "refs", "refs": [{"path": "a.md", "title": "A"}], '
        '"hit_count": 1}\n\n'.encode("utf-8")
    )
    assert build_refs_event(refs, 1) == expected
