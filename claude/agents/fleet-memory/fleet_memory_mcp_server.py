#!/usr/bin/env python3
"""Tiny fleet-memory MCP bridge for Hermes holographic facts.

Exposes direct SQLite-backed MCP tools over ~/.hermes/memory_store.db:
- fleet_memory_fact_search
- fleet_memory_fact_feedback
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

DB_PATH = Path(os.environ.get("HERMES_MEMORY_DB", "~/.hermes/memory_store.db")).expanduser()
HELPFUL_DELTA = 0.05
UNHELPFUL_DELTA = -0.10


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def fleet_memory_fact_search(
    query: str,
    category: str | None = None,
    min_trust: float = 0.3,
    limit: int = 10,
) -> dict[str, Any]:
    """Search Hermes facts by FTS5, with LIKE fallback for sparse/dev DBs."""
    query = (query or "").strip()
    if not query:
        raise ValueError("query must not be empty")
    limit = max(1, min(int(limit), 100))
    if not DB_PATH.exists():
        return {"db_path": str(DB_PATH), "count": 0, "results": []}

    with _connect() as conn:
        if not _has_table(conn, "facts"):
            return {"db_path": str(DB_PATH), "count": 0, "results": []}
        cols = _columns(conn, "facts")
        select_cols = [c for c in ("fact_id", "content", "category", "tags", "trust_score", "retrieval_count", "helpful_count", "created_at", "updated_at") if c in cols]
        category_clause = "AND f.category = ?" if category and "category" in cols else ""
        trust_clause = "AND f.trust_score >= ?" if "trust_score" in cols else ""
        params: list[Any] = []
        if _has_table(conn, "facts_fts"):
            params = [query]
            if "trust_score" in cols:
                params.append(float(min_trust))
            if category_clause:
                params.append(category)
            params.append(limit)
            sql = f"""
                SELECT {', '.join('f.' + c for c in select_cols)}
                FROM facts f JOIN facts_fts fts ON fts.rowid = f.fact_id
                WHERE facts_fts MATCH ? {trust_clause} {category_clause}
                ORDER BY fts.rank, {('f.trust_score DESC' if 'trust_score' in cols else 'f.fact_id DESC')}
                LIMIT ?
            """
            try:
                rows = conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                rows = []
        else:
            rows = []
        if not rows:
            where = ["f.content LIKE ?"]
            params = [f"%{query}%"]
            if "tags" in cols:
                where[0] = "(f.content LIKE ? OR f.tags LIKE ?)"
                params.append(f"%{query}%")
            if "trust_score" in cols:
                where.append("f.trust_score >= ?")
                params.append(float(min_trust))
            if category_clause:
                where.append("f.category = ?")
                params.append(category)
            params.append(limit)
            sql = f"""
                SELECT {', '.join('f.' + c for c in select_cols)}
                FROM facts f
                WHERE {' AND '.join(where)}
                ORDER BY {('f.trust_score DESC,' if 'trust_score' in cols else '')} f.fact_id DESC
                LIMIT ?
            """
            rows = conn.execute(sql, params).fetchall()
        results = [dict(row) for row in rows]
        if results and "retrieval_count" in cols and "fact_id" in cols:
            ids = [r["fact_id"] for r in results]
            conn.execute(
                f"UPDATE facts SET retrieval_count = retrieval_count + 1 WHERE fact_id IN ({','.join('?' for _ in ids)})",
                ids,
            )
            conn.commit()
        return {"db_path": str(DB_PATH), "count": len(results), "results": results}


def fleet_memory_fact_feedback(fact_id: int, action: str | None = None, helpful: bool | None = None) -> dict[str, Any]:
    """Apply asymmetric trust feedback (+0.05 helpful, -0.10 unhelpful)."""
    if helpful is None:
        if action not in {"helpful", "unhelpful"}:
            raise ValueError("action must be 'helpful' or 'unhelpful'")
        helpful = action == "helpful"
    if not DB_PATH.exists():
        raise FileNotFoundError(f"memory DB not found: {DB_PATH}")
    with _connect() as conn:
        if not _has_table(conn, "facts"):
            raise RuntimeError("memory DB does not contain a facts table")
        row = conn.execute(
            "SELECT fact_id, trust_score, helpful_count FROM facts WHERE fact_id = ?",
            (fact_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"fact_id {fact_id} not found")
        old = float(row["trust_score"])
        new = max(0.0, min(1.0, old + (HELPFUL_DELTA if helpful else UNHELPFUL_DELTA)))
        inc = 1 if helpful else 0
        conn.execute(
            "UPDATE facts SET trust_score = ?, helpful_count = helpful_count + ?, updated_at = CURRENT_TIMESTAMP WHERE fact_id = ?",
            (new, inc, fact_id),
        )
        conn.commit()
        return {"fact_id": int(fact_id), "old_trust": old, "new_trust": new, "helpful_count": int(row["helpful_count"]) + inc}


TOOLS = {
    "fleet_memory_fact_search": {
        "description": "Search Hermes holographic facts in ~/.hermes/memory_store.db.",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "category": {"type": "string"}, "min_trust": {"type": "number", "default": 0.3}, "limit": {"type": "integer", "default": 10}}, "required": ["query"]},
        "handler": fleet_memory_fact_search,
    },
    "fleet_memory_fact_feedback": {
        "description": "Rate a Hermes fact as helpful or unhelpful to adjust trust score.",
        "inputSchema": {"type": "object", "properties": {"fact_id": {"type": "integer"}, "action": {"type": "string", "enum": ["helpful", "unhelpful"]}, "helpful": {"type": "boolean"}}, "required": ["fact_id"]},
        "handler": fleet_memory_fact_feedback,
    },
}


def _ok(result: Any, msg_id: Any = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(message: str, msg_id: Any = None, code: int = -32000) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method, msg_id = message.get("method"), message.get("id")
    if method == "initialize":
        return _ok({"protocolVersion": "2024-11-05", "serverInfo": {"name": "fleet-memory", "version": "0.1.0"}, "capabilities": {"tools": {}}}, msg_id)
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return _ok({"tools": [{"name": name, "description": spec["description"], "inputSchema": spec["inputSchema"]} for name, spec in TOOLS.items()]}, msg_id)
    if method == "tools/call":
        params = message.get("params", {})
        name = params.get("name")
        if name not in TOOLS:
            return _err(f"unknown tool: {name}", msg_id, -32602)
        try:
            payload = TOOLS[name]["handler"](**(params.get("arguments") or {}))
            return _ok({"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}, msg_id)
        except Exception as exc:
            return _err(str(exc), msg_id)
    return _err(f"unsupported method: {method}", msg_id, -32601)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in TOOLS:
        args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
        print(json.dumps(TOOLS[sys.argv[1]]["handler"](**args), ensure_ascii=False))
        return 0
    for line in sys.stdin:
        if not line.strip():
            continue
        response = handle(json.loads(line))
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
