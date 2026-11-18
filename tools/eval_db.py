"""실제 DB + 앱과 같은 질의 경로(query_embedder_spec → Embedder.encode_query → collection.query)로 평가."""
import sys, os, re, time
APP_DIR = sys.argv[1]
sys.path.insert(0, APP_DIR)
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_retrieval.py"), encoding="utf-8").read()
exec(src[src.index("# ---- 정답 판정 함수"):src.index("# ---- 청크 로드")])  # GOLD 정의만 가져옴

import update_mdr as u
col = u.get_collection()
name, norm = u.query_embedder_spec()
emb = u.get_embedder(name, normalize=norm)
print("index model:", name, "normalize:", norm, "chunks:", col.count())
K, hits, rr, misses, t = 6, 0, 0.0, [], time.time()
for q, ok in GOLD:
    res = col.query(query_embeddings=emb.encode_query([q]), n_results=50)
    docs, metas = res["documents"][0], res["metadatas"][0]
    rank = next((i for i, (d, m) in enumerate(zip(docs, metas)) if ok(d, m)), None)
    hits += rank is not None and rank < K
    rr += 1 / (rank + 1) if rank is not None else 0
    if rank is None or rank >= K:
        misses.append(f"{q} (rank {rank + 1 if rank is not None else '>50'})")
print(f"Recall@{K}: {hits}/{len(GOLD)} = {hits/len(GOLD):.0%}  MRR@50: {rr/len(GOLD):.3f}  "
      f"avg query time: {(time.time()-t)/len(GOLD)*1000:.0f} ms")
for m in misses:
    print("  miss:", m)
