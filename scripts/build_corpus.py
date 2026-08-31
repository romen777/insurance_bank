# -*- coding: utf-8 -*-
"""요양급여 적용기준·심사지침 PDF -> 항목 단위 JSONL 코퍼스.

원본 PDF는 [항목 / 제목 / 세부인정사항] 3단 표 구조이고, 각 항목은
"(고시 제YYYY-N호, YY.M.D. 시행)" 형태의 근거표기로 끝난다.
이 구조를 이용해 표의 한 행 = 하나의 청크로 잘라낸다.

usage:  python scripts/build_corpus.py
out:    data/chunks.jsonl, data/manifest.json
"""
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data"

HEADER_Y = 585.0          # 이 위쪽은 페이지 머리글(부문/장 제목)
FOOTER_Y = 42.0           # 이 아래쪽은 쪽번호
TABLE_HEAD_Y = 540.0      # 이 위쪽의 "항 목"/"연번" 등은 표 머리글
LINE_TOL = 2.2            # 같은 시각적 행으로 묶을 y 허용오차
BLOCK_GAP = 19.0          # 본문 행간(~15)보다 크면 새 항목 블록
PAGE_OFFSET = 8           # PDF 쪽 - 8 = 인쇄된 쪽번호
COL1_WIDTH = 15.0         # 표제 영역에서 항목열/제목열을 가르는 폭
MAX_BODY = 2500           # 이보다 긴 본문은 분할
MIN_BODY = 20             # 이보다 짧은 본문만 있는 조각은 버림

# 매 페이지 반복되는 표 머리글 셀 (공백 제거 후 비교)
TABLE_HEAD_CELLS = {"항목", "제목", "세부인정사항", "연번", "구분", "내용", "비고"}

# 근거표기 시작: (고시 제2023-56호, ... / (공고 제2019-442호, ...
ATTR_RE = re.compile(r"^\(\s*(고시|공고|보건복지부|심사지침|행정해석|신설|개정)")
DECREE_RE = re.compile(r"(고시|공고)\s*제\s*(\d{4})\s*-\s*(\d+)\s*호")
YY_DATE_RE = re.compile(r"[’‘'`](\d{2})\s*\.\s*(\d{1,2})\s*\.\s*(\d{1,2})\s*\.")
YYYY_DATE_RE = re.compile(r"(20\d{2})\s*\.\s*(\d{1,2})\s*\.\s*(\d{1,2})\s*\.")
CODE_RE = re.compile(r"([가-힣]\d+(?:-\d+)*(?:[가-힣])?)")
SEQ_RE = re.compile(r"^(\d{1,4})\s")
CHAPTER_RE = re.compile(r"제\s*\d+\s*장")
DIVISION_RE = re.compile(r"^([ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ])[.\s]")
SENT_SPLIT_RE = re.compile(r"(?<=[.])\s+")
# 양끝맞춤 아티팩트: "T R U N K" -> "TRUNK"
DESPACE_RE = re.compile(r"(?:(?<=\s)|^)((?:[A-Za-z]\s){3,}[A-Za-z])(?=\s|$)")

DIVISION_NAMES = {
    "Ⅰ": "Ⅰ 행위",
    "Ⅱ": "Ⅱ 약제",
    "Ⅲ": "Ⅲ 치료재료",
    "Ⅳ": "Ⅳ 기결정",
    "Ⅴ": "Ⅴ 질병군",
    "Ⅵ": "Ⅵ 요양병원",
    "Ⅶ": "Ⅶ 호스피스·연명의료",
    "Ⅷ": "Ⅷ 기타",
    "Ⅸ": "Ⅸ 공공정책수가",
    "Ⅹ": "Ⅹ 관리급여",
}

# 짝수/홀수 페이지 머리글에 로마숫자 없이 나오는 부문 이름
PLAIN_DIVISIONS = {
    "행위": "Ⅰ 행위",
    "치료재료": "Ⅲ 치료재료",
    "기결정": "Ⅳ 기결정",
    "질병군": "Ⅴ 질병군",
    "요양병원": "Ⅵ 요양병원",
    "호스피스": "Ⅶ 호스피스·연명의료",
    "연명의료": "Ⅶ 호스피스·연명의료",
    "공공수가": "Ⅸ 공공정책수가",
    "관리급여": "Ⅹ 관리급여",
    "심사지침": "심사지침",
    "별지서식": "별지서식",
}

SKIP_DIVISIONS = {"별지서식"}   # 서식 이미지라 텍스트가 깨져 RAG에 무의미

