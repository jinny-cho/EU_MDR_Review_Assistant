"""
EU MDR (2017/745) + MDCG 가이던스 데이터 동기화 및 로컬 벡터 DB 구축 모듈.

설계 원칙
- 백지 상태(DB 없음, PDF 없음, db_version.json 없음)에서도 그대로 동작한다.
- 최신 여부 점검은 HTTP 조회만 사용한다 (LLM/Gemini 토큰 사용 없음).
- 원격 확인에 실패하면 "최신 아님"이 아니라 "확인 불가"로 취급하고, 기존 DB는 절대 건드리지 않는다.
- DB 재구축은 임시(staging) 컬렉션에 먼저 만들고, 검증을 통과한 경우에만 교체한다.

CLI 사용법
    py -3.14 update_mdr.py            # 점검 후 업데이트 여부를 묻는다
    py -3.14 update_mdr.py --yes      # 묻지 않고 업데이트 (신규 MDCG 포함)
    py -3.14 update_mdr.py --rebuild  # 변경이 없어도 강제 재구축
"""
import os
import re
import sys
import glob
import json
import time
import argparse
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
import chromadb

# ---------------------------------------------------------------------------
# 경로 및 상수 (실행 위치와 무관하게 이 파일이 있는 폴더 기준)
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PDF_DIR = os.path.join(BASE_DIR, "mdcg_pdfs")
DB_DIR = os.path.join(BASE_DIR, "mdr_chroma_db")
META_FILE = os.path.join(BASE_DIR, "db_version.json")
LOCK_FILE = os.path.join(BASE_DIR, ".build.lock")

COLLECTION_NAME = "eu_mdr_2017_745"
STAGING_NAME = COLLECTION_NAME + "__staging"
# 임베딩 모델 레지스트리. 질의는 반드시 'DB를 만든 모델'로 해야 하므로 db_version.json의 embed_model을 기준으로 고른다.
#   max_seq: 모델이 한 청크에서 읽는 최대 토큰 수 / query·passage: 모델이 요구하는 접두어 (E5 계열)
EMBED_MODELS = {
    "paraphrase-multilingual-MiniLM-L12-v2": {"max_seq": None, "query": "", "passage": "", "size": "약 470MB"},
    "intfloat/multilingual-e5-base": {"max_seq": 512, "query": "query: ", "passage": "passage: ", "size": "약 1.1GB"},
    "intfloat/multilingual-e5-large": {"max_seq": 512, "query": "query: ", "passage": "passage: ", "size": "약 2.2GB"},
    "BAAI/bge-m3": {"max_seq": 1024, "query": "", "passage": "", "size": "약 2.3GB"},
}
LEGACY_EMBED_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"   # embed_model 기록이 없는 기존 DB
# 새로 구축할 때 사용할 모델. 2026-11-18 평가(한국어 질의 30개, Recall@6):
#   MiniLM 70% · e5-base 83% · e5-large 93% · bge-m3 93%  (모두 1,000자 청크 기준, e5-large가 MRR 근소 우위)
EMBED_MODEL = "intfloat/multilingual-e5-large"

# EU 출판국 Cellar (공식 데이터 저장소). EUR-Lex 웹사이트는 봇 차단(AWS WAF)으로 자동 조회 불가.
MDR_CELEX_RE = re.compile(r"^02017R0745-(\d{8})$")
CELLAR_SPARQL_URL = "https://publications.europa.eu/webapi/rdf/sparql"
CELLAR_CELEX_URL = "http://publications.europa.eu/resource/celex/{celex}"
EURLEX_HUMAN_URL = "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:{celex}"

EC_GUIDANCE_URL = "https://health.ec.europa.eu/medical-devices-sector/new-regulations/guidance-mdcg-endorsed-documents-and-other-guidance_en"
EC_BASE = "https://health.ec.europa.eu"

# 구축 결과 검증 기준 (MDR 본문이 정상이면 약 290개 섹션, 약 60만 자)
MIN_MDR_CHUNKS = 200

# 파싱/청킹 로직이 바뀌면 올린다 → 기존 DB는 시작 시 점검에서 '재색인 필요'로 안내됨
#   1: 초기 버전 (중첩 div 중복 수집 문제)
#   2: 가장 안쪽 블록만 수집, 목록 행 병합, 청크 상한 6,000자
#   3: 청크 약 1,000자(상한 1,800자), PDF 1,000자/겹침 150자
PARSER_VERSION = 3
MIN_MDR_XHTML_BYTES = 500_000

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Referer": EC_GUIDANCE_URL,
}

