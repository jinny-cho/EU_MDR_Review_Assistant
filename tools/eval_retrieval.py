"""
검색 품질 평가: 한국어 질의 → 정답 조항/문서가 상위 k개 안에 드는지 (Recall@k, MRR).
사용: python eval_retrieval.py <app_dir> <model_name> [max_seq_length] [prefix_mode]
  prefix_mode: none | e5   (e5: "query: " / "passage: " 접두어)
"""
import sys, os, re, json, time
import numpy as np

APP_DIR, MODEL = sys.argv[1], sys.argv[2]
MAX_SEQ = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] != "-" else None
PREFIX = sys.argv[4] if len(sys.argv) > 4 else "none"
CHUNKS_JSON = sys.argv[5] if len(sys.argv) > 5 else None
sys.path.insert(0, APP_DIR)
HERE = os.path.dirname(os.path.abspath(__file__))

# ---- 정답 판정 함수 ----
def rule(n):
    pat = re.compile(rf"^(\d+\.\d+\.\s*)?Rule {n}$", re.M)
    return lambda d, m: m["doc_type"] == "EU_MDR" and bool(pat.search(d))

def article(*nums):
    secs = {f"Article {n}" for n in nums}
    return lambda d, m: m["doc_type"] == "EU_MDR" and re.sub(r"\s+", " ", m["section"]) in secs

def annex(*romans):
    secs = {f"ANNEX {r}" for r in romans}
    return lambda d, m: m["doc_type"] == "EU_MDR" and re.sub(r"\s+", " ", m["section"]) in secs

def doc(*names):
    return lambda d, m: m["doc_type"] == "MDCG_GUIDANCE" and any(n in m["source_name"] for n in names)

def any_of(*fs):
    return lambda d, m: any(f(d, m) for f in fs)

GOLD = [
    ("나노물질을 포함한 의료기기의 등급 분류 기준", rule(19)),
    ("X-ray 진단 영상을 기록하는 장치는 몇 등급인가", rule(17)),
    ("이온화 방사선을 방출하는 능동형 진단기기 등급", rule(10)),
    ("소프트웨어의 등급 분류 규칙", rule(11)),
    ("규제 준수 책임자 PRRC 자격 요건", any_of(article(15), doc("MDCG_2019-7"))),
    ("제조업자의 일반 의무", article(10)),
    ("의료기기의 정의", article(2)),
    ("UDI 할당 및 등록 의무", any_of(article(27, 29), doc("MDCG_2018-1"))),
    ("정기 안전성 갱신 보고서 PSUR 작성 주기", any_of(article(86), doc("MDCG_2022-21"))),
    ("시판 후 감시 계획에 포함할 내용", any_of(article(84), annex("III"))),
    ("기술문서에 포함되어야 하는 항목", annex("II")),
    ("EU 적합성 선언서에 들어가야 할 정보", any_of(annex("IV"), article(19))),
    ("CE 마킹 부착 요건", any_of(article(20), annex("V"))),
    ("임상평가 수행 요구사항", any_of(article(61), annex("XIV"))),
    ("중대한 사고 보고 기한", article(87)),
    ("레거시 기기의 경과 규정과 시장 출시 기한", any_of(article(120), doc("MDCG_2021-25"), doc("MDCG_2022-14"))),
    ("라벨과 사용설명서 표시 요구사항", annex("I")),
    ("안전성 및 임상성능 요약서 SSCP", any_of(article(32), doc("MDCG_2019-9"))),
    ("소프트웨어가 의료기기에 해당하는지 판단 기준", doc("MDCG_2019-11")),
    ("의료기기 사이버보안 요구사항", any_of(doc("MDCG_2019-16"), annex("I"))),
    ("레거시 기기의 중요한 설계 변경 판단 기준", doc("MDCG_2020-3")),
    ("동등성 입증을 위한 기술적 생물학적 임상적 특성", any_of(doc("MDCG_2020-5"), annex("XIV"))),
    ("국가별 사용설명서 언어 요구사항", any_of(doc("MDR_Language"), article(10))),
    ("Rule 11 소프트웨어 등급 분류 해설", any_of(doc("MDCG_2019-11"), doc("MDCG_2021-24"), rule(11))),
    ("Article 10 제조자 의무", article(10)),
    ("Annex VIII Rule 19 나노물질", rule(19)),
    ("일회용 기기의 재처리", article(17)),
    ("인증기관의 적합성 평가 절차 선택", any_of(article(52), annex("IX"), annex("X"), annex("XI"))),
    ("의료기기와 의약품이 결합된 제품의 분류", any_of(rule(14), article(1), doc("Borderline"))),
    ("주문 제작 기기의 요구사항", any_of(article(52), annex("XIII"))),
]

# ---- 청크 로드 (테스트 DB에서) ----
if CHUNKS_JSON:
    data = json.load(open(CHUNKS_JSON, encoding="utf-8"))
    docs, metas = data["docs"], data["metas"]
else:
    import update_mdr as u
    col = u.get_client().get_collection(u.COLLECTION_NAME)
    got = col.get(include=["documents", "metadatas"])
    docs, metas = got["documents"], got["metadatas"]

from sentence_transformers import SentenceTransformer
model = SentenceTransformer(MODEL, device="cpu")
if MAX_SEQ:
    model.max_seq_length = MAX_SEQ
qp, dp = ("query: ", "passage: ") if PREFIX == "e5" else ("", "")

tag = re.sub(r"[^A-Za-z0-9]+", "_", f"{MODEL}_{MAX_SEQ}_{PREFIX}_{len(docs)}_{CHUNKS_JSON and os.path.basename(CHUNKS_JSON)}")
cache = os.path.join(HERE, f"emb_{tag}.npy")
t = time.time()
if os.path.exists(cache):
    D = np.load(cache)
    t_doc = None
else:
    D = model.encode([dp + d for d in docs], batch_size=16, normalize_embeddings=True, show_progress_bar=False)
    np.save(cache, D)
    t_doc = time.time() - t
Q = model.encode([qp + q for q, _ in GOLD], normalize_embeddings=True)

K = 6
hits, rr, misses = 0, 0.0, []
for (q, ok), qv in zip(GOLD, Q):
    order = np.argsort(-D @ qv)
    rank = next((i for i, j in enumerate(order[:50]) if ok(docs[j], metas[j])), None)
    if rank is not None and rank < K:
        hits += 1
    if rank is not None:
        rr += 1.0 / (rank + 1)
    if rank is None or rank >= K:
        n_gold = sum(1 for j in range(len(docs)) if ok(docs[j], metas[j]))
        misses.append(f"{q} (rank {rank + 1 if rank is not None else '>50'}, gold chunks={n_gold})")

print(f"MODEL={MODEL} max_seq={model.max_seq_length} prefix={PREFIX} chunks={len(docs)}")
print(f"  Recall@{K}: {hits}/{len(GOLD)} = {hits/len(GOLD):.0%}   MRR@50: {rr/len(GOLD):.3f}"
      + (f"   doc-embed time: {t_doc:.0f}s" if t_doc else ""))
for m in misses:
    print("   miss:", m)
