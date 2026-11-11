"""一次性生成「功能需求清单」静态文件 catalog.json。

说明（重要）：
  * 本脚本**只在维护时手动运行**，用来把 doc.geweapi.com 公开文档里的
    「功能名称」抄成一份需求对照表。生成的 catalog.json 是纯静态数据。
  * 本项目运行时**不访问该网站、不调用任何外部 API、不需要任何 Token**，
    所有功能都由本机微信客户端的本地桥接实现。
  * 若不想联网，直接手工编辑 catalog.json 亦可，格式见文件内 "items"。

用法： python local_wechat/tools/build_catalog.py
"""
from __future__ import annotations

import json
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

INDEX_URL = "https://doc.geweapi.com/llms.txt"
OUT = Path(__file__).resolve().parent.parent / "catalog.json"


def main() -> int:
    print("抓取功能名称清单（仅名称，用于需求对照）...")
    req = urllib.request.Request(INDEX_URL, headers={"User-Agent": "Mozilla/5.0"})
    text = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")
    items = []
    for line in text.splitlines():
        m = re.search(
            r"-\s*API 参考\s*>\s*(.+?)\s*\[([^\]]+)\]\(https://doc\.geweapi\.com/(api-\d+)\.md\)",
            line,
        )
        if not m:
            continue
        parts = [p.strip() for p in m.group(1).split(">") if p.strip()]
        module = parts[0] if parts else "其他"
        submodule = parts[1] if len(parts) > 1 else ""
        name = m.group(2).strip()
        doc_id = m.group(3)
        # 纯说明页没有对应功能
        if "代理说明" in name:
            continue
        # 用文档 ID 作为稳定主键；功能名里可能重复
        items.append({
            "id": doc_id,
            "name": name,
            "module": module,
            "submodule": submodule,
            "doc_id": doc_id,
        })
    payload = {
        "_comment": "静态需求对照表：只是功能名称清单，运行时不联网、不需要 Token。可手工编辑。",
        "source": INDEX_URL,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(items),
        "items": items,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已写入 %s，共 %d 项功能名称" % (OUT, len(items)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
