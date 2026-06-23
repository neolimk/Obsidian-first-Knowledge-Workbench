import json
import ssl
import urllib.request

from app.config import build_config
from app.services.sse import (
    build_delta_event,
    build_done_event,
    build_error_event,
    build_log_event,
    build_start_event,
)


def extract_stream_events(raw_lines):
    for raw_line in raw_lines:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except Exception:
            continue
        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {}).get("content")
            if delta:
                yield {"type": "delta", "text": delta}
            finish_reason = choice.get("finish_reason")
            if finish_reason:
                yield {"type": "finish", "finish_reason": finish_reason}


def normalize_chat_error(source, error):
    return "{}: {}: {}".format(source, type(error).__name__, error)


def _ssl_context(config):
    if config.verify_tls:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def stream_via_request(url, body, headers, timeout):
    config = build_config()
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl_context(config))


def _provider_url(base):
    lowered = base.lower()
    if base.endswith(("/v1", "/v4", "/v2")) or "bigmodel" in lowered or "zhipu" in lowered:
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def _collect_events_from_response(resp, events, text_parts):
    finish_reason = None
    for event in extract_stream_events(resp):
        if event["type"] == "delta":
            text_parts.append(event["text"])
            events.append(build_delta_event(event["text"]))
        elif event["type"] == "finish":
            finish_reason = event["finish_reason"]
    return finish_reason


def stream_chat_with_fallback(body, route, litellm_base, litellm_master_key):
    events = []
    full_text_parts = []
    finish_reason = None
    provider_used = "litellm"
    direct_err = None

    if route.get("source") and route.get("source") != "litellm" and route.get("baseUrl"):
        provider_used = route.get("source")
        try:
            provider_resp = stream_via_request(
                _provider_url(route["baseUrl"].rstrip("/")),
                body,
                {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer {}".format(route.get("apiKey") or ""),
                },
                180,
            )
            finish_reason = _collect_events_from_response(provider_resp, events, full_text_parts)
            events.append(build_done_event())
            return {
                "events": events,
                "full_text": "".join(full_text_parts),
                "finish_reason": finish_reason,
                "provider": provider_used,
                "direct_err": None,
            }
        except Exception as error:
            direct_err = normalize_chat_error(provider_used, error)
            events.append(build_log_event(direct_err))
            full_text_parts = []
            finish_reason = None
            provider_used = "litellm_fallback"

    try:
        litellm_resp = stream_via_request(
            litellm_base + "/v1/chat/completions",
            body,
            {
                "Content-Type": "application/json",
                "Authorization": "Bearer {}".format(litellm_master_key),
                "Accept": "text/event-stream",
            },
            180,
        )
        if not events:
            events.append(build_start_event())
        finish_reason = _collect_events_from_response(litellm_resp, events, full_text_parts)
        events.append(build_done_event())
        return {
            "events": events,
            "full_text": "".join(full_text_parts),
            "finish_reason": finish_reason,
            "provider": provider_used,
            "direct_err": direct_err,
        }
    except Exception as error:
        message = normalize_chat_error(provider_used, error)
        events.append(build_error_event(message, None))
        events.append(build_done_event())
        return {
            "events": events,
            "full_text": "",
            "finish_reason": None,
            "provider": provider_used,
            "direct_err": direct_err,
        }
