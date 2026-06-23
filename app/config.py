import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass(frozen=True)
class AppConfig:
    base_dir: Path
    data_dir: Path
    db_path: Path
    static_dir: Path
    index_html: Path
    litellm_base: str
    litellm_master_key: str
    verify_tls: bool
    cors_allowed_origins: List[str]
    port: int


def _parse_origins(raw: Optional[str]) -> List[str]:
    if not raw:
        return ["http://127.0.0.1:8899"]
    return [item.strip() for item in raw.split(",") if item.strip()]


def build_config() -> AppConfig:
    default_base_dir = Path(__file__).resolve().parent.parent
    base_dir = Path(os.environ.get("OPENWEBUI_LITE_BASE_DIR", default_base_dir))
    master_key = os.environ.get("OPENWEBUI_LITE_MASTER_KEY")
    if not master_key:
        raise RuntimeError("OPENWEBUI_LITE_MASTER_KEY is required")

    data_dir = base_dir / "data"
    static_dir = base_dir / "static"
    data_dir.mkdir(parents=True, exist_ok=True)

    return AppConfig(
        base_dir=base_dir,
        data_dir=data_dir,
        db_path=data_dir / "openwebui-lite.db",
        static_dir=static_dir,
        index_html=static_dir / "index.html",
        litellm_base=os.environ.get("OPENWEBUI_LITE_BASE", "http://127.0.0.1:4000"),
        litellm_master_key=master_key,
        verify_tls=os.environ.get("OPENWEBUI_LITE_VERIFY_TLS", "true").lower() != "false",
        cors_allowed_origins=_parse_origins(os.environ.get("OPENWEBUI_LITE_CORS_ORIGINS")),
        port=int(os.environ.get("OPENWEBUI_LITE_PORT", "8899")),
    )