# 관리 대상 핵심 가이던스 (로컬 파일명 : EC 페이지 탐색 키워드 또는 상세 규칙)
#   - 문자열: EC 가이던스 페이지의 /document/download/ 링크 주변 텍스트·URL에서 키워드 검색
#   - dict  : 키워드로 직접 링크를 못 찾으면 landing 페이지로 이동해 file_hint가 포함된 PDF 링크 선택
TRACKED_GUIDANCES = {
    "MDCG_2021-24_Classification.pdf": "MDCG 2021-24",
    "MDCG_2019-11_Software_Qualification.pdf": "MDCG 2019-11",
    "Borderline_and_Classification_Manual.pdf": "Manual on borderline and classification",
    "MDR_Language_Requirements.pdf": {
        "keyword": "language requirements for manufacturers of medical devices",
        "landing": "https://health.ec.europa.eu/publications/overview-language-requirements-manufacturers-medical-devices_en",
        "file_hint": "lang-req-table-mdr",
    },
    "MDCG_2019-7_PRRC_Guidance.pdf": "MDCG 2019-7",
    "MDCG_2021-25_Legacy_Devices.pdf": "MDCG 2021-25",
    "MDCG_2022-4_Surveillance_Transitional.pdf": "MDCG 2022-4",
    "MDCG_2021-5_Standardisation.pdf": "MDCG 2021-5",
    "MDCG_2022-14_NB_Capacity_Transition.pdf": "MDCG 2022-14",
    "MDCG_2019-16_Cybersecurity.pdf": "MDCG 2019-16",
    "MDCG_2020-3_Significant_Changes.pdf": "MDCG 2020-3",
    "MDCG_2020-5_Clinical_Equivalence.pdf": "MDCG 2020-5",
    "MDCG_2020-6_Clinical_Evidence_Legacy.pdf": "MDCG 2020-6",
    "MDCG_2022-21_PSUR_Guidance.pdf": "MDCG 2022-21",
    "MDCG_2019-9_SSCP_Guidance.pdf": "MDCG 2019-9",
    "MDCG_2018-1_UDI_Assignment.pdf": "MDCG 2018-1",
    "MDCG_2021-19_UDI_QMS_Integration.pdf": "MDCG 2021-19",
}


class UpdateCheckError(Exception):
    """원격 서버에서 최신 정보를 확인하지 못함 (기존 DB는 그대로 유지해야 함)."""


class BuildError(Exception):
    """DB 구축 실패 (기존 DB는 그대로 유지됨)."""


def _noop(*_args, **_kwargs):
    pass


# ---------------------------------------------------------------------------
# 메타데이터 / DB 상태
# ---------------------------------------------------------------------------
def load_meta():
    if os.path.exists(META_FILE):
        try:
            with open(META_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def list_local_pdfs():
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(PDF_DIR, "*.pdf")))


class Embedder:
    """문서/질의 임베딩. 최초 사용 시 HuggingFace에서 모델을 내려받는다 (CPU 실행)."""

    def __init__(self, model_name, normalize=True):
        from sentence_transformers import SentenceTransformer
        self.name = model_name
        self.normalize = normalize
        self.cfg = EMBED_MODELS.get(model_name, {"max_seq": None, "query": "", "passage": ""})
        self.model = SentenceTransformer(model_name, device="cpu")
        if self.cfg.get("max_seq"):
            self.model.max_seq_length = self.cfg["max_seq"]

    def _encode(self, texts, prefix):
        vecs = self.model.encode([prefix + t for t in texts], batch_size=16,
                                 normalize_embeddings=self.normalize, show_progress_bar=False)
        return vecs.tolist()

    def encode_docs(self, texts):
        return self._encode(texts, self.cfg.get("passage", ""))

    def encode_query(self, texts):
        return self._encode(texts, self.cfg.get("query", ""))


def index_embed_model(meta=None):
    """현재 DB를 만든 임베딩 모델 (질의에도 같은 모델을 써야 함)."""
    meta = load_meta() if meta is None else meta
    return meta.get("embed_model", LEGACY_EMBED_MODEL)


def get_embedder(model_name=None, normalize=True):
    return Embedder(model_name or EMBED_MODEL, normalize=normalize)


def query_embedder_spec(meta=None):
    """현재 DB에 맞는 질의 임베딩 설정 (모델명, 정규화 여부).
    embed_model 기록이 없는 기존 DB는 정규화하지 않은 벡터(L2 거리)로 만들어졌으므로 질의도 그대로 맞춘다."""
    meta = load_meta() if meta is None else meta
    return index_embed_model(meta), "embed_model" in meta


def get_client():
    os.makedirs(DB_DIR, exist_ok=True)
    return chromadb.PersistentClient(path=DB_DIR)


def _collection_names(client):
    names = []
    for c in client.list_collections():
        names.append(c if isinstance(c, str) else c.name)
    return names


