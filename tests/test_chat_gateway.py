from app.services.chat_gateway import extract_stream_events, normalize_chat_error


def test_extract_stream_events_yields_deltas_and_finish_reason():
    raw_lines = [
        'data: {"choices":[{"delta":{"content":"你"}}]}\n'.encode("utf-8"),
        'data: {"choices":[{"delta":{"content":"好"},"finish_reason":"stop"}]}\n'.encode("utf-8"),
        b'data: [DONE]\n',
    ]

    events = list(extract_stream_events(raw_lines))

    assert events == [
        {"type": "delta", "text": "你"},
        {"type": "delta", "text": "好"},
        {"type": "finish", "finish_reason": "stop"},
    ]


def test_normalize_chat_error_includes_source():
    result = normalize_chat_error("provider", RuntimeError("boom"))
    assert result == "provider: RuntimeError: boom"
