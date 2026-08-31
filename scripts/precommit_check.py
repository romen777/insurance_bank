# -*- coding: utf-8 -*-
"""커밋 전 점검 — 이 저장소는 공개(public)다.

공개 저장소라 한 번 올라간 것은 지워도 이력에 남는다. 그래서 올리기 전에 본다.
심평원이 공개 배포하는 자료만 두고, 개인정보·자격증명·비공개 자산은 두지 않는다.

usage
   python scripts/precommit_check.py          추적 중인 파일 + 새로 담긴 것
   python scripts/precommit_check.py --all    작업 폴더 전부 (무시된 것 빼고)
"""
import argparse
import io
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SECRETS = [
    ("Supabase service 키", re.compile(r"sb_secret_[A-Za-z0-9_\-]{10,}")),
    ("Supabase anon 키", re.compile(r"sb_publishable_[A-Za-z0-9_\-]{10,}")),
    ("JWT 토큰", re.compile(r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}")),
    ("Google API 키", re.compile(r"AIza[0-9A-Za-z_\-]{30,}")),
    ("OpenAI 키", re.compile(r"\bsk-[A-Za-z0-9]{32,}")),
    ("Anthropic 키", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("Firebase DB 시크릿", re.compile(r"FIREBASE_DB_SECRET\s*=\s*\S+")),
]

PII = [
    ("주민등록번호", re.compile(r"\b\d{6}\s*-\s*[1-4]\d{6}\b")),
    ("휴대전화", re.compile(r"\b01[016-9]-?\d{3,4}-?\d{4}\b")),
    ("환자번호 칸", re.compile(r"(환자번호|수진자번호|증번호)\s*[:=]\s*\d{4,}")),
]

# 비공개 자산이 흘러든 흔적. 이름만 걸러도 대부분 막힌다.
PRIVATE = [
    ("골키퍼 룰 내용", re.compile(r"\bGKR_\d{5}\b|gk_(rule|target|dm_combo|corx_rule|dup_rule)\b")),
    ("이지스 운영DB 표", re.compile(r"\bh[0-9]_mst_|\bhz_mst_|\bh4check_|\bs_opinion\b", re.I)),
    ("당뇨 길라잡이 조각", re.compile(r"\bguide-p\d{3}\b|gk_ref_chunk\b")),
]

SKIP_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".zip", ".xlsx", ".hwp", ".hwpx"}


def tracked(all_files):
    if all_files:
        out = subprocess.check_output(["git", "ls-files", "-co", "--exclude-standard"],
                                      cwd=str(ROOT), text=True, encoding="utf-8")
    else:
        out = subprocess.check_output(["git", "ls-files", "-c", "-o", "--exclude-standard"],
                                      cwd=str(ROOT), text=True, encoding="utf-8")
    return [l.strip() for l in out.split("\n") if l.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()

    files = tracked(a.all)
    checked = 0
    findings = []
    for rel in files:
        p = ROOT / rel
        if not p.exists() or p.suffix.lower() in SKIP_EXT:
            continue
        try:
            s = io.open(p, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        checked += 1
        for group, rules in (("자격증명", SECRETS), ("개인정보", PII), ("비공개 자산", PRIVATE)):
            for name, pat in rules:
                m = pat.search(s)
                if m:
                    line = s[:m.start()].count("\n") + 1
                    findings.append((group, name, rel, line, m.group(0)[:28]))

    if (ROOT / ".env") in [ROOT / f for f in files]:
        findings.append(("자격증명", ".env 가 담겼다", ".env", 0, ""))

    print("점검 %d개 파일" % checked)
    if not findings:
        print("✓ 걸린 것 없음 — 올려도 됩니다.")
        return 0
    print("🔴 %d건 — 올리기 전에 확인하십시오\n" % len(findings))
    for g, n, rel, ln, sample in findings:
        print("  [%s] %s" % (g, n))
        print("      %s:%d  %s…" % (rel, ln, sample))
    print("\n이 저장소는 공개입니다. 한 번 올라가면 지워도 이력에 남습니다.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
