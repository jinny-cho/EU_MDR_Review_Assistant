import os
import re
import glob
import shutil
import subprocess
import tempfile
import html
import json
import time
from datetime import datetime
import streamlit as st
from google import genai
from google.genai import types
from docx import Document
from docx.shared import Pt, RGBColor
from update_mdr import (
    BASE_DIR,
    EMBED_MODEL,
    EMBED_MODELS,
    BuildError,
    build_database,
    check_for_updates,
    get_collection,
    get_embedder,
    list_local_pdfs,
    load_meta,
    query_embedder_spec,
)

# 경로 설정 (이 파일이 있는 폴더 기준 → 어느 PC/경로에 배포해도 동작)
ENV_FILE = os.path.join(BASE_DIR, ".env")
# 검토 기록: saved(AI 검토 성공 시 자동 저장, MD) / exported(사용자가 내보낸 MD+Word, 배포·지식 자료용)
RESULTS_DIR = os.path.join(BASE_DIR, "results")
SAVED_DIR = os.path.join(RESULTS_DIR, "saved")
EXPORT_DIR = os.path.join(RESULTS_DIR, "exported")
LEGACY_RECORDS_DIR = os.path.join(BASE_DIR, "saved_records")  # 이전 버전 폴더
os.makedirs(SAVED_DIR, exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True)


def _migrate_legacy_records():
    """이전 버전 폴더(saved_records/)의 기록을 results/로 옮긴다.
      saved_records/saved/            → results/saved/
      saved_records/exported, md, word → results/exported/  (md·word는 사용자가 직접 저장했던 기록)
    같은 이름이 이미 있으면 덮어쓰지 않고 그대로 둔다. 비워진 이전 폴더만 삭제."""
    import shutil
    moves = [("saved", SAVED_DIR), ("exported", EXPORT_DIR), ("md", EXPORT_DIR), ("word", EXPORT_DIR)]
    for old, dest in moves:
        old_dir = os.path.join(LEGACY_RECORDS_DIR, old)
        if not os.path.isdir(old_dir):
            continue
        for name in os.listdir(old_dir):
            src, dst = os.path.join(old_dir, name), os.path.join(dest, name)
            if os.path.isfile(src) and not os.path.exists(dst):
                shutil.move(src, dst)
        try:
            os.rmdir(old_dir)  # 비어 있을 때만 삭제됨
        except OSError:
            pass
    try:
        os.rmdir(LEGACY_RECORDS_DIR)  # 모두 옮겨져 비었으면 삭제
    except OSError:
        pass


_migrate_legacy_records()

# 사용 모델 (고성능 모델만 사용, lite 폴백 금지). 첫 번째가 기본, 이후는 동급 폴백.
GEMINI_MODELS = ["gemini-3.8-flash", "gemini-3.7-flash"]

# 최신 상태 자동 점검 주기 (HTTP 조회만 사용, Gemini 토큰 사용 없음)
UPDATE_CHECK_TTL_SEC = 6 * 3600

# 검색 범위 (화면 표시명 → 저장 기록에 남는 이름, ChromaDB 필터)
SCOPES = {
    "전체": ("전체 통합 검색 (EU MDR + MDCG 가이던스)", None),
    "EU MDR 법령": ("EU MDR (2017/745) 법령만", {"doc_type": "EU_MDR"}),
    "MDCG 가이던스": ("MDCG / 언어 가이던스 PDF만", {"doc_type": "MDCG_GUIDANCE"}),
}

st.set_page_config(page_title="EU MDR 규제 검토 어시스턴트", page_icon=":material/balance:", layout="wide")

# ---------------------------------------------------------------------------
# 스타일 (라이트/다크 테마 모두에서 동작하도록 색은 반투명 톤 위주)
# ---------------------------------------------------------------------------
st.html("""
<style>
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css');

/* padding-top은 Streamlit 상단 헤더 바(높이 약 56px, 항상 본문 위에 그려짐)보다 커야 로고가 가려지지 않음 */
.block-container { padding-top: 4.2rem; padding-bottom: 4rem; max-width: 1320px; }
footer { visibility: hidden; }

/* 코드/원문 블록은 가로 스크롤 대신 줄바꿈 (기록 조회의 긴 법령 원문) */
[data-testid="stMain"] pre, [data-testid="stMain"] pre code {
  white-space: pre-wrap !important; word-break: break-word; overflow-wrap: anywhere; }

/* 헤더 */
.ra-hero { display:flex; justify-content:space-between; align-items:flex-end; gap:1.2rem; flex-wrap:wrap;
           padding-bottom:1.1rem; margin-bottom:1.3rem; border-bottom:1px solid rgba(128,128,128,.22); }
.ra-eyebrow { font-size:.72rem; font-weight:700; letter-spacing:.09em; text-transform:uppercase; opacity:.55; }
.ra-title { font-size:1.75rem; font-weight:750; letter-spacing:-.02em; margin:.2rem 0 .25rem; line-height:1.25; }
.ra-sub { font-size:.92rem; opacity:.68; margin:0; }

/* 상태 칩 */
.ra-chips { display:flex; gap:.45rem; flex-wrap:wrap; }
.ra-chip { display:inline-flex; align-items:center; gap:.4rem; font-size:.78rem; font-weight:550;
           padding:.3rem .7rem; border-radius:999px; border:1px solid rgba(128,128,128,.28); white-space:nowrap; }
.ra-chip b { font-weight:700; }
.ra-dot { width:.5rem; height:.5rem; border-radius:50%; display:inline-block; }
.ra-dot.ok { background:#2E9E6A; } .ra-dot.warn { background:#D9902B; } .ra-dot.err { background:#D2504B; }

/* 섹션 라벨 (왼쪽 네이비→골드 포인트 바) */
.ra-label { display:flex; align-items:center; gap:.5rem; font-size:.72rem; font-weight:700; letter-spacing:.07em;
            text-transform:uppercase; margin:.1rem 0 .55rem; color:#8A93A3; }
.ra-label::before { content:""; width:3px; height:.8rem; border-radius:2px; flex:none;
                    background:#4A7BD0; }  /* 단색 악센트 바: 라이트·다크 모두에서 읽히는 중간 톤 파랑 */

/* 메인 헤더 로고 */
.ra-hero-brand { display:flex; align-items:center; gap:.9rem; }
.ra-hero-brand .ra-logo { width:52px; height:52px; flex:none; }

/* 패널 헤더 */
.ra-panel-title { font-size:1.08rem; font-weight:700; margin:0 0 .2rem; }
.ra-panel-desc { font-size:.88rem; opacity:.7; margin:0 0 .7rem; }

/* 목록 */
.ra-list { list-style:none; padding:0; margin:.1rem 0 .8rem; }
.ra-list li { display:flex; align-items:center; gap:.55rem; padding:.42rem 0; font-size:.9rem;
              border-bottom:1px dashed rgba(128,128,128,.22); }
.ra-list li:last-child { border-bottom:none; }

/* 태그 */
.ra-tag { display:inline-block; font-size:.68rem; font-weight:700; padding:.12rem .5rem; border-radius:5px;
          letter-spacing:.02em; white-space:nowrap; }
.ra-tag.law   { background:rgba(46,94,170,.15);  color:#4A7BD0; }
.ra-tag.guide { background:rgba(196,138,30,.17); color:#C48A1E; }
.ra-tag.new   { background:rgba(46,158,106,.16); color:#2E9E6A; }
.ra-tag.rev   { background:rgba(217,144,43,.18); color:#C9821F; }
.ra-tag.miss  { background:rgba(210,80,75,.15);  color:#D2504B; }
.ra-tag.info  { background:rgba(128,128,128,.16); }

/* 사이드바 키-값 */
.ra-kv { display:grid; grid-template-columns:auto 1fr; gap:.35rem .8rem; font-size:.86rem;
         margin:.2rem 0 .4rem !important; padding:0 !important; }
.ra-kv dt { opacity:.6; margin:0 !important; padding:0 !important; font-weight:500; }
.ra-kv dd { margin:0 !important; padding:0 !important; text-align:right; font-weight:650; }
.ra-kv code { font-size:.78rem; padding:.05rem .35rem; }
.ra-muted { font-size:.78rem; opacity:.6; }

/* 답변 메타 */
.ra-answer-head { display:flex; justify-content:space-between; align-items:center; gap:.6rem; flex-wrap:wrap;
                  margin-bottom:.3rem; }
</style>
""")


def esc(text):
    return html.escape(str(text or ""))


def _load_logo_img():
    """assets/logo_mark.svg를 <img> data URI로 사용.
    (st.html은 보안상 <svg> 태그를 제거하므로 인라인 SVG 대신 이미지로 넣는다. 파일이 없어도 앱은 동작)"""
    import base64
    try:
        with open(os.path.join(BASE_DIR, "assets", "logo_mark.svg"), "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return f'<img class="ra-logo" src="data:image/svg+xml;base64,{b64}" alt="">'
    except OSError:
        return ""


LOGO_SVG = _load_logo_img()


# --- .env 로컬 설정 로드/저장 (다른 항목은 보존) ---
def read_env():
    values = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def write_env_value(key, value):
    lines, found = [], False
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value.strip()}"
            found = True
    if not found:
        lines.append(f"{key}={value.strip()}")
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