# 부문 표시가 나오기 전(인쇄 1~70쪽)은 고시 조문 본문이라 단(段) 구조가 없다.
# 표 파서를 적용할 수 없으므로 페이지 단위로 통째 담는다.
PREAMBLE_DIVISION = "고시 전문"
PREAMBLE_CAPTION = "요양급여의 적용기준 및 방법에 관한 세부사항 (보건복지부 고시 제2026-144호) 본문"
PREAMBLE_DECREE = "고시 제2026-144호"


def despace(s):
    return DESPACE_RE.sub(lambda m: m.group(1).replace(" ", ""), s)


def clean(s):
    s = unicodedata.normalize("NFC", s)
    s = s.replace("　", " ").replace("\xa0", " ")
    s = re.sub(r"[ \t\r\n]+", " ", s).strip()
    return despace(s)


def is_table_head(text):
    return re.sub(r"\s+", "", text) in TABLE_HEAD_CELLS


def page_fragments(page):
    """(y, x, text) 조각 추출."""
    frags = []

    def visitor(text, cm, tm, font_dict, font_size):
        if not text or not text.strip():
            return
        frags.append((round(tm[5], 1), round(tm[4], 1), text))

    page.extract_text(visitor_text=visitor)
    return frags


def body_left_edge(frags):
    """긴 텍스트 조각의 최빈 x = 세부인정사항 열의 왼쪽 경계."""
    for min_len in (15, 8):
        xs = Counter(x for y, x, t in frags
                     if len(t.strip()) >= min_len and FOOTER_Y < y < HEADER_Y)
        if xs:
            top = xs.most_common(6)
            cutoff = top[0][1] * 0.5
            return min(x for x, cnt in top if cnt >= cutoff)
    return None


def is_index_page(frags):
    """목록/색인 페이지: 오른쪽 끝에 쪽번호 열이 반복됨."""
    n = sum(1 for y, x, t in frags
            if x > 340 and t.strip().isdigit() and FOOTER_Y < y < HEADER_Y)
    return n >= 5


def page_lines(page):
    """머리글을 분리하고 시각적 행 단위로 (y, head_items, body)를 반환.

    head_items 는 표제 영역의 (x, text) 목록, body 는 세부인정사항 문자열.
    목록/색인 페이지면 lines 대신 None 을 돌려준다.
    """
    frags = page_fragments(page)
    headers = [clean(t) for y, x, t in sorted(frags, reverse=True)
               if y >= HEADER_Y and t.strip()]
    if is_index_page(frags):
        return headers, None

    edge = body_left_edge(frags)
    if edge is None:
        return headers, []
    split_x = edge - 6

    body_frags = [(y, x, t) for y, x, t in frags if FOOTER_Y < y < HEADER_Y]
    body_frags.sort(key=lambda f: (-f[0], f[1]))

    lines, cur, cur_y = [], [], None
    for y, x, t in body_frags:
        if cur_y is None:
            cur, cur_y = [(x, t)], y
        elif abs(y - cur_y) <= LINE_TOL:
            cur.append((x, t))
        else:
            lines.append((cur_y, cur))
            cur, cur_y = [(x, t)], y
    if cur:
        lines.append((cur_y, cur))

    out = []
    for y, items in lines:
        items.sort()
        head_items, body_parts = [], []
        for x, t in items:
            if y >= TABLE_HEAD_Y and is_table_head(t):
                continue                      # 페이지마다 반복되는 표 머리글
            (body_parts if x >= split_x else head_items).append((x, t))
        head_items = [(x, clean(t)) for x, t in head_items if clean(t)]
        body = clean("".join(t for x, t in body_parts))
        if head_items or body:
            out.append((y, head_items, body))
    return headers, out


def parse_decree(text):
    m = DECREE_RE.search(text)
    decree = "%s 제%s-%s호" % (m.group(1), m.group(2), m.group(3)) if m else None
    eff = None
    m2 = YY_DATE_RE.search(text)
    if m2:
        yy = int(m2.group(1))
        year = 2000 + yy if yy < 90 else 1900 + yy
        eff = "%04d-%02d-%02d" % (year, int(m2.group(2)), int(m2.group(3)))
    else:
        m3 = YYYY_DATE_RE.search(text)
        if m3:
            eff = "%s-%02d-%02d" % (m3.group(1), int(m3.group(2)), int(m3.group(3)))
    return decree, eff


def extract_code(title):
    """표제에서 수가코드(자134-1다, 누100가 ...) 또는 연번을 뽑는다."""
    m = SEQ_RE.match(title)
    if m:
        m2 = CODE_RE.search(title[m.end():m.end() + 24])
        return m2.group(1) if m2 else m.group(1)
    m = CODE_RE.search(title[:24])
    return m.group(1) if m else None


