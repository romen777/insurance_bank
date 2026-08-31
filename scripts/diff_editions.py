# -*- coding: utf-8 -*-
"""판 대조 — 무엇이 신설·개정·폐기됐나.

새 판을 DB 에 밀어 넣기 **전에** 눈으로 확인하는 자리다.
id 가 위치가 아니라 내용으로 짜여 있어(항목코드+제목+장) 판이 바뀌어도 같은 조각은
같은 id 를 갖는다. 그래서 id 만 맞대 보면 신설·폐기가 그대로 나온다.

usage
   python scripts/diff_editions.py --against ref-2026-07
   python scripts/diff_editions.py --against ref-2026-07 --full     본문 앞뒤까지
   python scripts/diff_editions.py --old data/chunks.2026-07.jsonl  파일끼리
"""
import argparse
import difflib
import io
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JSONL = ROOT / "data" / "chunks.jsonl"
REL = "data/chunks.jsonl"


def load_lines(lines):
    out = {}
    for l in lines:
        l = l.strip()
        if not l:
            continue
        c = json.loads(l)
        out[c["id"]] = c
    return out


def from_git(ref):
    try:
        raw = subprocess.check_output(["git", "show", "%s:%s" % (ref, REL)],
                                      cwd=str(ROOT), stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        sys.exit("git show %s:%s 실패 — 태그가 있습니까?\n%s"
                 % (ref, REL, e.stderr.decode("utf-8", "replace")[:300]))
    return load_lines(raw.decode("utf-8").splitlines())


def label(c):
    t = (c.get("제목") or c.get("항목") or c.get("본문") or "")[:52]
    return "%-9s %s" % (c.get("항목코드") or "-", t.replace("\n", " "))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--against", help="견줄 git 태그·커밋 (예: ref-2026-07)")
    ap.add_argument("--old", help="견줄 jsonl 파일")
    ap.add_argument("--full", action="store_true", help="개정된 조각의 본문 차이까지")
    a = ap.parse_args()
    if not a.against and not a.old:
        sys.exit("--against 태그 또는 --old 파일 중 하나가 필요합니다")

    old = from_git(a.against) if a.against else load_lines(io.open(a.old, encoding="utf-8"))
    new = load_lines(io.open(JSONL, encoding="utf-8"))

    added = [k for k in new if k not in old]
    gone = [k for k in old if k not in new]
    changed = [k for k in new if k in old and new[k]["본문"] != old[k]["본문"]]
    same = len(new) - len(added) - len(changed)

    print("옛 판 %d조각 → 새 판 %d조각" % (len(old), len(new)))
    print("  그대로 %d · 신설 %d · 개정 %d · 폐기 %d" % (same, len(added), len(changed), len(gone)))

    if gone:
        print("\n── 폐기 (새 판에 없다. 인용해서는 안 된다) ──")
        for k in gone[:40]:
            print("  - %s  %s" % (k, label(old[k])))
        if len(gone) > 40:
            print("    … 외 %d개" % (len(gone) - 40))

    if added:
        print("\n── 신설 ──")
        for k in added[:40]:
            print("  + %s  %s" % (k, label(new[k])))
        if len(added) > 40:
            print("    … 외 %d개" % (len(added) - 40))

    if changed:
        print("\n── 개정 ──")
        for k in changed[:40]:
            o, n = old[k], new[k]
            print("  ~ %s  %s" % (k, label(n)))
            if o.get("고시번호") != n.get("고시번호"):
                print("      근거 %s → %s" % (o.get("고시번호"), n.get("고시번호")))
            if a.full:
                for line in list(difflib.unified_diff(
                        o["본문"].split(". "), n["본문"].split(". "),
                        lineterm="", n=0))[2:12]:
                    print("      %s" % line[:150])
        if len(changed) > 40:
            print("    … 외 %d개" % (len(changed) - 40))

    if not (added or gone or changed):
        print("\n바뀐 것이 없습니다.")


if __name__ == "__main__":
    main()
