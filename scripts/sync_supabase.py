# -*- coding: utf-8 -*-
"""chunks.jsonl -> Supabase gk_ref_bible.

── 왜 git 이 원본이고 DB 는 색인인가
   해마다 판이 바뀌고, 바뀐 기준은 폐기하고 새것으로 인용해야 한다.
   DB 에서 사람이 expire_ymd 를 찍는 방식은 필터를 한 군데만 빠뜨려도
   폐기된 기준이 답변에 인용된다. 그래서 판단을 사람에게 맡기지 않는다.

   이 스크립트는 git 에 있는 것을 참으로 삼는다.
     · jsonl 에 있는 조각  → upsert (use_yn='Y', expire_ymd=null)
     · DB 에만 있는 조각   → use_yn='N' + expire_ymd=오늘  ← 자동 폐기
   지우지 않는다. 「2026년 진료분은 그때 기준으로」를 되짚을 자리가 있어야 한다.

── 자격증명 (.env — 커밋하지 않는다)
   SUPABASE_SIMSA_URL=https://<프로젝트>.supabase.co
   SUPABASE_SECRET_KEY=sb_secret_...        service 키.
     ⚠ anon 키를 넣으면 RLS 에 막혀 **오류가 아니라 0건**이 온다.
        "자료가 아직 없나 보다" 로 보이므로 아래에서 키 종류를 먼저 본다.

usage
   python scripts/sync_supabase.py --edition 2026-07
   python scripts/sync_supabase.py --edition 2026-07 --dry     실제로 쓰지 않고 계획만
"""
import argparse
import datetime as dt
import io
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JSONL = ROOT / "data" / "chunks.jsonl"
TABLE = "gk_ref_bible"
BATCH = 400


# ── 자격증명 ────────────────────────────────────────────────────────────────
# 자격증명을 찾는 자리 — 먼저 찾은 값이 이긴다.
#   ① 이미 잡혀 있는 환경변수
#   ② 이 저장소의 .env (저장소 전용으로 다르게 쓰고 싶을 때)
#   ③ 메디클라우드 로컬 공용 .env — 같은 Supabase 프로젝트라 키를 두 번 적을 이유가 없다
# INSURANCE_BANK_ENV 로 경로를 직접 지정하면 그것을 맨 앞에 둔다.
ENV_FILES = [
    ROOT / ".env",
    Path("C:/mediclaud/.env"),
    Path("C:/mediportal/.env"),
]


