# -*- coding: utf-8 -*-
"""一次性脚本：把指定 keys 文件全量导入 sub2api grok 分组。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from dotenv import load_dotenv

load_dotenv(BASE / ".env", override=True)

from app import import_sso_to_upstream, _read_sso_file_lines  # noqa: E402


def main() -> int:
    key_file = BASE / "keys" / "grok_20260713_142712_100.txt"
    if len(sys.argv) > 1:
        key_file = Path(sys.argv[1])
        if not key_file.is_absolute():
            key_file = BASE / key_file

    if not key_file.is_file():
        print(f"文件不存在: {key_file}", flush=True)
        return 1

    lines = _read_sso_file_lines(key_file)
    print(f"FILE={key_file.name} LINES={len(lines)}", flush=True)
    print("开始导入 sub2api grok 分组（无缓存 token，将走 device flow，可能较久）…", flush=True)

    result = import_sso_to_upstream(sso_lines=lines, merge=True, max_workers=1)

    print("==== RESULT ====", flush=True)
    summary = {
        "ok": result.get("ok"),
        "message": result.get("message"),
        "success": result.get("success"),
        "fail": result.get("fail"),
        "total": result.get("total"),
        "cached_hits": result.get("cached_hits"),
        "flow_hits": result.get("flow_hits"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)

    fails = [r for r in (result.get("results") or []) if r.get("status") != "ok"]
    print(f"FAIL_COUNT={len(fails)}", flush=True)
    for r in fails[:30]:
        print(
            f"  [{r.get('index')}] {r.get('email') or r.get('sso_hint')}: {r.get('error')}",
            flush=True,
        )
    if len(fails) > 30:
        print(f"  ... 另有 {len(fails) - 30} 条失败", flush=True)

    out = BASE / "logs" / f"import_batch_{key_file.stem}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                **summary,
                "file": key_file.name,
                "results": result.get("results"),
                "imported": result.get("imported"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"SAVED={out}", flush=True)
    return 0 if result.get("ok") or (result.get("success") or 0) > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
