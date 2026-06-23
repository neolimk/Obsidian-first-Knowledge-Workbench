import json


def build_sse_event(payload):
    return "data: {}\n\n".format(
        json.dumps(payload, ensure_ascii=False)
    ).encode("utf-8")


def build_delta_event(text):
    return build_sse_event({"event": "delta", "text": text})


def build_log_event(text):
    return build_sse_event({"event": "log", "text": text})


def build_error_event(message, extra=None):
    payload = {"event": "error", "text": message}
    if extra:
        payload.update(extra)
    return build_sse_event(payload)


def build_refs_event(refs, hit_count=None):
    payload = {"event": "refs", "refs": refs}
    if hit_count is not None:
        payload["hit_count"] = hit_count
    return build_sse_event(payload)


def build_start_event():
    return build_sse_event({"event": "start"})


def build_done_event():
    return b"data: [DONE]\n\n"
