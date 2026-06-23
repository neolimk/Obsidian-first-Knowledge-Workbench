import json
import ssl
import urllib.error
import urllib.request

from app.config import AppConfig


class GatewayError(RuntimeError):
    pass


def _build_ssl_context(config: AppConfig):
    if config.verify_tls:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def request_json(
    config: AppConfig,
    url: str,
    method: str = "GET",
    headers=None,
    body=None,
    timeout: int = 15,
):
    req = urllib.request.Request(
        url,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers=headers or {},
        method=method,
    )
    try:
        with urllib.request.urlopen(
            req,
            timeout=timeout,
            context=_build_ssl_context(config),
        ) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GatewayError("HTTP {}: {}".format(exc.code, detail[:300]))
