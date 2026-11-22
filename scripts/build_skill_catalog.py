from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REF_DIR = ROOT / "skills" / "ifind-finance-data" / "references"
OUT = ROOT / "app" / "tools" / "skill_catalog.json"

DOMAINS = {
    "stock": ("A股", ["cn_stock.md"]),
    "global": ("港美股", ["global_stock.md"]),
    "index": ("指数与板块", ["index.md"]),
    "macro": ("宏观与行业", ["edb.md"]),
    "news": ("新闻与公告", ["news_notices.md"]),
}

EXAMPLE_RE = re.compile(r'"([^"]+)"')


def parse_table(path: Path) -> dict[str, dict[str, str]]:
    tools: dict[str, dict[str, str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text.startswith("|"):
            continue
        cells = [cell.strip() for cell in text.strip("|").split("|")]
        if len(cells) < 3:
            continue
        name = cells[0].replace("`", "").strip()
        if not name or name == "工具名称" or set(name) <= {"-"}:
            continue
        tail = cells[2].rsplit("如", 1)[-1]
        examples = [item for item in EXAMPLE_RE.findall(tail) if re.search(r"[\u4e00-\u9fa5]", item)]
        tools[name] = {
            "desc": cells[1],
            "example": examples[0] if examples else "",
        }
    return tools


def main() -> None:
    catalog = {
        "version": "",
        "generated_at": date.today().isoformat(),
        "domains": {},
    }
    skill_md = (ROOT / "skills" / "ifind-finance-data" / "SKILL.md").read_text(encoding="utf-8")
    version = re.search(r'version:\s*"([^"]+)"', skill_md)
    catalog["version"] = version.group(1) if version else ""
    for key, (label, files) in DOMAINS.items():
        tools: dict[str, dict[str, str]] = {}
        for name in files:
            tools.update(parse_table(REF_DIR / name))
        catalog["domains"][key] = {"label": label, "tools": tools}
    OUT.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    total = sum(len(item["tools"]) for item in catalog["domains"].values())
    print(f"wrote {OUT} domains={len(catalog['domains'])} tools={total}")


if __name__ == "__main__":
    main()