for _k, _v in read_env().items():
    os.environ.setdefault(_k, _v)

# ---------------------------------------------------------------------------
# AI 연결 방식 (회사 계약에 따라 API 키를 못 쓰는 경우를 위해 계정 로그인 방식 2가지 지원)
#   gemini_cli : 설치된 Gemini CLI를 통해 호출 (회사 계정 "Login with Google" SSO 로그인)
#   vertex     : Vertex AI + 계정 로그인(ADC: gcloud auth application-default login)
#   gemini     : Gemini API 키
#   openai     : OpenAI API 키
# ---------------------------------------------------------------------------
PROVIDERS = {
    "gemini_cli": "Gemini · 회사 계정 (Gemini CLI)",
    "vertex": "Gemini · 회사 계정 (Vertex AI)",
    "gemini": "Gemini · API 키",
    "openai": "OpenAI · API 키",
}
RETRIES_PER_MODEL = 4
GEMINI_CLI_TIMEOUT_SEC = 300
DEFAULT_VERTEX_LOCATION = "global"   # 리전을 따로 안내받지 않은 경우 Vertex AI의 global 엔드포인트 사용
_CONNECTION_TEST_PROMPT = "Reply with the single word OK."


class AIConnectionError(Exception):
    """사용자에게 그대로 보여줄 수 있는 연결 오류 (transient=True면 재시도·제공사 전환 안내)."""

    def __init__(self, message, transient=False):
        super().__init__(message)
        self.transient = transient


# --- Gemini CLI ---
def gemini_cli_path():
    return shutil.which("gemini")


