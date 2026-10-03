from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.core.settings import Settings
from app.repositories.evidence import EvidenceBundleError, EvidenceBundleRepository


def main() -> None:
    parser = argparse.ArgumentParser(description="计算哈希、校验并追加导入点时证据包")
    parser.add_argument("file", type=Path, help="待导入的证据包草稿 JSON")
    args = parser.parse_args()
    try:
        payload = json.loads(args.file.read_text("utf-8"))
        if not isinstance(payload, dict):
            raise EvidenceBundleError("证据包根节点必须是对象")
        target = EvidenceBundleRepository(Settings()).import_draft(payload)
    except (OSError, json.JSONDecodeError, EvidenceBundleError) as exc:
        parser.error(str(exc))
    print(target)


if __name__ == "__main__":
    main()
