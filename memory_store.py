# -*- coding: utf-8 -*-
"""长期记忆 —— SQLite + bge-small-zh(:1236) 语义检索，零显存占用。

定位：
    Whispera 的 mem0 能力的自建替代。存储用标准库 sqlite3（零依赖），
    向量用已注册的 embedding 服务（model_sched.SERVERS["embedding"]，
    llama.cpp --embedding --pooling cls，CPU 常驻，~30MB 模型）。

数据:
    state/memory.db  表 memories(id, kind, text, vec, no_vec, created,
                                last_hit, hit_count, source)
    kind: fact(事实/偏好) | note(对话要点) | diary(日记摘要)

降级策略（不黑箱）:
    embedding 离线时写入照常成功（vec 置空 + no_vec=1），检索退化为
    LIKE 关键词兜底并明确提示 —— 宁可降级不可失败。

检索:
    query 加 bge 官方检索指令前缀，cosine 相似度，阈值 MEM_MIN_SCORE
    过滤，结果按分数降序。命中会更新 last_hit/hit_count（供后续清理低价值记忆）。

用法:
    python memory_store.py remember "用户偏好直接剪切而非复制" [--kind fact]
    python memory_store.py recall "文件删除习惯" [-k 4]
    python memory_store.py recent [-n 10]
    python memory_store.py forget 3 | python memory_store.py forget "关键词"
    python memory_store.py stats
"""
import argparse
import json
import os
import sqlite3
import struct
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "state", "memory.db")
EMB_BASE = "http://127.0.0.1:1236"
MEM_MIN_SCORE = 0.42          # cosine 阈值（实测: 真命中 0.51+/0.63, 噪声 ~0.35）
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："   # bge-zh 官方查询前缀
KINDS = ("fact", "note", "diary")