def gemini_cli_auth_type():
    """Gemini CLI에 설정된 인증 방식 (settings.json의 선택값만 읽음, 자격 증명 내용은 읽지 않음)."""
    try:
        with open(os.path.join(os.path.expanduser("~"), ".gemini", "settings.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        return (cfg.get("security", {}).get("auth", {}).get("selectedType")
                or cfg.get("selectedAuthType") or "")
    except (OSError, ValueError):
        return ""


CLI_AUTH_LABEL = {"oauth-personal": "Google 계정 로그인", "gemini-api-key": "Gemini API 키",
                  "vertex-ai": "Vertex AI", "cloud-shell": "Cloud Shell",
                  "compute-default-credentials": "기본 자격 증명"}


def _run_gemini_cli(args, input_text=None, timeout=GEMINI_CLI_TIMEOUT_SEC):
    """빈 임시 폴더에서 Gemini CLI 실행 (프로젝트 파일 접근 차단). 반환: (종료 코드, stdout, stderr)."""
    cli = gemini_cli_path()
    if not cli:
        raise AIConnectionError("Gemini CLI가 설치되어 있지 않습니다. 사이드바의 설정 방법을 참고하세요.")
    work_dir = tempfile.mkdtemp(prefix="ra_gemini_")
    env = os.environ.copy()
    env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"   # 비어 있는 전용 임시 폴더만 신뢰 (헤드리스 실행 조건)
    env["NO_COLOR"] = "1"
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    if project:
        env["GOOGLE_CLOUD_PROJECT"] = project   # 회사 계정 로그인에 필요한 경우가 있음
    try:
        proc = subprocess.run([cli, *args], input=(input_text or "").encode("utf-8"), capture_output=True,
                              cwd=work_dir, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AIConnectionError(f"Gemini CLI 응답이 {timeout}초 안에 오지 않았습니다.", transient=True)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")


@st.cache_data(ttl=3600, show_spinner=False)
def list_gemini_cli_models(cli_marker):
    """CLI가 허용하는 모델 중 고성능 모델만 (auto·lite 제외 — R6), 최신 버전 우선. 실패 시 빈 목록."""
    try:
        rc, out, _ = _run_gemini_cli(["--list-models"], timeout=90)
    except AIConnectionError:
        return []
    names = re.findall(r"^([a-z0-9][\w.\-]*) \(", out, re.M)
    names = [n for n in dict.fromkeys(names) if n != "auto" and "lite" not in n.lower()]

    def key(n):
        m = re.search(r"(\d+(?:\.\d+)?)", n)
        return (float(m.group(1)) if m else 0.0, "pro" in n)
    return sorted(names, key=key, reverse=True)


def _cli_error_message(text):
    """CLI가 stderr/stdout에 남긴 JSON 오류의 message를 추출 (없으면 빈 문자열)."""
    for m in re.finditer(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', text):
        return m.group(1)
    return ""


def _classify_cli_error(text, model=""):
    """CLI 오류를 사용자 조치 단위로 분류. 순서 중요: 한도·모델 문제를 로그인 문제보다 먼저 판정
    (스택 트레이스에 auth 같은 단어가 섞여 있어 오판하지 않도록)."""
    t = text.lower()
    raw = _cli_error_message(text)
    model_txt = f"({model}) " if model else ""
    if "daily quota" in t or "exhausted your daily" in t:
        return AIConnectionError(f"선택한 모델 {model_txt}의 오늘 사용 한도를 모두 사용했습니다. "
                                 "사이드바에서 다른 모델을 선택하세요.")
    if "modelnotfound" in t or "no longer available" in t or "model not found" in t:
        return AIConnectionError(f"선택한 모델 {model_txt}을 이 계정에서 사용할 수 없습니다. "
                                 "사이드바에서 다른 모델을 선택하거나 직접 입력하세요.")
    if any(k in t for k in ("429", "quota", "resource_exhausted", "503", "unavailable", "overloaded",
                            "rate limit")):
        return AIConnectionError("Gemini 서버가 혼잡하거나 일시적으로 사용 한도에 도달했습니다. 잠시 후 다시 시도하세요."
                                 + (f" ({raw})" if raw else ""), transient=True)
    if any(k in t for k in ("unauthenticated", "login required", "please set an auth method",
                            "credential", "not logged in", "auth method")):
        return AIConnectionError("Gemini CLI 로그인이 필요합니다. 터미널에서 gemini 를 실행해 "
                                 "'Login with Google'을 선택하고 회사 계정으로 로그인하세요.")
    if "trusted" in t:
        return AIConnectionError("Gemini CLI 폴더 신뢰 설정 문제로 실행되지 않았습니다.")
    if raw:
        return AIConnectionError(f"Gemini CLI 오류: {raw[:300]}")
    lines = [ln for ln in text.strip().splitlines() if ln.strip() and not ln.strip().startswith("at ")]
    snippet = lines[-1][:300] if lines else "알 수 없는 오류"
    return AIConnectionError(f"Gemini CLI 오류: {snippet}")


def _cli_used_model(data, requested):
    """JSON stats에서 실제로 답변을 생성한 모델을 찾는다 (CLI가 자체적으로 다른 모델로 전환할 수 있음)."""
    best, best_tokens = None, -1
    for name, info in (data.get("stats", {}).get("models", {}) or {}).items():
        api = info.get("api", {})
        cand = info.get("tokens", {}).get("candidates", 0)
        if api.get("totalRequests", 0) > api.get("totalErrors", 0) and cand > best_tokens:
            best, best_tokens = name, cand
    return best or requested


def generate_with_gemini_cli(prompt, model):
    args = ["-p", "Follow the instructions above and answer.", "-o", "json", "--approval-mode", "plan"]
    if model:
        args += ["-m", model]
    rc, out, err = _run_gemini_cli(args, input_text=prompt)
    data = None
    if "{" in out:
        try:
            data = json.loads(out[out.index("{"):])
        except ValueError:
            data = None
    if rc != 0 or data is None or data.get("error"):
        detail = json.dumps(data.get("error"), ensure_ascii=False) if data and data.get("error") else (err + out)
        raise _classify_cli_error(detail, model)
    used = _cli_used_model(data, model)
    if "lite" in used.lower():  # R6: 경량 모델 답변은 사용하지 않음
        raise AIConnectionError(f"Gemini CLI가 경량 모델({used})로 전환해 답변을 사용하지 않았습니다. "
                                "다른 모델을 선택하거나 잠시 후 다시 시도하세요.", transient=True)
    return data.get("response", "") or "", used


# --- Vertex AI (계정 로그인 / ADC) ---
def vertex_credentials_status():
    """gcloud로 로그인한 ADC 자격 증명이 있는지 (True/False)."""
    try:
        import google.auth
        google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        return True
    except Exception:
        return False


def _classify_vertex_error(e):
    t = str(e)
    if "DefaultCredentialsError" in type(e).__name__ or "default credentials" in t.lower():
        return AIConnectionError("회사 계정 로그인 정보가 없습니다. 터미널에서 "
                                 "gcloud auth application-default login 을 실행하세요.")
    if "PERMISSION_DENIED" in t or "403" in t:
        return AIConnectionError("이 계정/프로젝트에 Vertex AI 사용 권한이 없습니다. 회사 계약이 Gemini CLI 방식이라면 "
                                 "'Gemini · 회사 계정 (Gemini CLI)'를 사용하세요.")
    if "SERVICE_DISABLED" in t or "has not been used" in t:
        return AIConnectionError("이 프로젝트에서 Vertex AI API가 활성화되어 있지 않습니다. IT 담당자에게 문의하세요.")
    if "NOT_FOUND" in t or "404" in t:
        return AIConnectionError(f"모델을 찾을 수 없습니다 ({', '.join(GEMINI_MODELS)}). "
                                 "프로젝트 ID나 리전(고급 설정)을 확인하세요.")
    return e


def make_genai_client(provider, api_key="", vertex_project="", vertex_location=""):
    if provider == "vertex":
        return genai.Client(vertexai=True, project=vertex_project,
                            location=vertex_location or DEFAULT_VERTEX_LOCATION)
    return genai.Client(api_key=api_key)


# --- OpenAI ---
# OpenAI 모델 목록 필터: 범용 상위 모델만 (mini/nano 등 경량 모델, 날짜 스냅샷, 특수 용도 모델 제외 — R6)
_OPENAI_MODEL_RE = re.compile(r"^gpt-(\d+(?:\.\d+)?)$")


@st.cache_data(ttl=3600, show_spinner=False)
def list_openai_models(api_key):
    """API 키로 사용 가능한 OpenAI 모델 중 고성능 범용 모델을 최신 버전 순으로 반환. 실패 시 빈 목록."""
    try:
        from openai import OpenAI
        ids = [m.id for m in OpenAI(api_key=api_key).models.list()]
    except Exception:
        return []
    picked = [(float(mt.group(1)), i) for i in ids if (mt := _OPENAI_MODEL_RE.match(i))]
    return [i for _, i in sorted(picked, reverse=True)]


def _is_transient(e):
    if isinstance(e, AIConnectionError):
        return e.transient
    msg = str(e)
    if any(k in msg for k in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "500", "502", "504", "overloaded")):
        return True
    try:
        import openai
        return isinstance(e, (openai.RateLimitError, openai.APIConnectionError,
                              openai.APITimeoutError, openai.InternalServerError))
    except Exception:
        return False


def _with_retry(call, retries=RETRIES_PER_MODEL):
    last_err = None
    for attempt in range(retries):
        try:
            return call()
        except Exception as e:
            last_err = e
            if not _is_transient(e):
                raise
            if attempt < retries - 1:
                time.sleep(2 ** (attempt + 1))  # 2, 4, 8초
    raise last_err


def generate_answer(provider, prompt, settings):
    """선택한 연결 방식으로 답변 생성. (답변 텍스트, 실제 응답 모델명) 반환.
    settings: gemini_key, openai_key, openai_model, vertex_project, vertex_location, cli_model"""
    if provider == "openai":
        from openai import OpenAI
        client = OpenAI(api_key=settings["openai_key"])
        resp = _with_retry(lambda: client.responses.create(model=settings["openai_model"], input=prompt))
        return resp.output_text or "", settings["openai_model"]

    if provider == "gemini_cli":
        # CLI는 내부적으로도 재시도하고 호출당 수 초가 걸리므로 재시도는 1회만
        return _with_retry(lambda: generate_with_gemini_cli(prompt, settings["cli_model"]), retries=2)

    # Gemini API 키 / Vertex AI: 고성능 모델만 사용 (lite 폴백 금지). 일시 오류는 재시도 후 동급 모델로 전환.
    try:
        client = make_genai_client(provider, settings["gemini_key"], settings["vertex_project"],
                                   settings["vertex_location"])
        last_err = None
        for model_name in GEMINI_MODELS:
            try:
                resp = _with_retry(lambda: client.models.generate_content(
                    model=model_name, contents=prompt, config=types.GenerateContentConfig(temperature=0.1)))
                return resp.text or "", model_name
            except Exception as e:
                if not _is_transient(e):
                    raise
                last_err = e
        raise last_err
    except AIConnectionError:
        raise
    except Exception as e:
        raise _classify_vertex_error(e) if provider == "vertex" else e


def test_ai_connection(provider, settings):
    """아주 짧은 요청으로 연결 확인. (성공 여부, 메시지)"""
    try:
        t0 = time.perf_counter()
        _answer, used = generate_answer(provider, _CONNECTION_TEST_PROMPT, settings)
        return True, f"연결 성공 · 응답 모델 {used} · {time.perf_counter() - t0:.1f}초"
    except Exception as e:
        return False, str(e)


# --- 파일명 안전 변환 ---
def sanitize_filename(name):
    cleaned = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned[:60] if cleaned else "RA_Review_Note"


# --- 기록용 Markdown(.md) 및 배포용 Word(.docx) 저장 함수 ---
def _unique_base(target_dir, base):
    """같은 초에 같은 제목이 겹치면 _2, _3 … 을 붙여 덮어쓰기를 막는다."""
    candidate, n = base, 2
    while any(os.path.exists(os.path.join(target_dir, candidate + ext)) for ext in (".md", ".docx")):
        candidate, n = f"{base}_{n}", n + 1
    return candidate


def save_review_files(title_input, query, answer_text, retrieved_docs, retrieved_metas, mdr_version, search_scope,
                      analysis_model="", source_numbers=None, target_dir=None, with_word=True):
    """검토 기록 파일 생성. target_dir: SAVED_DIR(자동 저장, MD만) 또는 EXPORT_DIR(내보내기, MD+Word).
    파일명: YYYYMMDD_HHMMSS_제목 (초 단위). 반환: (md_path, word_path 또는 None)"""
    target_dir = target_dir or EXPORT_DIR
    # source_numbers: 화면의 근거 번호 (AI 답변의 [n] 인용과 일치시키기 위해 그대로 사용)
    source_numbers = source_numbers or list(range(1, len(retrieved_docs) + 1))
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    if not answer_text.strip():
        answer_text = "(AI 검토 없음 — 검색된 원문 근거만 저장한 기록입니다.)"
    model_txt = analysis_model or "없음 (근거 검색만 수행)"
    safe_title = sanitize_filename(title_input if title_input.strip() else query[:35])
    base_filename = _unique_base(target_dir, f"{now.strftime('%Y%m%d_%H%M%S')}_{safe_title}")

    md_path = os.path.join(target_dir, f"{base_filename}.md")
    word_path = os.path.join(target_dir, f"{base_filename}.docx") if with_word else None

    # 1. 기록용 Markdown (.md) 생성 (AI Agent 입력에 최적화된 구조적 마크다운)
    md_lines = [
        f"# [EU MDR RA 검토 기록] {title_input.strip() or query[:50]}",
        "",
        f"- **검토 일시:** {now_str}",
        f"- **적용 규정 기준:** EU MDR (2017/745) Consolidated ({mdr_version})",
        f"- **검색 범위:** {search_scope}",
        f"- **검토 모델:** {model_txt}",
        "",
        "## 1. 질의 내용 (Query)",
        f"> {query}",
        "",
        "## 2. 규제 검토 및 해석 (AI Analysis)",
        answer_text,
        "",
        "## 3. 참조된 공식 원문 근거 (Reference Excerpts)",
    ]
    for idx, doc, meta in zip(source_numbers, retrieved_docs, retrieved_metas):
        doc_type_label = "EU MDR 법령" if meta.get("doc_type") == "EU_MDR" else "MDCG/EC 가이던스"
        md_lines.append(f"### [{idx}] {meta.get('title', '문서')} ({doc_type_label})")
        md_lines.append("```text")
        md_lines.append(doc)
        md_lines.append("```")
        md_lines.append("")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))
    if not with_word:  # 자동 저장은 MD만
        return md_path, None

    # 2. 배포용 Word (.docx) 생성 (보고서 스타일)
    doc_word = Document()

    # 기본 폰트 설정 (맑은 고딕)
    style_normal = doc_word.styles["Normal"]
    style_normal.font.name = "Malgun Gothic"
    style_normal.font.size = Pt(10.5)

    heading = doc_word.add_heading(f"EU MDR 규제 검토 보고서: {title_input.strip() or query[:45]}", level=1)
    for run in heading.runs:
        run.font.name = "Malgun Gothic"

    # 메타데이터 요약 표
    meta_rows = [
        ("검토 일시", now_str),
        ("적용 규정 버전", f"EU MDR (2017/745) Consolidated ({mdr_version})"),
        ("검색 범위", search_scope),
        ("검토 모델", model_txt),
    ]
    table = doc_word.add_table(rows=len(meta_rows), cols=2)
    table.style = "Table Grid"
    for row_idx, (k, v) in enumerate(meta_rows):
        table.cell(row_idx, 0).text = k
        table.cell(row_idx, 1).text = v

    doc_word.add_heading("1. 검토 질의 (Query)", level=2)
    p_q = doc_word.add_paragraph(query)
    p_q.runs[0].italic = True

    doc_word.add_heading("2. 규제 검토 의견 (Regulatory Assessment)", level=2)
    # 마크다운 기호(**, ### 등)를 깔끔하게 정리하여 Word 문단으로 반영
    for line in answer_text.split("\n"):
        clean_line = line.strip()
        if not clean_line:
            continue
        if clean_line.startswith("### "):
            doc_word.add_heading(clean_line.replace("### ", "").replace("**", ""), level=3)
        elif clean_line.startswith("## "):
            doc_word.add_heading(clean_line.replace("## ", "").replace("**", ""), level=2)
        elif clean_line.startswith(("- ", "* ")):
            doc_word.add_paragraph(clean_line[2:].replace("**", ""), style="List Bullet")
        else:
            doc_word.add_paragraph(clean_line.replace("**", ""))

    doc_word.add_heading("3. 참조된 공식 법령 및 가이던스 원문", level=2)
    for idx, doc_txt, meta in zip(source_numbers, retrieved_docs, retrieved_metas):
        p_ref_title = doc_word.add_paragraph()
        run_t = p_ref_title.add_run(f"[{idx}] {meta.get('title', '문서')}")
        run_t.bold = True
        run_t.font.color.rgb = RGBColor(0x1F, 0x4E, 0x79)

        p_ref_body = doc_word.add_paragraph(doc_txt)
        p_ref_body.paragraph_format.left_indent = Pt(12)
        for r in p_ref_body.runs:
            r.font.size = Pt(9)

    doc_word.save(word_path)
    return md_path, word_path