def recover_collection(client):
    """교체 도중 중단되어 본 컬렉션이 없고 staging만 남은 경우 복구한다."""
    names = _collection_names(client)
    if COLLECTION_NAME not in names and STAGING_NAME in names:
        staging = client.get_collection(STAGING_NAME)
        if staging.count() > 0:
            staging.modify(name=COLLECTION_NAME)


def get_collection():
    """앱에서 사용할 컬렉션. 없으면 None. (질의 임베딩은 Embedder로 직접 계산해 query_embeddings로 전달)"""
    client = get_client()
    recover_collection(client)
    if COLLECTION_NAME not in _collection_names(client):
        return None
    col = client.get_collection(name=COLLECTION_NAME)
    return col if col.count() > 0 else None


def db_ready(meta=None):
    """DB가 정상적으로 구축되어 검색 가능한 상태인지."""
    meta = load_meta() if meta is None else meta
    if meta.get("mdr_chunks", 0) < MIN_MDR_CHUNKS:
        return False
    try:
        client = get_client()
        recover_collection(client)
        if COLLECTION_NAME not in _collection_names(client):
            return False
        return client.get_collection(COLLECTION_NAME).count() > 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 1. EU MDR 최신 통합본 확인 (Cellar SPARQL) 및 본문 다운로드
# ---------------------------------------------------------------------------
def get_latest_consolidated_info():
    """최신 EU MDR 통합본의 (YYYY-MM-DD, CELEX)를 반환. 실패 시 UpdateCheckError."""
    query = """
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT DISTINCT ?celex WHERE {
  ?w cdm:resource_legal_id_celex ?celex .
  FILTER(STRSTARTS(STR(?celex), "02017R0745-"))
} ORDER BY DESC(?celex) LIMIT 50
"""
    try:
        resp = requests.get(
            CELLAR_SPARQL_URL,
            params={"query": query, "format": "application/sparql-results+json"},
            headers={"User-Agent": HEADERS["User-Agent"]},
            timeout=30,
        )
        resp.raise_for_status()
        bindings = resp.json()["results"]["bindings"]
    except Exception as e:
        raise UpdateCheckError(f"EU 출판국(Cellar) 조회 실패: {e}") from e

    dates = []
    for b in bindings:
        m = MDR_CELEX_RE.match(b.get("celex", {}).get("value", "").strip())
        if m:
            dates.append(m.group(1))
    if not dates:
        raise UpdateCheckError("EU MDR 통합본(02017R0745-*) 목록을 찾지 못했습니다.")
    latest = max(dates)
    return f"{latest[:4]}-{latest[4:6]}-{latest[6:]}", f"02017R0745-{latest}"


def fetch_mdr_xhtml(celex):
    """Cellar에서 해당 CELEX의 영문 XHTML 본문을 받는다. 실패 시 BuildError."""
    url = CELLAR_CELEX_URL.format(celex=celex)
    try:
        resp = requests.get(
            url,
            headers={"Accept": "application/xhtml+xml, text/html", "Accept-Language": "eng",
                     "User-Agent": HEADERS["User-Agent"]},
            timeout=180,
            allow_redirects=True,
        )
    except Exception as e:
        raise BuildError(f"EU MDR 본문 다운로드 실패: {e}") from e
    ctype = resp.headers.get("Content-Type", "")
    if resp.status_code != 200 or "html" not in ctype or len(resp.content) < MIN_MDR_XHTML_BYTES:
        raise BuildError(
            f"EU MDR 본문 응답이 비정상입니다 (status={resp.status_code}, type={ctype}, size={len(resp.content)})."
        )
    resp.encoding = "utf-8"
    return resp.text


# ---------------------------------------------------------------------------
# 2. EC MDCG 가이던스 개정/신규 확인
# ---------------------------------------------------------------------------
def _spec(value):
    if isinstance(value, dict):
        return value
    return {"keyword": value}


def _abs_url(href):
    return href if href.startswith("http") else f"{EC_BASE}{href}"


def _is_english_pdf(url):
    return bool(re.search(r"_en(_\d+)?\.pdf", url.lower()))


def _is_ivdr_only(text):
    t = text.lower()
    return ("ivdr" in t or "in vitro" in t or "ivd " in t) and not re.search(r"\bmdr\b|medical devices regulation", t)


def _resolve_from_landing(landing_url, file_hint):
    resp = requests.get(landing_url, headers=HEADERS, timeout=25)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/document/download/" in href and file_hint.lower() in href.lower():
            return _abs_url(href)
    return None