def load_env():
    seen = []
    files = ([Path(os.environ["INSURANCE_BANK_ENV"])] if os.environ.get("INSURANCE_BANK_ENV") else []) + ENV_FILES
    for p in files:
        if not p.exists():
            continue
        n = 0
        for line in io.open(p, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if not v or v.startswith("<") or v.endswith("..."):
                continue                     # 채우지 않은 예시 값은 건너뛴다
            if k.strip() not in os.environ:
                os.environ[k.strip()] = v
                n += 1
        if n:
            seen.append("%s(%d)" % (p, n))
    url = os.environ.get("SUPABASE_SIMSA_URL") or os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SECRET_KEY") or os.environ.get("SUPABASE_SIMSA_KEY")
    if not url or not key:
        sys.exit("SUPABASE_SIMSA_URL 과 SUPABASE_SIMSA_KEY(또는 SUPABASE_SECRET_KEY)를 찾지 못했습니다. "
                 "찾아본 곳: " + " · ".join(str(p) for p in files))
    if key.startswith("sb_publishable_") or (key.startswith("eyJ") and "anon" in key):
        sys.exit("anon 키로 보입니다. service 키(sb_secret_)를 쓰십시오 — "
                 "anon 이면 RLS 에 막혀 오류 없이 0건이 옵니다.")
    if seen:
        print("자격증명 — " + " · ".join(seen))
    return url.rstrip("/"), key


def rest(url, key, path, method="GET", body=None, prefer=None):
    req = urllib.request.Request(url + "/rest/v1/" + path, method=method)
    req.add_header("apikey", key)
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    if prefer:
        req.add_header("Prefer", prefer)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    try:
        with urllib.request.urlopen(req, data, timeout=120) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else []
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        sys.exit("Supabase %s %s → HTTP %d\n%s" % (method, path, e.code, detail))


# ── 조각 한 줄 → 표의 한 행 ─────────────────────────────────────────────────
def to_row(c, edition):
    body = c.get("본문") or ""
    noti_no = c.get("고시번호")
    notis = []
    if noti_no:
        notis.append({"no": noti_no, "ymd": c.get("시행일"), "raw": c.get("근거")})
    code = (c.get("항목코드") or "").strip()
    return {
        "chunk_id": c["id"],
        "edition": edition,
        "match_key": c["id"].split("#")[0],
        "division": c.get("부문"),
        "chapter": c.get("장"),
        "item": c.get("항목") or None,
        "title": c.get("제목") or None,
        "headline": c.get("표제") or None,
        "body": body,
        "codes": [code] if code else [],
        "notis": notis,
        "src_note": c.get("근거"),
        "effective_ymd": c.get("시행일"),
        "page_from": c.get("쪽"),
        "page_to": c.get("쪽"),
        "pdf_page": c.get("pdf쪽"),
        "part": c.get("분할"),
        "chars": len(body),
        "has_noti": bool(noti_no),
        "verify": "ok",
        "verify_why": [],
        "use_yn": "Y",
        "expire_ymd": None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--edition", required=True, help="예: 2026-07")
    ap.add_argument("--dry", action="store_true", help="쓰지 않고 계획만 보여 준다")
    a = ap.parse_args()

    url, key = load_env()
    chunks = [json.loads(l) for l in io.open(JSONL, encoding="utf-8") if l.strip()]
    rows = [to_row(c, a.edition) for c in chunks]
    now_ids = set(r["chunk_id"] for r in rows)

    # 지금 DB 에 무엇이 있나 — 폐기 대상을 알아야 한다
    have, off = {}, 0
    while True:
        page = rest(url, key,
                    "%s?select=chunk_id,edition,body,use_yn&limit=1000&offset=%d" % (TABLE, off))
        for r in page:
            have[r["chunk_id"]] = r
        if len(page) < 1000:
            break
        off += 1000

    added = [r for r in rows if r["chunk_id"] not in have]
    changed = [r for r in rows
               if r["chunk_id"] in have and have[r["chunk_id"]]["body"] != r["body"]]
    revived = [r for r in rows
               if r["chunk_id"] in have and have[r["chunk_id"]]["use_yn"] == "N"]
    retired = [cid for cid, r in have.items()
               if cid not in now_ids and r["use_yn"] == "Y"]

    print("판 %s · jsonl %d조각 · DB %d행" % (a.edition, len(rows), len(have)))
    print("  신설 %d · 개정 %d · 되살림 %d · 폐기 %d"
          % (len(added), len(changed), len(revived), len(retired)))
    if retired[:5]:
        print("  폐기 표본:")
        for cid in retired[:5]:
            print("    · %s" % cid)
    if a.dry:
        print("\n--dry 라 아무것도 쓰지 않았습니다.")
        return

    # 1) 있는 것을 넣는다 (chunk_id 충돌 시 갱신)
    for i in range(0, len(rows), BATCH):
        rest(url, key, TABLE + "?on_conflict=chunk_id", "POST", rows[i:i + BATCH],
             prefer="resolution=merge-duplicates,return=minimal")
        print("  넣는 중 %d/%d" % (min(i + BATCH, len(rows)), len(rows)), end="\r")
    print("  넣기 완료 %d행          " % len(rows))

    # 2) git 에서 사라진 것을 내린다 — 지우지 않고 자리만 뺀다
    today = dt.date.today().isoformat()
    for i in range(0, len(retired), BATCH):
        part = retired[i:i + BATCH]
        q = "%s?chunk_id=in.(%s)" % (TABLE, ",".join('"%s"' % c for c in part))
        rest(url, key, q, "PATCH", {"use_yn": "N", "expire_ymd": today},
             prefer="return=minimal")
    if retired:
        print("  폐기 처리 %d행 (use_yn='N', expire_ymd=%s)" % (len(retired), today))

    print("\n끝났습니다. 조회는 use_yn='Y' 인 것만 하십시오.")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
