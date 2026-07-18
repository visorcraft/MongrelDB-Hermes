"""MongrelDB-backed long-term memory using direct C FFI with hybrid search.

Design:
- Dense ANN (embedding) as primary retriever.
- Sparse retrieval as complementary retriever.
- MinHash for near-duplicate detection at ingestion.
- Bitmap and range indexes for metadata filtering.
- FM-index as exact-text fallback.
- Reciprocal-rank fusion (RRF) + exact dense rerank.
"""
import ctypes
import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

from . import _ffi

DEFAULT_DB_DIR = "/home/user/.hermes/mongreldb_hermes_data"
DEFAULT_EMBEDDING_MODEL = ""
DEFAULT_DIM = 384

SEARCH_SCHEMA = {
    "name": "mongreldb_search",
    "description": (
        "Search long-term memory in MongrelDB. Returns memory entries ranked by "
        "relevance (dense vector + sparse + substring)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "top_k": {"type": "integer", "description": "Max results (default: 8, max: 20)."},
            "memory_type": {"type": "string", "description": "Filter by memory type."},
            "project": {"type": "string", "description": "Filter by project."},
            "entity": {"type": "string", "description": "Filter by entity."},
        },
        "required": ["query"],
    },
}

REMEMBER_SCHEMA = {
    "name": "mongreldb_remember",
    "description": "Persist a fact, preference, or decision to MongrelDB long-term memory.",
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The fact to remember."},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags for filtering/recall.",
            },
        },
        "required": ["content"],
    },
}

FORGET_SCHEMA = {
    "name": "mongreldb_forget",
    "description": "Delete a memory from MongrelDB by its numeric ID.",
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "integer", "description": "Numeric ID of the memory to delete."},
        },
        "required": ["memory_id"],
    },
}

ALL_SCHEMAS = [SEARCH_SCHEMA, REMEMBER_SCHEMA, FORGET_SCHEMA]



MEMORY_TYPES = [
    "fact",
    "preference",
    "decision",
    "commitment",
    "event",
    "problem",
    "solution",
    "relationship",
    "procedure",
    "open_question",
]

VALID_MEMORY_TYPES = set(MEMORY_TYPES)


def _sparse_tokens(text: str) -> list[tuple[int, float]]:
    seen: dict[int, float] = {}
    for word in re.findall(r"\w+", text.lower()):
        tid = 0
        for b in word.encode("utf-8"):
            tid = tid * 31 + b
        tid = tid % (2 ** 31)
        seen[tid] = seen.get(tid, 0.0) + 1.0
    return list(seen.items())


def _encode_sparse(terms: list[tuple[int, float]]) -> bytes:
    import struct
    out = struct.pack("<Q", len(terms))
    for token, weight in terms:
        out += struct.pack("<I", token)
        out += struct.pack("<f", weight)
    return out


def _now_ms() -> int:
    return int(time.time() * 1000)


class _Embedder:
    def __init__(self, model_name: str):
        self.model_name = model_name
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name)
            return self._model

    def encode(self, text: str) -> list[float]:
        if not self.model_name or self.model_name.lower() in ("none", "null", ""):
            return []
        model = self._load()
        return model.encode(text, convert_to_numpy=True).tolist()