class Record:
    """표의 한 행. 표제 조각은 (정렬키, x, 텍스트)로 모았다가 마지막에 열별로 정렬한다."""

    __slots__ = ("head", "body", "attr", "page", "division", "chapter")

    def __init__(self, page, division, chapter):
        self.head, self.body, self.attr = [], [], None
        self.page, self.division, self.chapter = page, division, chapter

    def empty(self):
        return not self.body and not self.head

    def add_head(self, page, y, items):
        for x, t in items:
            self.head.append(((page, -y, x), x, t))

    def split_head(self):
        """표제 영역을 항목열/제목열로 나눠 각각 위에서 아래로 이어붙인다."""
        if not self.head:
            return "", ""
        min_x = min(x for _, x, _ in self.head)
        bound = min_x + COL1_WIDTH
        col1 = [(k, t) for k, x, t in self.head if x <= bound]
        col2 = [(k, t) for k, x, t in self.head if x > bound]
        join = lambda col: clean(" ".join(t for _, t in sorted(col)))
        return join(col1), join(col2)

    def to_dict(self):
        item, title = self.split_head()
        caption = clean((item + " " + title).strip())
        body = clean(" ".join(self.body))
        decree, eff = parse_decree(self.attr) if self.attr else (None, None)
        return {
            "부문": self.division,
            "장": self.chapter,
            "항목코드": extract_code(caption),
            "항목": item,
            "제목": title,
            "표제": caption,
            "본문": body,
            "근거": self.attr,
            "고시번호": decree,
            "시행일": eff,
            "쪽": self.page - PAGE_OFFSET,
            "pdf쪽": self.page,
        }


def update_division(headers, division, chapter):
    for h in headers:
        m = DIVISION_RE.match(h)
        if m:
            division = DIVISION_NAMES.get(m.group(1), h)
            continue
        plain = PLAIN_DIVISIONS.get(re.sub(r"\s+", "", h))
        if plain:
            division = plain
        elif CHAPTER_RE.search(h):
            chapter = re.sub(r"\s+", " ", h)
    return division, chapter


def build(src):
    reader = PdfReader(str(src))
    total = len(reader.pages)
    division = chapter = None
    records = []
    cur = None
    pending_close = False

    for pi in range(total):
        page_no = pi + 1
        headers, lines = page_lines(reader.pages[pi])
        division, chapter = update_division(headers, division, chapter)

        if page_no <= PAGE_OFFSET:             # 표지·목차 등 앞붙임
            continue

        if division is None:                   # 고시 조문 본문: 페이지 단위로 담음
            if cur is not None and not cur.empty():
                records.append(cur)
                cur, pending_close = None, False
            text = clean(" ".join(
                " ".join(t for _, t in items) + " " + body
                for _, items, body in (lines or [])))
            if text:
                records.append({
                    "부문": PREAMBLE_DIVISION, "장": None, "항목코드": None,
                    "항목": "", "제목": PREAMBLE_CAPTION, "표제": PREAMBLE_CAPTION,
                    "본문": text, "근거": PREAMBLE_DECREE,
                    "고시번호": PREAMBLE_DECREE, "시행일": None,
                    "쪽": page_no - PAGE_OFFSET, "pdf쪽": page_no,
                })
            continue

        if division in SKIP_DIVISIONS:
            if cur is not None and not cur.empty():
                records.append(cur)
            cur, pending_close = None, False
            continue
        if lines is None:                      # 목록/색인 페이지
            continue

        prev_y = None
        for y, head_items, body in lines:
            gap = (prev_y - y) if prev_y is not None else 0.0
            prev_y = y

            if body and ATTR_RE.match(body):
                if cur is None:
                    cur = Record(page_no, division, chapter)
                cur.add_head(page_no, y, head_items)
                cur.attr = (cur.attr + " " + body) if cur.attr else body
                pending_close = True
                continue

            if body:
                starts_new = (cur is None or pending_close
                              or (gap > BLOCK_GAP and head_items and cur.body))
                if starts_new:
                    if cur is not None and not cur.empty():
                        records.append(cur)
                    cur = Record(page_no, division, chapter)
                    pending_close = False
                cur.add_head(page_no, y, head_items)
                cur.body.append(body)
            elif head_items:
                if cur is None:
                    cur = Record(page_no, division, chapter)
                cur.add_head(page_no, y, head_items)

    if cur is not None and not cur.empty():
        records.append(cur)
    return [r if isinstance(r, dict) else r.to_dict() for r in records], total