# --- 벡터 DB / 임베딩 ---
@st.cache_resource(show_spinner="검색 모델을 불러오는 중입니다. 최초 실행 시 모델을 내려받습니다.")
def get_query_embedder(model_name, normalize):
    # DB를 만든 모델과 같은 모델로 질의해야 하므로 (모델명, 정규화 여부)를 키로 캐시
    return get_embedder(model_name, normalize=normalize)


def load_collection():
    # 컬렉션 핸들은 매번 가져온다 (구축 직후 즉시 반영, 'DB 없음' 상태가 캐시되지 않도록)
    return get_collection()


# --- 검색: 식별자 직접 조회 + 의미 검색 ---
_ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII", "XIII", "XIV", "XV", "XVI", "XVII"]
MAX_PINNED = 4  # 질문에 명시된 조항/문서로 상단 고정할 최대 근거 수


def extract_identifiers(query):
    """질문에 명시된 Rule/Article/Annex/MDCG 식별자 추출 (한국어 표기 포함)."""
    q = query.replace(" ", " ")
    rules = re.findall(r"(?i)\b(?:rule|규칙)\s*(\d{1,2})\b", q) + re.findall(r"(\d{1,2})\s*번?\s*규칙", q)
    arts = re.findall(r"(?i)\b(?:article|art\.)\s*(\d{1,3}[a-z]?)\b", q) + re.findall(r"제\s*(\d{1,3})\s*조", q)
    annexes = [a.upper() for a in re.findall(r"(?i)\b(?:annex|부속서)\s*([IVX]{1,5})\b", q)]
    mdcg = [m.replace(" ", "") for m in re.findall(r"(?i)MDCG\s*(20\d{2}\s*-\s*\d{1,2})", q)]
    uniq = lambda xs: list(dict.fromkeys(xs))
    return {
        "rules": [r for r in uniq(rules) if 1 <= int(r) <= 22],
        "articles": [a for a in uniq(arts) if 1 <= int(re.match(r"\d+", a).group()) <= 123],
        "annexes": [a for a in uniq(annexes) if a in _ROMAN],
        "mdcg": uniq(mdcg),
    }


def _and_where(*conds):
    conds = [c for c in conds if c]
    if not conds:
        return None
    return conds[0] if len(conds) == 1 else {"$and": conds}


def retrieve(collection, query, top_k, where_filter, pdf_files):
    """
    1) 질문에 명시된 식별자(Rule 19, Article 10, Annex II, MDCG 2019-11)는 정확히 조회해 상단에 고정
    2) 나머지는 의미 검색(임베딩)으로 채움
    반환: [{"id", "doc", "meta", "pinned"}]
    """
    ids = extract_identifiers(query)
    scope_type = (where_filter or {}).get("doc_type")
    pinned = []

    def add_hits(res, limit):
        for i, d, m in zip(res["ids"], res["documents"], res["metadatas"]):
            if len(pinned) >= MAX_PINNED or limit <= 0:
                break
            if all(p["id"] != i for p in pinned):
                pinned.append({"id": i, "doc": d, "meta": m, "pinned": True})
                limit -= 1

    if scope_type in (None, "EU_MDR"):
        for n in ids["rules"]:
            add_hits(collection.get(where={"doc_type": "EU_MDR"},
                                    where_document={"$regex": rf"(?m)^(\d+\.\d+\.\s*)?Rule {n}$"},
                                    include=["documents", "metadatas"]), 1)
        for a in ids["articles"]:
            add_hits(collection.get(where=_and_where({"doc_type": "EU_MDR"}, {"section": f"Article {a}"}),
                                    include=["documents", "metadatas"], limit=2), 2)
        if not ids["rules"]:  # Rule이 지정되면 Annex 전체보다 해당 Rule이 더 구체적
            for r in ids["annexes"]:
                add_hits(collection.get(where=_and_where({"doc_type": "EU_MDR"}, {"section": f"ANNEX {r}"}),
                                        include=["documents", "metadatas"], limit=1), 1)

    embed_name, normalize = query_embedder_spec()
    qvec = get_query_embedder(embed_name, normalize).encode_query([query])

    if scope_type in (None, "MDCG_GUIDANCE"):
        for code in ids["mdcg"]:
            prefix = "MDCG_" + code + "_"
            sources = [os.path.splitext(p)[0] for p in pdf_files if p.upper().startswith(prefix.upper())]
            if sources:  # 해당 MDCG 문서 안에서 질문과 가장 관련 있는 페이지
                res = collection.query(query_embeddings=qvec, n_results=2,
                                       where={"source_name": {"$in": sources}})
                add_hits({k: res[k][0] for k in ("ids", "documents", "metadatas")}, 2)

    res = collection.query(query_embeddings=qvec, n_results=top_k + len(pinned), where=where_filter)
    results = list(pinned)
    for i, d, m in zip(res["ids"][0], res["documents"][0], res["metadatas"][0]):
        if len(results) >= max(top_k, len(pinned)):
            break
        if all(r["id"] != i for r in results):
            results.append({"id": i, "doc": d, "meta": m, "pinned": False})
    return results


# --- 최신 상태 점검 (HTTP 조회만, LLM 토큰 0) ---
def _state_signature():
    """로컬 상태가 바뀌면(구축 완료, PDF 추가 등) 캐시를 무효화하기 위한 서명."""
    meta = load_meta()
    return json.dumps(
        {"v": meta.get("version_date"), "b": meta.get("built_at"), "pdfs": list_local_pdfs()},
        ensure_ascii=False,
    )


@st.cache_data(ttl=UPDATE_CHECK_TTL_SEC, show_spinner=False)
def cached_update_check(signature):  # 밑줄로 시작하면 캐시 키에서 제외되므로 일반 이름 사용
    return check_for_updates()


REASON_TAG = {
    "missing": ("miss", "누락"),
    "revision": ("rev", "개정판"),
    "unverified": ("info", "버전 재확인"),
}


def overall_status(check):
    """(점 색상 클래스, 짧은 상태 문구)"""
    if check["needs_build"]:
        return "err", "구축 필요"
    if not check["ok"]:
        return "warn", "최신 여부 확인 불가"
    if check["has_updates"]:
        return "warn", "업데이트 있음"
    return "ok", "최신 상태"


def run_build(check, include_new):
    """원클릭 구축/업데이트 실행. 실패해도 기존 DB는 유지된다."""
    with st.status("최신 문서를 내려받고 검색 DB를 구축하는 중입니다. 창을 닫지 마세요.", expanded=True) as status:
        bar = st.progress(0.0, text="준비 중")

        def log(msg):
            st.write(msg)

        def progress(frac, msg):
            bar.progress(min(max(frac, 0.0), 1.0), text=msg)

        try:
            summary = build_database(check=check, include_new=include_new, log=log, progress=progress)
        except BuildError as e:
            status.update(label="업데이트 실패 — 기존 DB는 그대로 유지됩니다", state="error", expanded=True)
            st.error(str(e), icon=":material/error:")
            return False
        status.update(label="구축 완료", state="complete", expanded=False)

    st.session_state["build_summary"] = summary
    st.session_state.pop("dismiss_update", None)
    cached_update_check.clear()
    st.rerun()
    return True