def check_ec_guidances_status(local_meta=None):
    """
    EC 공식 가이던스 페이지를 1회 조회하여
      1) 관리 대상 문서의 최신 URL과 개정/누락 여부
      2) 새로 발행된 MDCG 문서 코드
    를 반환한다. 실패 시 UpdateCheckError.
    """
    local_meta = local_meta or {}
    legacy_meta = bool(local_meta) and "guidance_urls" not in local_meta
    saved_urls = local_meta.get("guidance_urls", {})
    known_codes = set(local_meta.get("known_mdcg_codes", []))
    tracked = dict(TRACKED_GUIDANCES)
    tracked.update(local_meta.get("extra_tracked", {}))

    try:
        resp = requests.get(EC_GUIDANCE_URL, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        raise UpdateCheckError(f"EC 가이던스 페이지 조회 실패: {e}") from e
    soup = BeautifulSoup(resp.text, "lxml")

    download_links = []          # (url, context_text)
    page_codes = {}              # code -> {code, title, url}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/document/download/" not in href:
            continue
        full_url = _abs_url(href)
        parent_txt = a.parent.get_text(" ", strip=True) if a.parent else ""
        ctx = f"{a.get_text(' ', strip=True)} {parent_txt} {href}"
        download_links.append((full_url, ctx))
        for code_match in re.findall(r"MDCG\s+20\d{2}-\d+", ctx, re.IGNORECASE):
            code = re.sub(r"\s+", " ", code_match.upper())
            entry = page_codes.get(code)
            # 같은 코드의 링크가 여러 개면 영문 PDF 링크를 우선
            if entry is None or (not _is_english_pdf(entry["url"]) and _is_english_pdf(full_url)):
                page_codes[code] = {"code": code, "title": (parent_txt or code)[:160], "url": full_url}

    if not download_links:
        raise UpdateCheckError("EC 가이던스 페이지에서 다운로드 링크를 찾지 못했습니다 (페이지 구조 변경 가능성).")

    latest_tracked, updated_items, unmatched = {}, [], []
    for fname, raw in tracked.items():
        spec = _spec(raw)
        kw = spec["keyword"].lower()
        kw_us, kw_hy = kw.replace(" ", "_"), kw.replace(" ", "-")
        matched = None
        for url, ctx in download_links:
            ul = url.lower()
            if "ivdr" in ul and "mdr" not in ul.replace("ivdr", ""):
                continue
            if kw in ctx.lower() or kw_us in ul or kw_hy in ul:
                matched = url
                break
        if matched is None and spec.get("landing"):
            try:
                matched = _resolve_from_landing(spec["landing"], spec.get("file_hint", ""))
            except Exception:
                matched = None
        if matched is None:
            unmatched.append(fname)
            continue

        latest_tracked[fname] = matched
        exists = os.path.exists(os.path.join(PDF_DIR, fname))
        old = saved_urls.get(fname)
        if not exists:
            reason = "missing"
        elif legacy_meta or old is None:
            reason = "unverified"   # 이전 버전 DB라 개정 여부를 알 수 없음 → 재다운로드로 최신 보장
        elif old != matched:
            reason = "revision"
        else:
            continue
        updated_items.append({"filename": fname, "keyword": spec["keyword"], "url": matched,
                              "reason": reason, "is_revision": reason == "revision"})

    tracked_codes = {re.sub(r"\s+", " ", _spec(v)["keyword"].upper()) for v in tracked.values()}
    new_docs = []
    if known_codes:  # 최초 구축 시에는 페이지 전체를 기준선으로 삼는다
        for code, info in sorted(page_codes.items(), reverse=True):
            if code in known_codes or code in tracked_codes or _is_ivdr_only(info["title"]):
                continue
            new_docs.append(info)

    return {
        "latest_tracked_urls": latest_tracked,
        "updated_items": updated_items,
        "unmatched": unmatched,
        "all_page_mdcg_codes": sorted(page_codes.keys()),
        "new_mdcg_docs": new_docs,
        "legacy_meta": legacy_meta,
    }


# ---------------------------------------------------------------------------
# 3. 통합 점검 (앱 시작 시 자동 호출: HTTP 요청 2회, LLM 토큰 0)
# ---------------------------------------------------------------------------
def check_for_updates(local_meta=None):
    meta = load_meta() if local_meta is None else local_meta
    result = {
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "db_ready": db_ready(meta),
        "mdr": {"latest": None, "celex": None, "local": meta.get("version_date"), "changed": False, "error": None},
        "guidance": {"updated_items": [], "new_mdcg_docs": [], "unmatched": [], "latest_tracked_urls": {},
                     "all_page_mdcg_codes": [], "legacy_meta": False, "error": None},
        "local_pdf_changed": False,
        # 문서 처리 방식(파서) 또는 임베딩 모델이 바뀌면 재색인 필요
        "parser_outdated": bool(meta) and (meta.get("parser_version", 1) < PARSER_VERSION
                                           or index_embed_model(meta) != EMBED_MODEL),
    }

    try:
        latest, celex = get_latest_consolidated_info()
        result["mdr"].update(latest=latest, celex=celex, changed=(latest != meta.get("version_date")))
    except UpdateCheckError as e:
        result["mdr"]["error"] = str(e)

    try:
        result["guidance"].update(check_ec_guidances_status(meta))
    except UpdateCheckError as e:
        result["guidance"]["error"] = str(e)

    if meta:
        result["local_pdf_changed"] = list_local_pdfs() != sorted(meta.get("pdf_files", []))

    g = result["guidance"]
    result["needs_build"] = not result["db_ready"]
    result["has_updates"] = bool(
        result["mdr"]["changed"] or g["updated_items"] or g["new_mdcg_docs"] or result["local_pdf_changed"]
        or result["parser_outdated"]
    )
    result["ok"] = result["mdr"]["error"] is None and g["error"] is None
    return result


# ---------------------------------------------------------------------------
# 4. 다운로드 / 파싱
# ---------------------------------------------------------------------------
def download_guidance_updates(items, log=print):
    """PDF를 내려받아 검증(%PDF 시그니처) 후 원자적으로 교체. (성공 파일명 목록, 실패 목록) 반환."""
    os.makedirs(PDF_DIR, exist_ok=True)
    session = requests.Session()
    session.headers.update(HEADERS)
    ok, failed = [], []
    for item in items:
        fname, url = item["filename"], item["url"]
        out_path = os.path.join(PDF_DIR, fname)
        tmp_path = out_path + ".part"
        try:
            resp = session.get(url, timeout=60, allow_redirects=True)
            resp.raise_for_status()
            if not resp.content.startswith(b"%PDF-"):
                raise ValueError(f"PDF가 아닌 응답 (Content-Type: {resp.headers.get('Content-Type')})")
            with open(tmp_path, "wb") as f:
                f.write(resp.content)
            os.replace(tmp_path, out_path)
            tag = {"revision": "개정판", "missing": "신규", "unverified": "재확인", "new": "신규 MDCG"}.get(
                item.get("reason"), "다운로드")
            log(f"  ✔ [{tag}] {fname} ({round(len(resp.content) / 1024, 1)} KB)")
            ok.append(fname)
        except Exception as e:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            log(f"  ❌ [다운로드 실패] {fname}: {e}")
            failed.append({"filename": fname, "error": str(e)})
        time.sleep(1.0)
    return ok, failed


_AMEND_MARKER_RE = re.compile(r"[▼►◄](?:[A-Z]\d*)?")


def _clean_text(txt):
    txt = txt.replace("\xa0", " ")
    txt = _AMEND_MARKER_RE.sub("", txt)       # 통합본 개정 표시(▼B, ▼M1, ►C1, ◄) 제거
    return re.sub(r"[ \t]+", " ", txt).strip()


_BLOCK_TAGS = ["p", "div", "table", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li"]

# 청크 분할 기준: 목표 크기를 넘으면 하위 항목 경계에서, 상한을 넘으면 어디서든 분할
# (임베딩 모델이 512토큰까지만 읽으므로 약 1,000자 단위가 검색 정확도에 유리 — 평가 결과 반영)
CHUNK_TARGET = 1000
CHUNK_HARD_MAX = 1800
PDF_CHUNK_SIZE = 1000
PDF_CHUNK_OVERLAP = 150
_SUBCLAUSE_RE = re.compile(r"^(\(\d+\)|\d+\.|[a-z]\)|\([a-z]\)|Rule\s+\d+|—)")


def _iter_text_blocks(root):
    """
    문서를 위에서 아래로 훑으며 '가장 안쪽' 블록만 한 번씩 반환한다.
    (중첩된 div를 각각 읽으면 같은 문장이 여러 번, 최상위 컨테이너는 문서 전체가 중복 수집됨)
    - table, 목록 행(grid-container: '—' 기호 열 + 본문 열)은 한 덩어리로 반환
    - 하위 블록이 없는 요소는 그대로 반환
    - 하위 블록이 있는 컨테이너는 내부로 내려간다
    """
    for child in root.find_all(True, recursive=False):
        classes = child.get("class") or []
        if child.name == "table" or "grid-container" in classes:
            yield child
        elif child.find(_BLOCK_TAGS) is None:
            yield child
        else:
            yield from _iter_text_blocks(child)


def parse_mdr_html(html_text):
    """EU MDR XHTML을 조항(Recital, Article, Annex) 단위로 분리한다."""
    soup = BeautifulSoup(html_text, "lxml")
    chunks = []
    current_chapter = ""
    current_section = "Preamble / Recitals"
    current_title = "Recitals (Whereas)"
    buffer = []

    def flush_buffer():
        text = "\n".join(buffer).strip()
        if len(text) > 50:
            chunks.append({
                "doc_type": "EU_MDR",
                "source_name": "EU MDR (2017/745)",
                "chapter": current_chapter,
                "section": current_section,
                "title": current_title,
                "content": text,
            })
        buffer.clear()

    for tag in _iter_text_blocks(soup.body or soup):
        txt = _clean_text(tag.get_text(" ", strip=True))
        if not txt:
            continue
        if re.match(r"^CHAPTER\s+[IVX]+", txt, re.IGNORECASE):
            flush_buffer()
            current_chapter = txt
            continue
        if re.match(r"^Article\s+\d+[a-z]?$", txt, re.IGNORECASE):
            flush_buffer()
            current_section = txt
            current_title = txt
            buffer.append(txt)
            continue
        if re.match(r"^ANNEX\s+[IVX]+$", txt, re.IGNORECASE):
            flush_buffer()
            current_chapter = "ANNEXES"
            current_section = txt
            current_title = txt
            buffer.append(txt)
            continue
        if len(buffer) == 1 and buffer[0] == current_section and len(txt) < 150:
            current_title = f"{current_section} - {txt}"
            buffer.append(f"[{txt}]")
            continue
        buf_len = sum(len(x) for x in buffer)
        if (buf_len > CHUNK_TARGET and _SUBCLAUSE_RE.match(txt)) or buf_len + len(txt) > CHUNK_HARD_MAX:
            flush_buffer()
            buffer.append(f"({current_title} 이어짐)")
        buffer.append(txt)

    flush_buffer()
    return chunks


def parse_mdcg_pdfs(pdf_dir, log=print):
    """폴더 내 모든 PDF를 페이지/청크 단위로 파싱한다. (청크, 성공 파일, 실패 목록) 반환."""
    os.makedirs(pdf_dir, exist_ok=True)
    pdf_chunks, processed, failed = [], [], []
    for pdf_path in sorted(glob.glob(os.path.join(pdf_dir, "*.pdf"))):
        filename = os.path.basename(pdf_path)
        doc_label = os.path.splitext(filename)[0]
        try:
            reader = PdfReader(pdf_path)
            count = 0
            for page_idx, page in enumerate(reader.pages, start=1):
                raw_text = page.extract_text() or ""
                cleaned = re.sub(r"\n{3,}", "\n\n", raw_text).strip()
                if len(cleaned) < 40:
                    continue
                size, overlap = PDF_CHUNK_SIZE, PDF_CHUNK_OVERLAP
                parts = [cleaned] if len(cleaned) <= size else [
                    cleaned[s:s + size] for s in range(0, len(cleaned), size - overlap)
                ]
                for part_idx, part in enumerate(parts, start=1):
                    suffix = f" (Part {part_idx})" if len(parts) > 1 else ""
                    pdf_chunks.append({
                        "doc_type": "MDCG_GUIDANCE",
                        "source_name": doc_label,
                        "chapter": "MDCG / EC Guidance",
                        "section": f"Page {page_idx}",
                        "title": f"{doc_label} - Page {page_idx}{suffix}",
                        "content": part,
                    })
                    count += 1
            processed.append(filename)
            log(f"  ✔ {filename} ({len(reader.pages)}p → {count}개 섹션)")
        except Exception as e:
            log(f"  ❌ {filename} 파싱 실패: {e}")
            failed.append({"filename": filename, "error": str(e)})
    return pdf_chunks, processed, failed


# ---------------------------------------------------------------------------
# 5. DB 구축 (staging → 검증 → 교체)
# ---------------------------------------------------------------------------
def _acquire_lock():
    if os.path.exists(LOCK_FILE) and time.time() - os.path.getmtime(LOCK_FILE) > 2 * 3600:
        os.remove(LOCK_FILE)  # 비정상 종료로 남은 오래된 잠금
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        raise BuildError("다른 창에서 이미 DB 업데이트가 진행 중입니다. 잠시 후 다시 시도하세요.")


def _release_lock():
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


def _safe_new_filename(code, url):
    stem = os.path.splitext(url.split("filename=")[-1].split("&")[0])[0] if "filename=" in url else "guidance"
    stem = re.sub(r"[^A-Za-z0-9_\-]", "_", stem)[:60]
    return f"{code.replace(' ', '_')}_{stem}.pdf"


def build_database(check=None, include_new=True, log=print, progress=None):
    """
    최신 문서를 내려받고 벡터 DB를 다시 만든다. 실패하면 BuildError를 던지고 기존 DB는 유지된다.
    check: check_for_updates() 결과 (없으면 여기서 점검). 앱에서 이미 점검했다면 넘겨서 중복 조회를 피한다.
    progress: (0.0~1.0, 메시지)를 받는 콜백 (선택).
    """
    progress = progress or _noop
    _acquire_lock()
    client = None
    try:
        os.makedirs(PDF_DIR, exist_ok=True)
        meta = load_meta()
        if check is None:
            log("최신 상태 점검 중...")
            check = check_for_updates(meta)
        if check["mdr"]["error"]:
            raise BuildError(f"EU MDR 최신 버전을 확인하지 못해 중단합니다: {check['mdr']['error']}")
        if check["guidance"]["error"]:
            raise BuildError(f"MDCG 가이던스 목록을 확인하지 못해 중단합니다: {check['guidance']['error']}")

        g = check["guidance"]
        items = list(g["updated_items"])
        new_files = {}
        if include_new:
            for doc in g["new_mdcg_docs"]:
                fname = _safe_new_filename(doc["code"], doc["url"])
                new_files[fname] = doc["code"]
                items.append({"filename": fname, "keyword": doc["code"], "url": doc["url"], "reason": "new"})

        progress(0.05, "가이던스 PDF 다운로드")
        ok_files, failed_downloads = [], []
        if items:
            log(f"[1/4] 가이던스 PDF {len(items)}건 다운로드")
            ok_files, failed_downloads = download_guidance_updates(items, log)
        else:
            log("[1/4] 다운로드할 가이던스 없음")

        progress(0.25, "EU MDR 통합본 다운로드")
        celex, latest_date = check["mdr"]["celex"], check["mdr"]["latest"]
        log(f"[2/4] EU MDR 통합본 다운로드 및 파싱 ({latest_date}, {celex})")
        mdr_chunks = parse_mdr_html(fetch_mdr_xhtml(celex))
        if len(mdr_chunks) < MIN_MDR_CHUNKS:
            raise BuildError(f"EU MDR 파싱 결과가 비정상입니다 ({len(mdr_chunks)}개 < 최소 {MIN_MDR_CHUNKS}개).")
        log(f"  ✔ EU MDR {len(mdr_chunks)}개 섹션")

        progress(0.35, "가이던스 PDF 파싱")
        log("[3/4] 가이던스 PDF 파싱")
        pdf_chunks, processed_files, failed_parse = parse_mdcg_pdfs(PDF_DIR, log)
        all_chunks = mdr_chunks + pdf_chunks

        progress(0.45, "임베딩 모델 준비")
        size_txt = EMBED_MODELS.get(EMBED_MODEL, {}).get("size", "")
        log(f"[4/4] 벡터 DB 구축 ({len(all_chunks)}개 섹션, 모델 {EMBED_MODEL}) — "
            f"최초 실행 시 임베딩 모델({size_txt}) 다운로드")
        embedder = get_embedder(EMBED_MODEL)
        client = get_client()
        if STAGING_NAME in _collection_names(client):
            client.delete_collection(STAGING_NAME)
        staging = client.create_collection(name=STAGING_NAME, embedding_function=None,
                                           metadata={"hnsw:space": "cosine"})
        batch_size = 32
        for i in range(0, len(all_chunks), batch_size):
            batch = all_chunks[i:i + batch_size]
            docs = [f"[{c['source_name']} | {c['title']}]\n{c['content']}" for c in batch]
            staging.add(
                ids=[f"chunk_{i + j}" for j in range(len(batch))],
                documents=docs,
                embeddings=embedder.encode_docs(docs),
                metadatas=[{k: c[k] for k in ("doc_type", "source_name", "chapter", "section", "title")}
                           for c in batch],
            )
            done = min(i + batch_size, len(all_chunks))
            progress(0.45 + 0.5 * done / len(all_chunks), f"임베딩 {done}/{len(all_chunks)}")

        if staging.count() != len(all_chunks):
            raise BuildError(f"DB 적재 검증 실패 ({staging.count()} / {len(all_chunks)}).")

        # 교체: 본 컬렉션 삭제 → staging 이름 변경 (중간에 끊겨도 recover_collection()이 복구)
        if COLLECTION_NAME in _collection_names(client):
            client.delete_collection(COLLECTION_NAME)
        staging.modify(name=COLLECTION_NAME)

        # 메타데이터: 실제로 받은 파일만 URL을 갱신 (실패분은 다음 점검에서 다시 제안됨)
        guidance_urls = dict(meta.get("guidance_urls", {}))
        failed_names = {f["filename"] for f in failed_downloads}
        for fname, url in g["latest_tracked_urls"].items():
            if fname not in failed_names and os.path.exists(os.path.join(PDF_DIR, fname)):
                guidance_urls[fname] = url
        extra_tracked = dict(meta.get("extra_tracked", {}))
        for fname, code in new_files.items():
            if fname in ok_files:
                extra_tracked[fname] = code
                guidance_urls[fname] = next(d["url"] for d in g["new_mdcg_docs"] if d["code"] == code)
        known = set(meta.get("known_mdcg_codes", []))
        if not known:
            known = set(g["all_page_mdcg_codes"])          # 최초 기준선
        known |= {code for fname, code in new_files.items() if fname in ok_files}

        new_meta = {
            "version_date": latest_date,
            "celex": celex,
            "source_url": EURLEX_HUMAN_URL.format(celex=celex),
            "built_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "parser_version": PARSER_VERSION,
            "embed_model": EMBED_MODEL,
            "mdr_chunks": len(mdr_chunks),
            "pdf_chunks": len(pdf_chunks),
            "total_chunks": len(all_chunks),
            "pdf_files": processed_files,
            "guidance_urls": guidance_urls,
            "extra_tracked": extra_tracked,
            "known_mdcg_codes": sorted(known),
        }
        tmp_meta = META_FILE + ".tmp"
        with open(tmp_meta, "w", encoding="utf-8") as f:
            json.dump(new_meta, f, ensure_ascii=False, indent=2)
        os.replace(tmp_meta, META_FILE)

        progress(1.0, "완료")
        summary = {
            "version_date": latest_date,
            "mdr_chunks": len(mdr_chunks),
            "pdf_count": len(processed_files),
            "pdf_chunks": len(pdf_chunks),
            "downloaded": ok_files,
            "failed_downloads": failed_downloads,
            "failed_parse": failed_parse,
            "unmatched": g["unmatched"],
        }
        log(f"🎉 완료: EU MDR ({latest_date}) + 가이던스 PDF {len(processed_files)}종")
        return summary
    except BuildError:
        raise
    except Exception as e:
        raise BuildError(f"DB 구축 중 오류: {e}") from e
    finally:
        try:
            if client is not None and STAGING_NAME in _collection_names(client):
                client.delete_collection(STAGING_NAME)
        except Exception:
            pass
        _release_lock()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _print_check(check):
    m, g = check["mdr"], check["guidance"]
    print(f"▶ DB 상태          : {'정상' if check['db_ready'] else '없음/불완전 (구축 필요)'}")
    if m["error"]:
        print(f"▶ EU MDR           : 확인 불가 — {m['error']}")
    else:
        print(f"▶ EU MDR 기준일    : 최신 {m['latest']} / 로컬 {m['local'] or '없음'}")
    if g["error"]:
        print(f"▶ MDCG 가이던스    : 확인 불가 — {g['error']}")
    else:
        print(f"▶ 가이던스 개정/누락: {len(g['updated_items'])}건")
        for it in g["updated_items"]:
            print(f"   - [{it['reason']}] {it['filename']}")
        print(f"▶ 신규 MDCG        : {len(g['new_mdcg_docs'])}건")
        for d in g["new_mdcg_docs"]:
            print(f"   - {d['code']}: {d['title']}")
        if g["unmatched"]:
            print(f"⚠ EC 페이지에서 찾지 못한 관리 문서: {', '.join(g['unmatched'])}")
    if check["local_pdf_changed"]:
        print("▶ mdcg_pdfs 폴더의 PDF 구성이 DB와 다름 (재구축 필요)")
    if check.get("parser_outdated"):
        print("▶ 문서 처리/검색 방식이 개선됨 (재색인 필요)")


def main():
    parser = argparse.ArgumentParser(description="EU MDR / MDCG 로컬 DB 동기화")
    parser.add_argument("--yes", action="store_true", help="묻지 않고 업데이트 (신규 MDCG 포함)")
    parser.add_argument("--rebuild", action="store_true", help="변경이 없어도 강제 재구축")
    args = parser.parse_args()

    print("최신 상태 점검 중 (EU 출판국 Cellar + EC 가이던스 페이지)...\n")
    check = check_for_updates()
    _print_check(check)
    if not check["ok"]:
        print("\n❌ 원격 확인에 실패하여 기존 DB를 그대로 유지합니다. 네트워크 상태를 확인 후 다시 실행하세요.")
        return 1

    if not (check["needs_build"] or check["has_updates"] or args.rebuild):
        print("\n✅ EU MDR 법령과 모든 가이던스가 최신 상태입니다.")
        return 0

    include_new = True
    if not args.yes:
        if input("\n지금 업데이트(재구축)할까요? (y/n): ").strip().lower() != "y":
            return 0
        if check["guidance"]["new_mdcg_docs"]:
            include_new = input("신규 MDCG 가이던스도 함께 추가할까요? (y/n): ").strip().lower() == "y"

    print()
    try:
        summary = build_database(check=check, include_new=include_new)
    except BuildError as e:
        print(f"\n❌ {e}\n기존 DB는 변경되지 않았습니다.")
        return 1
    if summary["failed_downloads"] or summary["failed_parse"]:
        print("⚠ 일부 문서 처리에 실패했습니다. 다음 점검 때 다시 시도됩니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