# ---------------------------------------------------------------- 基础
def _db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS memories(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        text TEXT NOT NULL,
        vec BLOB,
        no_vec INTEGER DEFAULT 0,
        created REAL,
        last_hit REAL,
        hit_count INTEGER DEFAULT 0,
        source TEXT)""")
    return conn


def _vec_to_blob(vec):
    return struct.pack("%df" % len(vec), *vec)


def _blob_to_vec(blob):
    n = len(blob) // 4
    return list(struct.unpack("%df" % n, blob))


# ---------------------------------------------------------------- embedding
def embed(text, is_query=False):
    """调 :1236 生成向量。离线/失败返回 None（调用方降级）。"""
    try:
        import requests
        s = requests.Session()
        s.trust_env = False
        content = (QUERY_INSTRUCTION + text) if is_query else text
        r = s.post(EMB_BASE + "/v1/embeddings",
                   json={"input": content, "model": "bge-small-zh"},
                   timeout=10)
        if r.status_code != 200:
            return None
        return r.json()["data"][0]["embedding"]
    except Exception:
        return None


def _online():
    return embed("ping") is not None


# ---------------------------------------------------------------- 写入
def remember(text, kind="fact", source=None):
    """存一条记忆。近似去重：同 kind 下完全相同文本跳过。返回 (id, msg)。"""
    text = (text or "").strip()
    if not text:
        return None, "空文本，忽略"
    if kind not in KINDS:
        kind = "fact"
    conn = _db()
    try:
        dup = conn.execute(
            "SELECT id FROM memories WHERE kind=? AND text=?",
            (kind, text)).fetchone()
        if dup:
            return dup[0], "已存在（#%d），跳过" % dup[0]
        vec = embed(text)
        no_vec = 0 if vec else 1
        cur = conn.execute(
            "INSERT INTO memories(kind,text,vec,no_vec,created,source) "
            "VALUES(?,?,?,?,?,?)",
            (kind, text, _vec_to_blob(vec) if vec else None, no_vec,
             time.time(), source))
        conn.commit()
        mid = cur.lastrowid
        note = "" if vec else "  ⚠ embedding 离线，本条无向量（检索走关键词兜底）"
        return mid, "已记住 #%d [%s] %s%s" % (mid, kind, text[:40], note)
    finally:
        conn.close()


# ---------------------------------------------------------------- 检索
def recall(query, top_k=4, kinds=None):
    """语义检索 top_k。返回 [(id, text, kind, score)]。"""
    query = (query or "").strip()
    if not query:
        return []
    conn = _db()
    try:
        q = ("SELECT id,kind,text,vec,no_vec FROM memories"
             + (" WHERE kind IN (%s)" % ",".join("?" * len(kinds))
                if kinds else ""))
        rows = conn.execute(q, tuple(kinds) if kinds else ()).fetchall()
        if not rows:
            return []
        qvec = embed(query, is_query=True)
        if qvec:
            scored = []
            import math
            qn = math.sqrt(sum(x * x for x in qvec)) or 1.0
            for rid, kind, text, blob, no_vec in rows:
                if no_vec or not blob:
                    continue
                v = _blob_to_vec(blob)
                vn = math.sqrt(sum(x * x for x in v)) or 1.0
                score = sum(a * b for a, b in zip(qvec, v)) / (qn * vn)
                if score >= MEM_MIN_SCORE:
                    scored.append((rid, text, kind, round(score, 3)))
            scored.sort(key=lambda x: -x[3])
            out = scored[:top_k]
        else:
            # 降级：关键词兜底（embedding 离线时明确可感知）
            kws = [w for w in query.split() if len(w) >= 2] or [query]
            scored = []
            for rid, kind, text, blob, no_vec in rows:
                hit = sum(1 for w in kws if w in text)
                if hit:
                    scored.append((rid, text, kind, hit / len(kws)))
            scored.sort(key=lambda x: -x[3])
            out = scored[:top_k]
        # 更新命中计数
        if out:
            now = time.time()
            for rid, _, _, _ in out:
                conn.execute("UPDATE memories SET last_hit=?, hit_count=hit_count+1 "
                             "WHERE id=?", (now, rid))
            conn.commit()
        return out
    finally:
        conn.close()


def context_for_prompt(query, top_k=4):
    """chat 模式注入用：返回文本块（无命中返回空串）。"""
    hits = recall(query, top_k=top_k)
    if not hits:
        return ""
    lines = ["[长期记忆-相关]"]
    for rid, text, kind, score in hits:
        lines.append("- %s" % text)
    return "\n".join(lines)


# ---------------------------------------------------------------- 管理
def recent(n=10):
    conn = _db()
    try:
        return conn.execute(
            "SELECT id,kind,text,created,hit_count,no_vec FROM memories "
            "ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    finally:
        conn.close()


def forget(target):
    """按 id 或关键词删。返回 (删除数, 明细)。"""
    conn = _db()
    try:
        if str(target).isdigit():
            cur = conn.execute("DELETE FROM memories WHERE id=?", (int(target),))
        else:
            cur = conn.execute("DELETE FROM memories WHERE text LIKE ?",
                               ("%%%s%%" % target,))
        conn.commit()
        return cur.rowcount, "已删除 %d 条（匹配 %r）" % (cur.rowcount, target)
    finally:
        conn.close()


def stats():
    conn = _db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        by_kind = conn.execute(
            "SELECT kind, COUNT(*) FROM memories GROUP BY kind").fetchall()
        no_vec = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE no_vec=1").fetchone()[0]
        return {"total": total, "by_kind": dict(by_kind), "no_vec": no_vec}
    finally:
        conn.close()


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="长期记忆（SQLite + bge :1236）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("remember")
    p1.add_argument("text")
    p1.add_argument("--kind", default="fact", choices=KINDS)
    p1.add_argument("--source", default="cli")
    p2 = sub.add_parser("recall")
    p2.add_argument("query")
    p2.add_argument("-k", type=int, default=4)
    p2.add_argument("--kind", choices=KINDS, nargs="*")
    p3 = sub.add_parser("recent")
    p3.add_argument("-n", type=int, default=10)
    p4 = sub.add_parser("forget")
    p4.add_argument("target")
    sub.add_parser("stats")
    sub.add_parser("online")          # embedding 服务探测（自检用）
    a = ap.parse_args()

    if a.cmd == "remember":
        mid, msg = remember(a.text, a.kind, a.source)
        print(msg)
    elif a.cmd == "recall":
        hits = recall(a.query, a.k, a.kind)
        if not hits:
            print("(无命中)")
        for rid, text, kind, score in hits:
            print("#%-4d [%.3f %s] %s" % (rid, score, kind, text))
    elif a.cmd == "recent":
        rows = recent(a.n)
        if not rows:
            print("(空)")
        for rid, kind, text, created, hits, no_vec in rows:
            ts = time.strftime("%m-%d %H:%M", time.localtime(created))
            warn = " ⚠无向量" if no_vec else ""
            print("#%-4d %s [%s] 命中%d%s  %s" % (rid, ts, kind, hits, warn, text))
    elif a.cmd == "forget":
        n, msg = forget(a.target)
        print(msg)
    elif a.cmd == "stats":
        st = stats()
        print("总计 %d 条  分类 %s  无向量 %d 条" % (
            st["total"], st["by_kind"] or "{}", st["no_vec"]))
    elif a.cmd == "online":
        print("embedding :1236 %s" % ("在线" if _online() else "离线"))


if __name__ == "__main__":
    main()