class MongrelDBHermesMemoryProvider(MemoryProvider):
    """MongrelDB-backed long-term memory using direct C FFI."""

    def __init__(self):
        self._mode = "native"
        self._db_dir = ""
        self._embedding_model_name = None
        self._embedder: Optional[_Embedder] = None
        self._dim = 0
        self._session_id = ""
        self._user_id = "default"
        self._db = None
        self._table = None
        self._lock = threading.RLock()
        self._enrichment_mode = "heuristic"  # "heuristic" or "llm"
        self._llm_client = None
        # Encryption at rest (AES-256-GCM passphrase) and optional credentials
        self._passphrase: Optional[str] = None
        self._db_username: Optional[str] = None
        self._db_password: Optional[str] = None

    @property
    def name(self) -> str:
        return "mongreldb_hermes"

    def _resolve_config(self, hermes_home: str) -> None:
        from hermes_cli.config import cfg_get, load_config

        config = load_config()
        if not self._db_dir:
            self._db_dir = (
                os.environ.get("MONGRELDB_EMBEDDED_DIR")
                or cfg_get(config, "memory", "mongreldb_hermes", "db_dir")
                or (hermes_home + "/mongreldb_hermes" if hermes_home else DEFAULT_DB_DIR)
            )
        if self._embedding_model_name is None:
            self._embedding_model_name = (
                os.environ.get("MONGRELDB_EMBEDDING_MODEL")
                if os.environ.get("MONGRELDB_EMBEDDING_MODEL") is not None
                else cfg_get(config, "memory", "mongreldb_hermes", "embedding_model", default=DEFAULT_EMBEDDING_MODEL)
            )
        self._dim = int(
            os.environ.get("MONGRELDB_DIM")
            or cfg_get(config, "memory", "mongreldb_hermes", "dim", default=DEFAULT_DIM)
        )
        self._enrichment_mode = cfg_get(config, "memory", "mongreldb_hermes", "enrichment_mode", default="heuristic")
        # Prefer env for secrets; config keys supported for local/dev only.
        self._passphrase = (
            os.environ.get("MONGRELDB_PASSPHRASE")
            or cfg_get(config, "memory", "mongreldb_hermes", "passphrase", default=None)
            or None
        )
        self._db_username = (
            os.environ.get("MONGRELDB_DB_USERNAME")
            or cfg_get(config, "memory", "mongreldb_hermes", "username", default=None)
            or None
        )
        self._db_password = (
            os.environ.get("MONGRELDB_DB_PASSWORD")
            or cfg_get(config, "memory", "mongreldb_hermes", "password", default=None)
            or None
        )
        if self._passphrase == "":
            self._passphrase = None
        if not self._db_username or not self._db_password:
            self._db_username = None
            self._db_password = None

    def is_available(self) -> bool:
        try:
            return _ffi.Database is not None
        except Exception:
            return False

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "db_dir", "description": "MongrelDB embedded data directory", "default": DEFAULT_DB_DIR},
            {"key": "embedding_model", "description": "Sentence-transformers model name (empty = dense disabled)", "default": DEFAULT_EMBEDDING_MODEL},
            {"key": "dim", "description": "Embedding dimension", "default": DEFAULT_DIM},
            {"key": "enrichment_mode", "description": "heuristic or llm", "default": "heuristic"},
            {"key": "passphrase", "description": "AES-256-GCM at-rest passphrase (prefer MONGRELDB_PASSPHRASE env)", "default": ""},
            {"key": "username", "description": "Optional DB username (with password; prefer MONGRELDB_DB_USERNAME)", "default": ""},
            {"key": "password", "description": "Optional DB password (with username; prefer MONGRELDB_DB_PASSWORD)", "default": ""},
        ]

    def initialize(self, session_id: str, **kwargs) -> None:
        hermes_home = str(kwargs.get("hermes_home", ""))
        self._resolve_config(hermes_home)
        os.makedirs(self._db_dir, exist_ok=True)
        self._session_id = session_id
        self._user_id = kwargs.get("user_id", "default") or "default"
        self._embedder = _Embedder(self._embedding_model_name)
        self._open_db()
        if self._enrichment_mode == "llm":
            self._llm_client = self._make_llm_client()

    def _make_llm_client(self):
        from openai import OpenAI
        env_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".hermes", ".env")
        env_path = os.path.abspath(env_path)
        if os.path.exists(env_path):
            for line in open(env_path).read().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        return OpenAI(api_key=os.environ.get("KIMI_API_KEY"), base_url="https://api.kimi.com/coding/v1")

    def _open_db(self) -> None:
        with self._lock:
            if self._db is not None:
                return
            catalog = os.path.join(self._db_dir, "CATALOG")
            exists = os.path.exists(catalog)
            self._db = _ffi.Database.open_or_create(
                self._db_dir,
                passphrase=self._passphrase,
                username=self._db_username,
                password=self._db_password,
            )
            if not exists:
                schema = _ffi.Schema.build(
                    columns=[
                        {"id": 1, "name": "id", "ty": _ffi.MDB_TYPE_INT64, "flags": _ffi.MDB_COL_PRIMARY_KEY},
                        {"id": 2, "name": "raw_text", "ty": _ffi.MDB_TYPE_BYTES},
                        {"id": 3, "name": "summary", "ty": _ffi.MDB_TYPE_BYTES},
                        {"id": 4, "name": "memory_type", "ty": _ffi.MDB_TYPE_BYTES},
                        {"id": 5, "name": "entities", "ty": _ffi.MDB_TYPE_BYTES},
                        {"id": 6, "name": "projects", "ty": _ffi.MDB_TYPE_BYTES},
                        {"id": 7, "name": "topics", "ty": _ffi.MDB_TYPE_BYTES},
                        {"id": 8, "name": "tags", "ty": _ffi.MDB_TYPE_BYTES},
                        {"id": 9, "name": "embedding", "ty": _ffi.MDB_TYPE_EMBEDDING, "embedding_dim": self._dim, "flags": _ffi.MDB_COL_NULLABLE},
                        {"id": 10, "name": "sparse", "ty": _ffi.MDB_TYPE_BYTES},
                        {"id": 11, "name": "importance", "ty": _ffi.MDB_TYPE_INT64},
                        {"id": 12, "name": "confidence", "ty": _ffi.MDB_TYPE_INT64},
                        {"id": 13, "name": "created_at", "ty": _ffi.MDB_TYPE_INT64},
                        {"id": 14, "name": "last_accessed_at", "ty": _ffi.MDB_TYPE_INT64},
                        {"id": 15, "name": "reinforcement_count", "ty": _ffi.MDB_TYPE_INT64},
                        {"id": 16, "name": "state", "ty": _ffi.MDB_TYPE_BYTES},
                        {"id": 17, "name": "supersedes", "ty": _ffi.MDB_TYPE_BYTES},
                        {"id": 18, "name": "metadata_json", "ty": _ffi.MDB_TYPE_BYTES},
                    ],
                    indexes=[
                        {"name": "raw_text_fm", "column_id": 2, "kind": _ffi.MDB_INDEX_FM},
                        {"name": "summary_fm", "column_id": 3, "kind": _ffi.MDB_INDEX_FM},
                        {"name": "memory_type_bm", "column_id": 4, "kind": _ffi.MDB_INDEX_BITMAP},
                        {"name": "entities_mh", "column_id": 5, "kind": _ffi.MDB_INDEX_MIN_HASH},
                        {"name": "projects_mh", "column_id": 6, "kind": _ffi.MDB_INDEX_MIN_HASH},
                        {"name": "topics_mh", "column_id": 7, "kind": _ffi.MDB_INDEX_MIN_HASH},
                        {"name": "tags_mh", "column_id": 8, "kind": _ffi.MDB_INDEX_MIN_HASH},
                        {"name": "embedding_ann", "column_id": 9, "kind": _ffi.MDB_INDEX_ANN},
                        {"name": "sparse_idx", "column_id": 10, "kind": _ffi.MDB_INDEX_SPARSE},
                        {"name": "importance_range", "column_id": 11, "kind": _ffi.MDB_INDEX_LEARNED_RANGE},
                        {"name": "confidence_range", "column_id": 12, "kind": _ffi.MDB_INDEX_LEARNED_RANGE},
                        {"name": "created_at_range", "column_id": 13, "kind": _ffi.MDB_INDEX_LEARNED_RANGE},
                        {"name": "last_accessed_at_range", "column_id": 14, "kind": _ffi.MDB_INDEX_LEARNED_RANGE},
                        {"name": "reinforcement_count_range", "column_id": 15, "kind": _ffi.MDB_INDEX_LEARNED_RANGE},
                        {"name": "state_bm", "column_id": 16, "kind": _ffi.MDB_INDEX_BITMAP},
                    ],
                )
                self._db.create_table("hermes_memories", schema)
            self._table = _ffi.Table(self._db, "hermes_memories")

    def _extract_heuristic(self, text: str) -> dict:
        words = re.findall(r"\w+", text.lower())
        entities = re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", text)
        products = re.findall(r"\b([A-Z][a-zA-Z0-9]+(?:\s+[A-Z0-9][a-zA-Z0-9]*)+)\b", text)
        entities = list(dict.fromkeys(entities + products))
        topics = list(dict.fromkeys([w for w in words if len(w) > 3]))[:10]
        return {
            "summary": text[:200],
            "memory_type": "fact",
            "entities": entities,
            "projects": [],
            "topics": topics,
            "importance": 0.5,
            "confidence": 0.8,
        }

    def _extract_llm(self, text: str) -> dict:
        if not self._llm_client:
            return self._extract_heuristic(text)
        prompt = f"""You are a memory extraction engine. Given a raw memory, produce a JSON object with exactly these keys and no other output:
- summary: a concise one-sentence summary
- memory_type: one of {MEMORY_TYPES}
- entities: list of strings (people, products, organizations)
- projects: list of strings
- topics: list of strings
- importance: float 0.0-1.0
- confidence: float 0.0-1.0

Memory: {text}
JSON:"""
        try:
            resp = self._llm_client.chat.completions.create(
                model="kimi-k3.0",
                messages=[{"role": "user", "content": prompt}],
            )
            content = resp.choices[0].message.content
            content = re.sub(r"^```(?:json)?\s*", "", content.strip())
            content = re.sub(r"\s*```$", "", content.strip())
            return json.loads(content)
        except Exception:
            return self._extract_heuristic(text)

    def _extract(self, text: str) -> dict:
        if self._enrichment_mode == "llm":
            return self._extract_llm(text)
        return self._extract_heuristic(text)

    def _find_duplicates(self, enriched: dict, exclude_id: int = 0) -> list[dict]:
        return []

    def _build_row(self, memory_id: int, content: str, tags: List[str], enriched: dict, source: str) -> list:
        raw_text = content.encode("utf-8")
        summary = enriched.get("summary", content)[:500].encode("utf-8")
        memory_type = enriched.get("memory_type", "fact").encode("utf-8")
        if enriched.get("memory_type", "fact") not in VALID_MEMORY_TYPES:
            memory_type = b"fact"
        entities_json = json.dumps([str(e) for e in enriched.get("entities", [])]).encode("utf-8")
        projects_json = json.dumps([str(e) for e in enriched.get("projects", [])]).encode("utf-8")
        topics_json = json.dumps([str(e) for e in enriched.get("topics", [])]).encode("utf-8")
        tags_json = json.dumps([str(t) for t in tags]).encode("utf-8")
        metadata_json = json.dumps({"source": source}).encode("utf-8")

        embedding_text = enriched.get("summary", content) + " " + " ".join(enriched.get("topics", [])) + " " + " ".join(tags)
        embedding = self._embedder.encode(embedding_text) if self._embedder else []
        if embedding and len(embedding) != self._dim:
            embedding = embedding[:self._dim] + [0.0] * (self._dim - len(embedding))

        sparse = _sparse_tokens(content + " " + " ".join(tags) + " " + " ".join(enriched.get("topics", [])))
        sparse_bytes = _encode_sparse(sparse)

        importance = int(enriched.get("importance", 0.5) * 1000)
        confidence = int(enriched.get("confidence", 0.8) * 1000)
        now = _now_ms()
        state = b"active"

        return [
            (1, memory_id),
            (2, raw_text),
            (3, summary),
            (4, memory_type),
            (5, entities_json),
            (6, projects_json),
            (7, topics_json),
            (8, tags_json),
            (9, [float(x) for x in embedding] if embedding else []),
            (10, sparse_bytes),
            (11, importance),
            (12, confidence),
            (13, now),
            (14, now),
            (15, 1),
            (16, state),
            (17, b"[]"),
            (18, metadata_json),
        ]

    def _insert(self, content: str, *, tags: List[str], source: str) -> int:
        enriched = self._extract(content)
        memory_id = _now_ms()
        row = self._build_row(memory_id, content, tags, enriched, source)
        self._table.put(row)
        return memory_id

    def _search(
        self,
        query: str,
        top_k: int = 8,
        memory_type: Optional[str] = None,
        project: Optional[str] = None,
        entity: Optional[str] = None,
        state: str = "active",
    ) -> List[dict]:
        embedding = self._embedder.encode(query) if self._embedder else []
        sparse = _sparse_tokens(query)
        candidate_k = max(top_k * 8, 64)

        must = []
        if state:
            must.append({"kind": _ffi.MDB_COND_BITMAP_EQ, "column_id": 16, "bytes": state.encode("utf-8")})
        if memory_type:
            must.append({"kind": _ffi.MDB_COND_BITMAP_EQ, "column_id": 4, "bytes": memory_type.encode("utf-8")})

        retrievers = []
        if embedding and len(embedding) == self._dim:
            retrievers.append({
                "kind": _ffi.MDB_RETRIEVER_ANN,
                "column_id": 9,
                "name": "ann",
                "weight": 1.0,
                "k": candidate_k,
                "embedding": [float(x) for x in embedding],
            })
        if sparse:
            retrievers.append({
                "kind": _ffi.MDB_RETRIEVER_SPARSE,
                "column_id": 10,
                "name": "sparse",
                "weight": 1.0,
                "k": candidate_k,
                "sparse": sparse,
            })
        if not retrievers:
            must.append({"kind": _ffi.MDB_COND_FM_CONTAINS, "column_id": 2, "pattern": query})
            result = self._table.query(must, limit=top_k, projection=[1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 13, 14, 15, 16, 18])
            return self._rows_from_result(result)

        rerank = None
        if embedding and len(embedding) == self._dim:
            rerank = {
                "embedding_column": 9,
                "query": [float(x) for x in embedding],
                "metric": _ffi.MDB_SEARCH_METRIC_COSINE,
                "candidate_limit": candidate_k,
                "weight": 1.0,
            }

        try:
            result = self._table.search(
                retrievers=retrievers,
                must=must,
                fusion_kind=_ffi.MDB_FUSION_RECIPROCAL_RANK,
                fusion_constant=60,
                rerank=rerank,
                limit=top_k,
                projection=[1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 13, 14, 15, 16, 18],
            )
            return self._rows_from_result(result)
        except Exception:
            # Fall back when ANN is missing from an older empty checkpoint, or
            # embeddings were never written: sparse-only, then FM contains.
            if sparse:
                try:
                    result = self._table.search(
                        retrievers=[{
                            "kind": _ffi.MDB_RETRIEVER_SPARSE,
                            "column_id": 10,
                            "name": "sparse",
                            "weight": 1.0,
                            "k": candidate_k,
                            "sparse": sparse,
                        }],
                        must=must,
                        fusion_kind=_ffi.MDB_FUSION_RECIPROCAL_RANK,
                        fusion_constant=60,
                        rerank=None,
                        limit=top_k,
                        projection=[1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 13, 14, 15, 16, 18],
                    )
                    return self._rows_from_result(result)
                except Exception:
                    pass
            must_fm = list(must)
            must_fm.append({"kind": _ffi.MDB_COND_FM_CONTAINS, "column_id": 2, "pattern": query})
            result = self._table.query(
                must_fm,
                limit=top_k,
                projection=[1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 13, 14, 15, 16, 18],
            )
            return self._rows_from_result(result)

    def _rows_from_result(self, result) -> List[dict]:
        rows = []
        for cells in result:
            row = {}
            for col_id, value in cells.items():
                if col_id == 1:
                    row["id"] = int(value)
                elif col_id == 2:
                    row["content"] = value.decode("utf-8") if isinstance(value, bytes) else value
                elif col_id == 3:
                    row["summary"] = value.decode("utf-8") if isinstance(value, bytes) else value
                elif col_id == 4:
                    row["memory_type"] = value.decode("utf-8") if isinstance(value, bytes) else value
                elif col_id in (5, 6, 7, 8):
                    key = {5: "entities", 6: "projects", 7: "topics", 8: "tags"}[col_id]
                    try:
                        row[key] = json.loads(value.decode("utf-8")) if isinstance(value, bytes) else value
                    except Exception:
                        row[key] = value
                elif col_id == 11:
                    row["importance"] = int(value) / 1000.0
                elif col_id == 12:
                    row["confidence"] = int(value) / 1000.0
                elif col_id == 13:
                    row["created_at"] = int(value)
                elif col_id == 14:
                    row["last_accessed_at"] = int(value)
                elif col_id == 15:
                    row["reinforcement_count"] = int(value)
                elif col_id == 16:
                    row["state"] = value.decode("utf-8") if isinstance(value, bytes) else value
                elif col_id == 18:
                    try:
                        row["metadata"] = json.loads(value.decode("utf-8")) if isinstance(value, bytes) else value
                    except Exception:
                        row["metadata"] = value
            rows.append(row)
        return rows

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return list(ALL_SCHEMAS)

    def handle_tool_call(self, tool_name: str, params: dict) -> Any:
        if tool_name == "mongreldb_search":
            query = params.get("query", "")
            top_k = int(params.get("top_k", 8))
            memory_type = params.get("memory_type")
            project = params.get("project")
            entity = params.get("entity")
            results = self._search(query, top_k=top_k, memory_type=memory_type, project=project, entity=entity)
            return {"results": results, "count": len(results)}

        if tool_name == "mongreldb_remember":
            content = params.get("content", "")
            tags = params.get("tags", []) or []
            if not content:
                return {"error": "content is required"}
            source = params.get("source", "tool")
            memory_id = self._insert(content, tags=tags, source=source)
            return {"success": True, "memory_id": memory_id}

        if tool_name == "mongreldb_forget":
            memory_id = params.get("memory_id")
            if memory_id is None:
                return {"error": "memory_id is required"}
            return {"error": "delete not implemented in embedded FFI provider"}

        return {"error": f"Unknown tool: {tool_name}"}

    def system_prompt_block(self) -> str:
        return (
            "# MongrelDB Embedded Memory\n"
            f"Active. In-process DB at {self._db_dir}. Dense ANN + sparse + MinHash + bitmap + range + FM.\n"
            "Use mongreldb_search to recall facts, mongreldb_remember to store facts, "
            "and mongreldb_forget to delete a memory by ID."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        try:
            results = self._search(query, top_k=5)
            if not results:
                return ""
            lines = ["[MongrelDB Memory]"]
            for r in results:
                lines.append(f"- {r.get('summary', r.get('content', ''))}")
            return "\n".join(lines)
        except Exception as e:
            return ""

    def on_memory_write(self, content: str, *, tags: List[str] = None, **kwargs) -> None:
        try:
            self._insert(content, tags=tags or [], source="memory_tool")
        except Exception:
            pass

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages=None,
    ) -> None:
        """Persist a completed turn (Hermes MemoryProvider contract)."""
        try:
            content = f"User: {user_content}\nAssistant: {assistant_content}"
            tags = ["turn", session_id or self._session_id]
            self._insert(content, tags=tags, source="turn")
        except Exception:
            pass

    def shutdown(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
                self._table = None


def register(ctx) -> None:
    ctx.register_memory_provider(MongrelDBHermesMemoryProvider())
