from __future__ import annotations

from pathlib import Path
import sys

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from traceable_rag.runtime_config import RuntimeConfig
from traceable_rag.web_app import create_app


RUNTIME_ROOT = ROOT / ".artifacts" / "e2e-runtime-v3"


def build_app():
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    runtime = RuntimeConfig(
        mineru_token=None,
        embedding_base_url=None,
        embedding_api_key=None,
        embedding_model=None,
        embedding_query_prompt=None,
        reranker_model=None,
        database_path=RUNTIME_ROOT / "traceable-rag.db",
        managed_storage_root=RUNTIME_ROOT / "managed",
        settings_path=RUNTIME_ROOT / "settings.json",
        static_dir=ROOT / "web" / "dist",
    )
    return create_app(runtime_config=runtime)


if __name__ == "__main__":
    uvicorn.run(build_app(), host="127.0.0.1", port=8000)
