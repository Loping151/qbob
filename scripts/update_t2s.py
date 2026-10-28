#!/usr/bin/env python3
"""从 OpenCC 的 TSPhrases.txt + TSCharacters.txt 重新生成 core/t2s.py(繁体 -> 简体).

刻意不做运行时拉取, 需要时手动跑。与 OpenCC 的 t2s 同一做法:
- 先按词语最长匹配, 再逐字; 多个候选都取第一个;
- 词语只收逐字转会转错的(OpenCC 的例外词: 特徵->特征 靠逐字也行, 乾隆/哪吒 要靠它保住);
- 目标在 BMP 之外(扩展区, 多数客户端没字形)的字和词不收.
    python scripts/update_t2s.py [--check]   # --check: 有更新时以 1 退出
"""
from __future__ import annotations

import argparse
import urllib.request
from pathlib import Path

SOURCE = "https://raw.githubusercontent.com/BYVoid/OpenCC/master/data/dictionary/"
TARGET = Path(__file__).resolve().parent.parent / "qqbot_onebot/core/t2s.py"
CHUNK = 40

HEADER = '''"""繁体 -> 简体: 词语最长匹配 + 逐字(自动生成, 勿手改).

数据取自 OpenCC 的 data/dictionary/TSPhrases.txt 与 TSCharacters.txt
(https://github.com/BYVoid/OpenCC, Copyright BYVoid 与 OpenCC 贡献者, Apache-2.0).
取舍见 scripts/update_t2s.py.

更新: python scripts/update_t2s.py
"""

import re

'''

FOOTER = '''
TABLE = str.maketrans(TRAD, SIMP)
# 长的在前: 正则在同一位置取第一个匹配的分支, 即最长匹配
_PHRASE_RE = re.compile("|".join(map(re.escape, sorted(PHRASES, key=len, reverse=True))))


def to_simplified(text: str) -> str:
    out, pos = [], 0
    for match in _PHRASE_RE.finditer(text):
        out.append(text[pos:match.start()].translate(TABLE))
        out.append(PHRASES[match.group(0)])
        pos = match.end()
    out.append(text[pos:].translate(TABLE))
    return "".join(out)
'''


def fetch(name: str) -> str:
    with urllib.request.urlopen(SOURCE + name, timeout=30) as resp:  # noqa: S310 固定域名
        return resp.read().decode("utf-8")


def entries(raw: str):
    """(繁体, 第一个候选); 第一个候选在扩展区的跳过."""
    for line in raw.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        trad, _, values = line.partition("\t")
        candidates = values.split()
        if candidates and all(ord(ch) <= 0xFFFF for ch in candidates[0]):
            yield trad, candidates[0]


def build(chars_raw: str, phrases_raw: str) -> tuple[dict[str, str], dict[str, str]]:
    chars = {t: s for t, s in entries(chars_raw) if len(t) == 1 and len(s) == 1 and t != s}
    table = str.maketrans(chars)
    phrases = {t: s for t, s in entries(phrases_raw) if t.translate(table) != s}
    return dict(sorted(chars.items())), dict(sorted(phrases.items()))


def literal(name: str, text: str) -> str:
    rows = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)]
    return f"{name} = (\n" + "".join(f'    "{row}"\n' for row in rows) + ")\n"


def phrase_block(phrases: dict[str, str]) -> str:
    lines = [f"# 逐字转会转错的词({len(phrases)} 个)", "PHRASES = {"]
    row = "   "
    for key, value in phrases.items():
        piece = f' "{key}": "{value}",'
        if len(row) + len(piece) > 86:
            lines.append(row)
            row = "   "
        row += piece
    if row.strip():
        lines.append(row)
    return "\n".join(lines + ["}"]) + "\n"


def render(chars: dict[str, str], phrases: dict[str, str]) -> str:
    return (HEADER + f"# {len(chars)} 字, TRAD 与 SIMP 逐字对应\n"
            + literal("TRAD", "".join(chars)) + literal("SIMP", "".join(chars.values()))
            + "\n" + phrase_block(phrases) + FOOTER)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="只比对, 有更新时以 1 退出(给 CI 用)")
    args = parser.parse_args()

    fresh = render(*build(fetch("TSCharacters.txt"), fetch("TSPhrases.txt")))
    old = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
    if fresh == old:
        print("表已是最新")
        return 0
    if args.check:
        print("OpenCC 表有更新, 跑一次 python scripts/update_t2s.py")
        return 1
    TARGET.write_text(fresh, encoding="utf-8")
    print(f"已更新 {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