def render_update_panel(check):
    """메인 화면 상단의 점검 결과 + 원클릭 버튼. DB가 없으면 True(검색 화면 중단) 반환."""
    m, g = check["mdr"], check["guidance"]

    # 직전 구축 결과 알림 (1회)
    summary = st.session_state.pop("build_summary", None)
    if summary:
        st.success(
            f"구축 완료 · EU MDR **{summary['version_date']}** ({summary['mdr_chunks']}개 섹션) · "
            f"가이던스 PDF **{summary['pdf_count']}종** ({summary['pdf_chunks']}개 섹션)",
            icon=":material/task_alt:",
        )
        problems = [f"다운로드 실패: {f['filename']}" for f in summary["failed_downloads"]]
        problems += [f"파싱 실패: {f['filename']}" for f in summary["failed_parse"]]
        if problems:
            st.warning("일부 문서를 처리하지 못했습니다. 다음 점검 때 다시 시도됩니다.\n\n- " + "\n- ".join(problems),
                       icon=":material/warning:")

    if g.get("unmatched"):
        st.caption("EC 페이지에서 찾지 못한 관리 문서 (기존 파일 유지): " + ", ".join(g["unmatched"]))

    # 1) 백지 상태: 구축 필요
    if check["needs_build"]:
        with st.container(border=True):
            st.html(
                '<div class="ra-panel-title">검색 데이터 초기 구축</div>'
                '<div class="ra-panel-desc">처음 실행하셨습니다. 공식 출처에서 최신 문서를 내려받아 로컬 검색 DB를 만듭니다.</div>'
            )
            if not check["ok"]:
                st.error(
                    "공식 서버에서 최신 문서 정보를 확인하지 못했습니다. 인터넷 또는 사내 프록시 연결을 확인한 뒤 다시 시도하세요.\n\n"
                    + "\n\n".join(e for e in (m["error"], g["error"]) if e),
                    icon=":material/cloud_off:",
                )
                if st.button("다시 확인", type="primary", icon=":material/refresh:"):
                    cached_update_check.clear()
                    st.rerun()
                return True
            st.html(
                '<ul class="ra-list">'
                f'<li><span class="ra-tag law">EU MDR</span>최신 통합본 <b>{esc(m["latest"])}</b> · EU 출판국 공식 저장소</li>'
                f'<li><span class="ra-tag guide">MDCG</span>공식 가이던스 PDF <b>{len(g["updated_items"])}건</b> · European Commission</li>'
                f'<li><span class="ra-tag info">DB</span>로컬 검색 DB 구축 · 최초 1회 검색 모델 다운로드'
                f'({esc(EMBED_MODELS.get(EMBED_MODEL, {}).get("size", ""))}) 포함, 보통 10~20분</li>'
                '</ul>'
            )
            if st.button("구축 시작", type="primary", icon=":material/rocket_launch:", use_container_width=True):
                run_build(check, include_new=True)
        return True

    # 2) 원격 확인 실패: 기존 DB로 계속 사용
    if not check["ok"]:
        st.warning(
            f"최신 여부를 확인하지 못했습니다. 현재 DB(EU MDR 기준일 {m['local']})로 계속 사용합니다.\n\n"
            + "\n\n".join(e for e in (m["error"], g["error"]) if e),
            icon=":material/cloud_off:",
        )
        return False

    # 3) 업데이트 있음: 원클릭 업데이트 제안
    if check["has_updates"] and not st.session_state.get("dismiss_update"):
        with st.container(border=True):
            st.html(
                '<div class="ra-panel-title">최신 문서 업데이트가 있습니다</div>'
                '<div class="ra-panel-desc">최신 법령과 가이던스를 기준으로 검토하려면 업데이트하세요. 실패해도 기존 DB는 유지됩니다.</div>'
            )
            items = []
            if m["changed"]:
                items.append(f'<li><span class="ra-tag law">EU MDR</span>개정 통합본 '
                             f'{esc(m["local"] or "없음")} → <b>{esc(m["latest"])}</b></li>')
            for it in g["updated_items"]:
                cls, label = REASON_TAG.get(it["reason"], ("info", it["reason"]))
                items.append(f'<li><span class="ra-tag {cls}">{label}</span>{esc(it["filename"])}</li>')
            for d in g["new_mdcg_docs"]:
                items.append(f'<li><span class="ra-tag new">신규</span><b>{esc(d["code"])}</b>&nbsp;{esc(d["title"])}</li>')
            if check["local_pdf_changed"]:
                items.append('<li><span class="ra-tag info">로컬</span>mdcg_pdfs 폴더의 PDF 구성이 변경됨 (재색인 필요)</li>')
            if check.get("parser_outdated"):
                items.append('<li><span class="ra-tag info">개선</span>검색 정확도 개선이 있습니다 '
                             f'(문서 처리 방식·검색 모델 변경, 재색인 필요 · 최초 1회 모델 '
                             f'{esc(EMBED_MODELS.get(EMBED_MODEL, {}).get("size", ""))} 다운로드, 약 10~20분)</li>')
            st.html(f'<ul class="ra-list">{"".join(items)}</ul>')
            include_new = True
            if g["new_mdcg_docs"]:
                include_new = st.checkbox("신규 발행된 MDCG 가이던스도 함께 추가", value=True)
            c1, c2 = st.columns([3, 1])
            if c1.button("지금 업데이트", type="primary", icon=":material/download:", use_container_width=True):
                run_build(check, include_new=include_new)
            if c2.button("나중에", use_container_width=True):
                st.session_state["dismiss_update"] = True
                st.rerun()
    return False


# ---------------------------------------------------------------------------
# 앱 시작 시 최신 상태 자동 점검
# ---------------------------------------------------------------------------
with st.spinner("공식 출처에서 문서 최신 상태를 확인하는 중입니다. (Gemini 토큰 사용 없음)"):
    update_check = cached_update_check(_state_signature())

meta = load_meta()
local_version = meta.get("version_date", "미구축")
pdf_list = meta.get("pdf_files", [])