def split_body(body):
    """긴 본문을 문장 경계에서 MAX_BODY 이하 조각으로 나눈다."""
    if len(body) <= MAX_BODY:
        return [body]
    parts, buf = [], ""
    for sent in SENT_SPLIT_RE.split(body):
        while len(sent) > MAX_BODY:            # 문장 하나가 지나치게 길면 강제 절단
            if buf:
                parts.append(buf.strip())
                buf = ""
            parts.append(sent[:MAX_BODY])
            sent = sent[MAX_BODY:]
        if len(buf) + len(sent) + 1 > MAX_BODY and buf:
            parts.append(buf.strip())
            buf = sent
        else:
            buf = (buf + " " + sent).strip()
    if buf:
        parts.append(buf.strip())
    parts = [p for p in parts if p]
    # 꼬리 조각이 너무 짧으면 앞 조각에 붙인다
    if len(parts) > 1 and len(parts[-1]) < 200:
        tail = parts.pop()
        parts[-1] = parts[-1] + " " + tail
    return parts


def is_junk(rec):
    """간지(divider)나 표 머리글만 걸려든 조각."""
    return len(rec["본문"]) < 40 and not rec["고시번호"]


def _norm(t):
    return re.sub(r"\s+", "", t or "")


def _key1(rec):
    """1순위 키 — 위치가 아니라 내용. 개정돼도 살아남는다."""
    return "|".join([_norm(rec.get("항목코드")), _norm(rec.get("제목")), _norm(rec.get("장"))])


def _prefix(rec):
    return rec["부문"].split()[0] if rec["부문"] else "X"


def assign_ids(recs):
    """판이 바뀌어도 같은 조각이면 같은 id.

    부문별 순번(Ⅰ-0001)으로 매기면 앞에 조각 하나만 늘어도 뒤가 전부 밀린다.
    그러면 git diff 가 「2,500줄 전부 변경」이 되어 무엇이 신설·개정·폐기됐는지
    알 수 없다. 해마다 판이 바뀌는 자료라 이 점이 가장 중요하다.

    1패스에서 코드+제목+장으로 키를 잡고, 그것만으로 유일하면 그대로 쓴다.
    겹치는 것(같은 제목이 여러 곳에 나오거나 제목이 아예 없는 조각)만 2패스에서
    본문 앞머리를 키에 보탠다. 순번 접미사는 붙이지 않는다 — 앞 조각이 사라지면
    번호가 밀려 다시 위치에 기대게 되기 때문이다.
    """
    n1 = Counter(_key1(r) for r in recs)
    used = Counter()
    for rec in recs:
        k = _key1(rec)
        if not k.strip("|") or n1[k] > 1:
            k = k + "|" + _norm(rec.get("본문"))[:60]
        base = "%s-%s" % (_prefix(rec), hashlib.sha1(k.encode("utf-8")).hexdigest()[:8])
        used[base] += 1
        rec["_base"] = base if used[base] == 1 else "%s.%d" % (base, used[base])
    return recs


def finalize(raw):
    """짧은 조각을 버리고, 긴 본문을 나누고, 판을 넘어 안정적인 id 를 붙인다."""
    keep = [r for r in raw if len(r["본문"]) >= MIN_BODY and not is_junk(r)]
    assign_ids(keep)
    out = []
    for rec in keep:
        base = rec.pop("_base")
        pieces = split_body(rec["본문"])
        for i, piece in enumerate(pieces, 1):
            item = dict(rec)
            item["본문"] = piece
            item["id"] = base if len(pieces) == 1 else "%s#%d" % (base, i)
            item["분할"] = None if len(pieces) == 1 else "%d/%d" % (i, len(pieces))
            out.append({"id": item.pop("id"), **item})
    return out

def main():
    cands = sorted((ROOT / "source").glob("*.pdf")) or sorted(ROOT.glob("요양급여*.pdf"))
    if not cands:
        sys.exit("원본 PDF를 찾을 수 없습니다 (source/*.pdf)")
    src = cands[0]
    OUT_DIR.mkdir(exist_ok=True)

    raw, pages = build(src)
    chunks = finalize(raw)

    with (OUT_DIR / "chunks.jsonl").open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    lens = sorted(len(c["본문"]) for c in chunks)
    manifest = {
        "source_pdf": src.name,
        "pdf_pages": pages,
        "page_offset": PAGE_OFFSET,
        "chunks": len(chunks),
        "with_decree": sum(1 for c in chunks if c["고시번호"]),
        "with_code": sum(1 for c in chunks if c["항목코드"]),
        "divisions": dict(Counter(c["부문"] for c in chunks)),
        "body_len": {
            "min": lens[0],
            "p50": lens[len(lens) // 2],
            "p90": lens[int(len(lens) * 0.9)],
            "max": lens[-1],
        },
        "builder": "scripts/build_corpus.py",
    }
    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
