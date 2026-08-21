from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from traceable_rag.openapi import build_openapi_spec  # noqa: E402


def main() -> None:
    output = ROOT / "docs" / "openapi.json"
    output.write_text(
        json.dumps(
            build_openapi_spec(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