# ---------------------------------------------------------------------------
# 사이드바
# ---------------------------------------------------------------------------
with st.sidebar:
    st.html('<div class="ra-label">AI 모델</div>')
    env_now = read_env()

    # 기본 연결 방식: 저장된 선택 → (이전 설정) Vertex 환경변수 → Gemini CLI 설치 시 CLI → API 키
    saved_provider = env_now.get("LLM_PROVIDER", "")
    if saved_provider not in PROVIDERS:
        if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").strip().lower() in ("1", "true", "yes"):
            saved_provider = "vertex"
        elif gemini_cli_path():
            saved_provider = "gemini_cli"
        else:
            saved_provider = "gemini"
    provider_keys = list(PROVIDERS.keys())
    provider = st.selectbox(
        "AI 연결 방식",
        options=provider_keys,
        index=provider_keys.index(saved_provider),
        format_func=lambda k: PROVIDERS[k],
        help="회사 계약상 API 키를 쓸 수 없으면 '회사 계정' 방식을 사용하세요. Gemini CLI·Vertex AI 중 회사에서 "
             "동작하는 쪽을 '연결 테스트'로 확인할 수 있습니다. 선택은 자동 저장됩니다.",
    )
    if provider != env_now.get("LLM_PROVIDER"):
        write_env_value("LLM_PROVIDER", provider)

    ai_settings = {"gemini_key": "", "openai_key": "", "openai_model": "", "vertex_project": "",
                   "vertex_location": "", "cli_model": ""}
    llm_ready = False

    def _project_input(help_text):
        saved = env_now.get("GOOGLE_CLOUD_PROJECT", os.environ.get("GOOGLE_CLOUD_PROJECT", ""))
        value = st.text_input("Google Cloud 프로젝트 ID", value=saved, placeholder="회사에서 안내한 프로젝트 ID",
                              help=help_text).strip()
        if value != saved:
            write_env_value("GOOGLE_CLOUD_PROJECT", value)
        os.environ["GOOGLE_CLOUD_PROJECT"] = value
        return value

    if provider == "gemini_cli":
        cli = gemini_cli_path()
        if cli:
            auth = gemini_cli_auth_type()
            auth_txt = CLI_AUTH_LABEL.get(auth, auth or "미설정 (로그인 필요)")
            st.html('<dl class="ra-kv">'
                    f'<dt>Gemini CLI</dt><dd><span class="ra-dot ok"></span>&nbsp;설치됨</dd>'
                    f'<dt>인증 방식</dt><dd>{esc(auth_txt)}</dd>'
                    '</dl>')
            cli_models = list_gemini_cli_models(cli)
            saved_cli_model = env_now.get("GEMINI_CLI_MODEL", "")
            MANUAL = "직접 입력"
            # CLI 목록이 계정별 사용 가능 여부와 다를 수 있어(예: 목록에 있지만 사용 불가) 직접 입력도 허용
            options = cli_models + [MANUAL]
            if saved_cli_model and saved_cli_model not in cli_models:
                idx = options.index(MANUAL)
            else:
                idx = cli_models.index(saved_cli_model) if saved_cli_model in cli_models else 0
            if not cli_models:
                st.caption("모델 목록을 불러오지 못했습니다. 로그인 상태를 확인하거나 모델명을 직접 입력하세요.")
            choice = st.selectbox(
                "모델", options=options, index=idx,
                help="Gemini CLI가 알려 준 모델 중 고성능 모델만 표시합니다 (auto·lite 제외). 최신 버전이 맨 위입니다. "
                     "목록의 모델을 쓸 수 없다면 '직접 입력'으로 회사에서 안내한 모델명을 넣으세요.")
            if choice == MANUAL:
                ai_settings["cli_model"] = st.text_input(
                    "모델명", value=saved_cli_model if saved_cli_model not in cli_models else "",
                    placeholder="예: gemini-3-flash-preview").strip()
                if "lite" in ai_settings["cli_model"].lower():
                    st.warning("경량(lite) 모델은 검토 품질 기준상 사용할 수 없습니다.", icon=":material/block:")
                    ai_settings["cli_model"] = ""
            else:
                ai_settings["cli_model"] = choice
            if ai_settings["cli_model"] and ai_settings["cli_model"] != saved_cli_model:
                write_env_value("GEMINI_CLI_MODEL", ai_settings["cli_model"])
            _project_input("회사 계정으로 Gemini CLI를 쓸 때 GOOGLE_CLOUD_PROJECT 설정이 필요하면 입력하세요. "
                           "필요 없으면 비워 두세요.")
            llm_ready = bool(ai_settings["cli_model"])
        else:
            st.html('<div class="ra-muted"><span class="ra-dot err"></span>&nbsp;Gemini CLI가 설치되어 있지 않습니다.</div>')
        with st.expander("Gemini CLI 설정 방법", icon=":material/help:"):
            st.markdown(
                "1. Node.js 설치 후 터미널에서 설치: `npm install -g @google/gemini-cli`\n"
                "2. 회사에서 안내한 경우 프로젝트 ID를 위 칸에 입력\n"
                "3. 터미널에서 `gemini` 실행 → 인증 방식에서 **Login with Google** 선택\n"
                "4. 브라우저에서 회사 계정으로 로그인(SSO) → 완료 후 이 화면의 **연결 테스트**")

    elif provider == "vertex":
        ai_settings["vertex_project"] = _project_input("Vertex AI를 사용할 회사 Google Cloud 프로젝트 ID입니다.")
        with st.expander("고급 설정", icon=":material/tune:"):
            saved_loc = env_now.get("GOOGLE_CLOUD_LOCATION", "") or DEFAULT_VERTEX_LOCATION
            loc = st.text_input("리전", value=saved_loc,
                                help="따로 안내받지 않았다면 global 그대로 두세요.").strip() or DEFAULT_VERTEX_LOCATION
            if loc != saved_loc:
                write_env_value("GOOGLE_CLOUD_LOCATION", loc)
        ai_settings["vertex_location"] = loc
        has_adc = vertex_credentials_status()
        has_gcloud = bool(shutil.which("gcloud"))
        st.html('<dl class="ra-kv">'
                f'<dt>gcloud</dt><dd><span class="ra-dot {"ok" if has_gcloud else "err"}"></span>&nbsp;'
                f'{"설치됨" if has_gcloud else "미설치"}</dd>'
                f'<dt>로그인</dt><dd><span class="ra-dot {"ok" if has_adc else "err"}"></span>&nbsp;'
                f'{"확인됨" if has_adc else "필요"}</dd>'
                '</dl>')
        with st.expander("Vertex AI 설정 방법", icon=":material/help:"):
            st.markdown(
                "1. Google Cloud CLI(gcloud) 설치 — 회사 PC 설치 가능 여부는 IT에 확인\n"
                "2. 터미널에서 실행: `gcloud auth application-default login`\n"
                "3. 브라우저에서 회사 계정으로 로그인(SSO)\n"
                "4. 위에 프로젝트 ID 입력 → **연결 테스트**\n\n"
                "권한 없음 오류가 나오면 회사 계약이 Gemini CLI 방식일 가능성이 높습니다.")
        llm_ready = bool(ai_settings["vertex_project"]) and has_adc

    elif provider == "gemini":
        fallback_txt = ", ".join(GEMINI_MODELS[1:]) or "없음"
        st.html(
            '<dl class="ra-kv">'
            f'<dt>기본</dt><dd><code>{esc(GEMINI_MODELS[0])}</code></dd>'
            f'<dt>폴백</dt><dd><code>{esc(fallback_txt)}</code></dd>'
            '</dl>'
        )
        saved_key = env_now.get("GEMINI_API_KEY", os.environ.get("GEMINI_API_KEY", ""))
        ai_settings["gemini_key"] = st.text_input(
            "Gemini API Key",
            value=saved_key,
            type="password",
            placeholder="AIza… 형식의 키 입력",
            help="한 번 입력하면 이 폴더의 .env 파일에 저장되어 다음 실행 시 자동으로 입력됩니다."
        )
        if ai_settings["gemini_key"] and ai_settings["gemini_key"] != saved_key:
            write_env_value("GEMINI_API_KEY", ai_settings["gemini_key"])
            st.toast("Gemini API Key를 저장했습니다.", icon=":material/key:")
        llm_ready = bool(ai_settings["gemini_key"])

    else:  # openai
        saved_oa_key = env_now.get("OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
        ai_settings["openai_key"] = st.text_input(
            "OpenAI API Key",
            value=saved_oa_key,
            type="password",
            placeholder="sk-… 형식의 키 입력",
            help="platform.openai.com 에서 발급한 API 키입니다. ChatGPT 구독과는 별개이며, "
                 "한 번 입력하면 이 폴더의 .env 파일에 저장됩니다."
        )
        if ai_settings["openai_key"] and ai_settings["openai_key"] != saved_oa_key:
            write_env_value("OPENAI_API_KEY", ai_settings["openai_key"])
            st.toast("OpenAI API Key를 저장했습니다.", icon=":material/key:")
        if ai_settings["openai_key"]:
            models = list_openai_models(ai_settings["openai_key"])
            saved_model = env_now.get("OPENAI_MODEL", "")
            if models:
                idx = models.index(saved_model) if saved_model in models else 0
                ai_settings["openai_model"] = st.selectbox(
                    "모델", options=models, index=idx,
                    help="이 키로 사용 가능한 범용 상위 모델만 표시합니다 (mini·nano 등 경량 모델 제외). 최신 버전이 맨 위입니다."
                )
            else:
                st.caption("모델 목록을 불러오지 못했습니다. 키를 확인하거나 모델명을 직접 입력하세요.")
                ai_settings["openai_model"] = st.text_input("모델", value=saved_model, placeholder="예: gpt-5")
            if ai_settings["openai_model"] and ai_settings["openai_model"] != saved_model:
                write_env_value("OPENAI_MODEL", ai_settings["openai_model"])
        llm_ready = bool(ai_settings["openai_key"] and ai_settings["openai_model"])

    # 연결 테스트 (짧은 요청 1회) — 회사 계정 방식이 실제로 동작하는지 현장에서 판별
    if st.button("연결 테스트", icon=":material/network_check:", use_container_width=True, disabled=not llm_ready,
                 help="아주 짧은 요청을 한 번 보내 이 연결 방식이 실제로 동작하는지 확인합니다."):
        with st.spinner("연결을 확인하는 중입니다."):
            st.session_state["conn_test"] = (provider, *test_ai_connection(provider, ai_settings))
    conn = st.session_state.get("conn_test")
    if conn and conn[0] == provider:
        (st.success if conn[1] else st.error)(conn[2], icon=":material/check_circle:" if conn[1] else ":material/error:")

    st.divider()
    st.html('<div class="ra-label">데이터베이스</div>')
    dot, status_txt = overall_status(update_check)
    if update_check["db_ready"]:
        st.html(
            '<dl class="ra-kv">'
            f'<dt>EU MDR 기준일</dt><dd>{esc(local_version)}</dd>'
            f'<dt>법령 섹션</dt><dd>{meta.get("mdr_chunks", 0):,}</dd>'
            f'<dt>가이던스 PDF</dt><dd>{len(pdf_list)}종 · {meta.get("pdf_chunks", 0):,}</dd>'
            f'<dt>상태</dt><dd><span class="ra-dot {dot}"></span>&nbsp;{esc(status_txt)}</dd>'
            '</dl>'
            f'<div class="ra-muted">마지막 점검 {esc(update_check["checked_at"])}</div>'
        )
        if pdf_list:
            with st.expander("반영된 가이던스 문서", icon=":material/folder_open:"):
                for pdf_name in pdf_list:
                    st.caption(pdf_name)
    else:
        st.html(f'<div class="ra-muted"><span class="ra-dot err"></span>&nbsp;아직 구축되지 않았습니다. '
                f'메인 화면에서 구축을 시작하세요.</div>')

    c1, c2 = st.columns(2)
    if c1.button("다시 확인", icon=":material/refresh:", use_container_width=True):
        cached_update_check.clear()
        st.session_state.pop("dismiss_update", None)
        st.rerun()
    if update_check["db_ready"] and c2.button("재구축", icon=":material/build:", use_container_width=True,
                                              help="변경 사항이 없어도 모든 문서를 다시 색인합니다."):
        st.session_state["force_rebuild"] = True
        st.rerun()

    st.divider()
    st.html('<div class="ra-label">검색 설정</div>')
    # 근거 문서 수는 .env(TOP_K)에 저장해 재시작 후에도 유지
    try:
        saved_top_k = min(max(int(env_now.get("TOP_K", 6)), 3), 12)
    except ValueError:
        saved_top_k = 6
    top_k = st.slider("근거 문서 수", min_value=3, max_value=12, value=saved_top_k)
    if top_k != saved_top_k:
        write_env_value("TOP_K", str(top_k))

    st.divider()
    st.html('<div class="ra-label">저장된 검토 기록</div>')
    # 자동 저장(saved)과 내보낸 기록(exported)을 함께 최신순으로 표시. 내보낸 기록은 [내보냄] 표시
    record_files = {}
    for path in glob.glob(os.path.join(SAVED_DIR, "*.md")):
        record_files[os.path.basename(path)] = path
    for path in glob.glob(os.path.join(EXPORT_DIR, "*.md")):
        record_files[f"[내보냄] {os.path.basename(path)}"] = path
    record_labels = sorted(record_files, key=lambda k: os.path.getmtime(record_files[k]), reverse=True)
    NONE_NOTE = "선택 안 함 (검색 화면)"
    if record_labels:
        selected_note = st.selectbox("기록 열람", options=[NONE_NOTE] + record_labels,
                                     label_visibility="collapsed",
                                     help="AI 검토가 끝나면 자동 저장됩니다. [내보냄]은 MD+Word로 내보낸 기록입니다.")
    else:
        selected_note = NONE_NOTE
        st.caption("아직 저장된 기록이 없습니다. AI 검토가 끝나면 자동으로 저장됩니다.")

    st.divider()
    with st.popover("도움말", icon=":material/help:"):
        st.markdown(
            "**화면 테마**  \n오른쪽 위 **⋮** 메뉴에서 System · Light · Dark 를 선택할 수 있습니다."
        )

# ---------------------------------------------------------------------------
# 메인 화면
# ---------------------------------------------------------------------------
dot, status_txt = overall_status(update_check)
chips = [f'<span class="ra-chip"><span class="ra-dot {dot}"></span>{esc(status_txt)}</span>']
if update_check["db_ready"]:
    chips.append(f'<span class="ra-chip">EU MDR 기준일 <b>{esc(local_version)}</b></span>')
    chips.append(f'<span class="ra-chip">가이던스 <b>{len(pdf_list)}종</b></span>')
st.html(
    '<div class="ra-hero">'
    '<div class="ra-hero-brand">'
    f'{LOGO_SVG}'
    '<div>'
    '<div class="ra-eyebrow">EU MDR 2017/745 · MDCG Guidance</div>'
    '<div class="ra-title">규제 검토 어시스턴트</div>'
    '<p class="ra-sub">EU MDR 공식 통합본과 MDCG 가이던스를 교차 검색하고, 원문 근거와 함께 한국어로 해석합니다.</p>'
    '</div>'
    '</div>'
    f'<div class="ra-chips">{"".join(chips)}</div>'
    '</div>'
)

if st.session_state.pop("force_rebuild", False):
    if update_check["ok"]:
        run_build(update_check, include_new=True)
    else:
        st.error("최신 정보를 확인할 수 없어 재구축을 시작하지 않았습니다. 네트워크 확인 후 다시 시도하세요.",
                 icon=":material/cloud_off:")

if render_update_panel(update_check):
    st.stop()

# 사이드바에서 과거 기록을 선택한 경우 해당 기록 열람 화면 표시
if selected_note != NONE_NOTE:
    st.info(f"저장된 검토 기록 **{selected_note}** 을(를) 보고 있습니다. 검색 화면으로 돌아가려면 사이드바에서 "
            f"'{NONE_NOTE}'을 선택하세요.", icon=":material/history:")
    with st.container(border=True):
        with open(record_files[selected_note], "r", encoding="utf-8") as f:
            st.markdown(f.read())
    st.stop()

collection = load_collection()
if collection is None:
    st.error("로컬 검색 DB를 열 수 없습니다. 사이드바에서 '다시 확인'을 누른 뒤 구축을 진행하세요.", icon=":material/error:")
    st.stop()

# --- 질의 입력 ---
with st.container(border=True):
    with st.form("search_form", clear_on_submit=False, border=False):
        scope_key = st.segmented_control("검색 범위", options=list(SCOPES.keys()), default="전체",
                                         selection_mode="single")
        query = st.text_area(
            "질문",
            height=96,
            placeholder="예: X-ray 장비의 등급 분류(Rule 10)와 소프트웨어(Rule 11) 적용 기준을 MDR과 MDCG 가이던스 근거로 설명해 줘",
            help="Ctrl+Enter로 '검색 + AI 검토'를 실행합니다.",
        )
        b1, b2, _sp = st.columns([1.25, 1, 2.6])
        # 주 기능(AI 검토)을 첫 번째·강조 버튼으로 배치. Ctrl+Enter는 첫 번째 버튼(검색 + AI 검토)으로 실행된다
        do_both = b1.form_submit_button("검색 + AI 검토", type="primary", icon=":material/auto_awesome:",
                                        use_container_width=True)
        do_search = b2.form_submit_button("근거 검색", icon=":material/search:",
                                          use_container_width=True)

search_scope, where_filter = SCOPES[scope_key or "전체"]


def build_prompt(query_text, numbered_items):
    """numbered_items: [(화면 번호, 결과)] — 화면의 근거 번호와 같은 번호로 인용하도록 한다."""
    context_text = "\n\n---\n\n".join(f"[{n}] {it['doc']}" for n, it in numbered_items)
    return f"""당신은 유럽 의료기기 규정(EU MDR 2017/745) 및 MDCG 가이던스 전문 Regulatory Affairs(RA) 컨설턴트입니다.
아래 제공된 [EU MDR 법령 및 MDCG 가이던스 원문 발췌]만을 근거로 사용자의 질문에 정확하고 실무적으로 답변하세요.

[작성 원칙]
1. 법적 구속력이 있는 **EU MDR 조항**(예: Article 10, Annex I 11.1, Annex VIII Rule 10 등)과 실무 해석 기준인 **MDCG 가이던스 출처**(예: MDCG 2021-24, MDCG 2019-11, Language requirements 등 및 페이지 번호)를 구분하여 명확히 인용하세요.
2. 원문에 없는 내용은 추측하지 말고, 제공된 발췌문 범위 내에서 설명하세요. 발췌문으로 답할 수 없는 부분은 "제공된 원문에서 확인되지 않음"이라고 명시하세요.
3. 실무자가 바로 업무(기술문서 작성, 등급 판정, 번역 범위 산정, 변경관리 등)에 적용할 수 있도록 조건·예외 사항을 구조화해 한국어로 설명하고, 핵심 영문 원문 용어는 괄호 안에 병기하세요.
4. 근거를 인용할 때는 발췌문 앞의 번호를 [1], [3]처럼 함께 표기하세요.

[EU MDR 법령 및 MDCG 가이던스 원문 발췌]
{context_text}

[사용자 질문]
{query_text}
"""


def run_analysis(qa_state, selected_idx):
    """선택한 근거만으로 AI 검토. 결과는 qa_state에 기록."""
    items = [(i + 1, qa_state["results"][i]) for i in selected_idx]
    qa_state["error"] = ""
    with st.spinner(f"선택한 근거 {len(items)}건으로 {PROVIDERS[provider]} 검토 중입니다."):
        try:
            t0 = time.perf_counter()
            answer, model_used = generate_answer(provider, build_prompt(qa_state["query"], items), ai_settings)
            # 요청한 모델(폴백 여부 표시용): CLI는 선택 모델, Gemini API·Vertex는 기본 모델, OpenAI는 선택 모델
            requested = {"gemini_cli": ai_settings["cli_model"], "openai": ai_settings["openai_model"]}.get(
                provider, GEMINI_MODELS[0])
            qa_state.update(answer=answer, model=model_used, model_requested=requested, provider=provider,
                            used=list(selected_idx),
                            analyzed_at=datetime.now().strftime("%H:%M:%S"),
                            analysis_sec=time.perf_counter() - t0, saved_path="", save_error="")
        except Exception as e:
            hint = " 사이드바에서 다른 AI 제공사로 전환해 다시 시도할 수 있습니다." if _is_transient(e) else ""
            qa_state["error"] = f"{PROVIDERS[provider]} 호출 오류: {e}{hint}"
            return

    # AI 검토가 성공하면 자동 저장 (saved/). 저장에 실패해도 검토 결과 표시는 유지한다.
    if qa_state["answer"].strip():
        try:
            md_path, _ = save_review_files(
                title_input="",
                query=qa_state["query"],
                answer_text=qa_state["answer"],
                retrieved_docs=[qa_state["results"][i]["doc"] for i in selected_idx],
                retrieved_metas=[qa_state["results"][i]["meta"] for i in selected_idx],
                mdr_version=local_version,
                search_scope=qa_state["scope"],
                analysis_model=f"{PROVIDERS.get(provider, provider)} · {qa_state['model']}",
                source_numbers=[i + 1 for i in selected_idx],
                target_dir=SAVED_DIR,
                with_word=False,
            )
            qa_state["saved_path"] = md_path
        except Exception as e:
            qa_state["save_error"] = f"자동 저장 실패: {e}"


# 검색 실행 시 결과를 session_state에 보관 (저장·검토 버튼을 눌러도 화면이 유지되도록)
if (do_search or do_both) and query.strip():
    with st.spinner("관련 EU MDR 조항과 MDCG 가이던스를 검색하는 중입니다."):
        t0 = time.perf_counter()
        found = retrieve(collection, query, top_k, where_filter, pdf_list)
        search_sec = time.perf_counter() - t0
    st.session_state["qa_nonce"] = st.session_state.get("qa_nonce", 0) + 1
    st.session_state["last_qa"] = {
        "query": query, "scope": search_scope, "results": found, "nonce": st.session_state["qa_nonce"],
        "searched_at": datetime.now().strftime("%H:%M:%S"), "search_sec": search_sec,
        "answer": "", "model": "", "provider": "", "used": [], "error": "",
    }
    if do_both and llm_ready and found:
        run_analysis(st.session_state["last_qa"], list(range(len(found))))
        if st.session_state["last_qa"].get("saved_path"):
            st.rerun()  # 사이드바의 저장 기록 목록에 방금 자동 저장한 기록을 바로 반영

# --- 검색 결과 / 검토 / 저장 ---
qa = st.session_state.get("last_qa")
if qa and "results" not in qa:  # 이전 버전 형식의 세션 상태는 무시
    qa = None
if qa:
    results = qa["results"]
    nonce = qa["nonce"]
    st.write("")
    col1, col2 = st.columns([1.3, 1], gap="large")

    with col2:
        n_pinned = sum(1 for r in results if r["pinned"])
        st.html(f'<div class="ra-label">원문 근거 · {len(results)}건</div>')
        if qa.get("searched_at"):
            st.caption(f":material/schedule: {qa['searched_at']} 검색 · {qa['search_sec']:.1f}초 · 근거 {len(results)}건")
        if not results:
            st.warning("선택한 범위에서 검색된 문서가 없습니다.", icon=":material/search_off:")
        else:
            st.caption("체크한 근거만 AI 검토와 저장에 사용됩니다."
                       + (f" 📌 표시는 질문에 적힌 조항·문서를 직접 찾은 결과입니다." if n_pinned else ""))
        for idx, r in enumerate(results):
            meta_item = r["meta"]
            is_law = meta_item.get("doc_type") == "EU_MDR"
            kind = "법령" if is_law else "가이던스"
            pin = "📌 " if r["pinned"] else ""
            c_chk, c_exp = st.columns([1, 13], vertical_alignment="top")
            c_chk.checkbox("검토에 사용", value=True, key=f"use_{nonce}_{idx}", label_visibility="collapsed")
            with c_exp.expander(f"{pin}[{idx + 1}] {kind} · {meta_item.get('title', '문서')}",
                                expanded=(idx < 2),
                                icon=":material/gavel:" if is_law else ":material/description:"):
                st.text(r["doc"])
        selected = [i for i in range(len(results)) if st.session_state.get(f"use_{nonce}_{i}", True)]

    with col1:
        st.html('<div class="ra-label">검토 답변</div>')
        if qa.get("error"):
            st.error(qa["error"], icon=":material/error:")

        if qa["answer"]:
            with st.container(border=True):
                used = qa.get("model", "")
                used_provider = qa.get("provider", "gemini")
                label = f"{PROVIDERS.get(used_provider, used_provider)} · {used}"
                if qa.get("analyzed_at"):
                    label += f" · {qa['analyzed_at']} 검토 · {qa['analysis_sec']:.1f}초"
                requested = qa.get("model_requested", "")
                if used and requested and used != requested:
                    st.html(f'<div class="ra-answer-head"><span class="ra-chip"><span class="ra-dot warn"></span>'
                            f'{esc(label)} · 요청 모델 {esc(requested)} 대신 응답됨</span></div>')
                elif used:
                    st.html(f'<div class="ra-answer-head"><span class="ra-chip"><span class="ra-dot ok"></span>'
                            f'{esc(label)}</span></div>')
                st.markdown(qa["answer"])
                st.caption("검토에 사용한 근거: " + ", ".join(f"[{i + 1}]" for i in qa["used"]))
                if qa.get("saved_path"):
                    st.caption(f":material/check_circle: 자동 저장됨 · `{os.path.basename(qa['saved_path'])}`")
                elif qa.get("save_error"):
                    st.warning(qa["save_error"], icon=":material/warning:")
            if llm_ready and selected and selected != qa["used"]:
                if st.button(f"선택한 근거 {len(selected)}건으로 다시 검토", icon=":material/refresh:"):
                    run_analysis(qa, selected)
                    st.rerun()
        elif results:
            with st.container(border=True):
                st.html('<div class="ra-panel-title">AI 검토</div>'
                        '<div class="ra-panel-desc">오른쪽 근거를 확인하고, 관련 없는 근거는 체크를 해제한 뒤 검토를 요청하세요. '
                        '검토 의견은 체크한 근거만으로 작성됩니다.</div>')
                if not llm_ready:
                    st.info(f"사이드바의 AI 모델에서 '{PROVIDERS[provider]}' 연결을 설정하면 AI 검토를 받을 수 있습니다.",
                            icon=":material/key:")
                elif st.button(f"선택한 근거 {len(selected)}건으로 AI 검토", type="primary",
                               icon=":material/auto_awesome:", disabled=not selected, use_container_width=True):
                    run_analysis(qa, selected)
                    st.rerun()

        # --- 내보내기 (자동 저장과 별개로, 배포·지식 자료용 MD+Word를 exported/에 생성. 검토 없이 근거만도 가능) ---
        if results:
            save_idx = qa["used"] if qa["answer"] else selected
            with st.container(border=True):
                desc = ("AI 검토 의견과 검토에 사용한 근거를" if qa["answer"] else "체크한 원문 근거를") \
                       + " 팀 배포·지식 자료용 Markdown과 Word 파일로 내보냅니다."
                if qa["answer"]:
                    desc += " 검토 기록 자체는 이미 자동 저장되어 있습니다."
                st.html(f'<div class="ra-panel-title">검토 결과 내보내기</div><div class="ra-panel-desc">{desc}</div>')
                custom_title = st.text_input(
                    "문서 제목",
                    value="",
                    placeholder="비워 두면 질문 내용으로 자동 지정 · 예: X-ray_장비_등급분류_및_SW_적용기준",
                    key=f"title_{nonce}",
                )
                if st.button("MD + Word 내보내기", type="primary", icon=":material/ios_share:", disabled=not save_idx,
                             key=f"save_{nonce}"):
                    model_label = f"{PROVIDERS.get(qa['provider'], qa['provider'])} · {qa['model']}" \
                        if qa["answer"] else ""
                    md_saved_path, word_saved_path = save_review_files(
                        title_input=custom_title,
                        query=qa["query"],
                        answer_text=qa["answer"],
                        retrieved_docs=[results[i]["doc"] for i in save_idx],
                        retrieved_metas=[results[i]["meta"] for i in save_idx],
                        mdr_version=local_version,
                        search_scope=qa["scope"],
                        analysis_model=model_label,
                        source_numbers=[i + 1 for i in save_idx],
                        target_dir=EXPORT_DIR,
                        with_word=True,
                    )
                    # 사이드바(저장된 검토 기록)는 이미 그려졌으므로, 메시지를 남기고 다시 그려 목록을 즉시 갱신
                    st.session_state["save_notice"] = (md_saved_path, word_saved_path)
                    st.rerun()
                notice = st.session_state.pop("save_notice", None)
                if notice:
                    st.success(
                        f"내보냈습니다. 사이드바의 저장된 검토 기록에서 [내보냄] 항목으로 열어볼 수 있습니다.\n\n"
                        f"- Markdown: `{notice[0]}`\n- Word: `{notice[1]}`",
                        icon=":material/task_alt:",
                    )
