"""MongrelDB-backed long-term memory using native FFI or the HTTP daemon.

Design:
- Dense ANN (embedding) as primary retriever.
- Sparse retrieval as complementary retriever.
- MinHash for near-duplicate detection at ingestion.
- Bitmap and range indexes for metadata filtering.
- FM-index as exact-text fallback.
- Reciprocal-rank fusion (RRF) + exact dense rerank.
"""
import base64
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

DEFAULT_DB_DIR = ""
DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
DEFAULT_DIM = 384
DEFAULT_LLM_BASE_URL = "https://api.openai.com/v1"
DEFAULT_LLM_MODEL = "gpt-4.1-mini"
DEFAULT_DAEMON_URL = "http://127.0.0.1:8453"
try:
    from .install_mongreldb import VERSION as _MDB_VERSION
except ImportError:
    from install_mongreldb import VERSION as _MDB_VERSION  # type: ignore
DEFAULT_DAEMON_BINARY = os.path.join(os.path.dirname(__file__), "vendor", _MDB_VERSION, "mongreldb-server")
DEFAULT_ENCRYPTION = "enabled"
TABLE_NAME = "hermes_memories"
RESULT_COLUMNS = [1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 13, 14, 15, 16, 17, 18]
# Near-duplicate consolidation threshold (Jaccard over topics/entities/projects/tags).
DUP_JACCARD_THRESHOLD = 0.55
# Content-token Jaccard below this, with high set overlap, is treated as a conflict.
CONFLICT_CONTENT_JACCARD = 0.35
# Recency half-life for post-search ranking (milliseconds).
RECENCY_HALF_LIFE_MS = 14 * 24 * 60 * 60 * 1000
# Decay: expire low-value memories not touched for this long.
DECAY_AGE_MS = 90 * 24 * 60 * 60 * 1000
DECAY_MAX_IMPORTANCE = 0.45
DECAY_MAX_REINFORCEMENT = 1
# Skip automatic writes for non-primary agent contexts (Hermes contract).
WRITE_SKIP_CONTEXTS = frozenset({"subagent", "cron", "flush"})
# Negation cues used for lightweight conflict detection (not NLI).
_NEGATION_CUES = frozenset(
    {
        "no",
        "not",
        "never",
        "dont",
        "don't",
        "doesnt",
        "doesn't",
        "isnt",
        "isn't",
        "wont",
        "won't",
        "cannot",
        "can't",
        "cant",
        "without",
        "avoid",
        "disable",
        "disabled",
        "false",
        "unlike",
    }
)


def _load_ffi():
    from . import _ffi

    return _ffi


def _install_binaries():
    from .install_mongreldb import install

    return install()


def _normalize_encryption(value) -> str:
    value = str(value or DEFAULT_ENCRYPTION).strip().lower()
    if value in {"enabled", "true", "yes", "on"}:
        return "enabled"
    if value in {"disabled", "false", "no", "off"}:
        return "disabled"
    raise ValueError("MongrelDB encryption must be 'enabled' or 'disabled'")


def _install_python_package(package: str) -> None:
    import importlib
    import shutil
    import sys

    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError(f"{package} is required; install uv so Hermes can install it")
    subprocess.run(
        [uv, "pip", "install", "--python", sys.executable, package],
        check=True,
        timeout=600,
    )
    importlib.invalidate_caches()

SEARCH_SCHEMA = {
    "name": "mongreldb_search",
    "description": (
        "Search long-term memory in MongrelDB. Returns memory entries ranked by "
        "relevance (dense vector + sparse + substring) with recency and reinforcement boosts. "
        "Results may include conflict annotations when opposing memories are retrieved together."
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

UPDATE_SCHEMA = {
    "name": "mongreldb_update",
    "description": (
        "Patch an existing memory in place by numeric ID. Prefer this over writing a "
        "new memory when a fact has changed, so the store does not accumulate parallel versions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {
                "type": "integer",
                "description": "Numeric ID of the memory to update.",
            },
            "content": {
                "type": "string",
                "description": "Replacement text. Omit to keep the existing content.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Replacement tags. Omit to keep existing tags.",
            },
        },
        "required": ["memory_id"],
    },
}

ALL_SCHEMAS = [SEARCH_SCHEMA, REMEMBER_SCHEMA, FORGET_SCHEMA, UPDATE_SCHEMA]



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
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError:
                _install_python_package("sentence-transformers")
                from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name)
            return self._model

    def encode(self, text: str) -> list[float]:
        if not self.model_name or self.model_name.lower() in ("none", "null", ""):
            return []
        model = self._load()
        return model.encode(text, convert_to_numpy=True).tolist()


class MongrelDBHermesMemoryProvider(MemoryProvider):
    """MongrelDB-backed long-term memory using native FFI or HTTP."""

    def __init__(self):
        self._mode = "native"
        self._db_dir = ""
        self._daemon_url = DEFAULT_DAEMON_URL
        self._daemon_data_dir = ""
        self._daemon_binary = DEFAULT_DAEMON_BINARY
        self._daemon_pidfile = "/tmp/mongreldb-hermes.pid"
        self._daemon_log = "/tmp/mongreldb-hermes.log"
        self._daemon_auth_token: Optional[str] = None
        self._encryption = DEFAULT_ENCRYPTION
        self._embedding_model_name = None
        self._embedder: Optional[_Embedder] = None
        self._dim = 0
        self._session_id = ""
        self._user_id = "default"
        self._agent_context = "primary"
        self._hermes_home = ""
        self._db = None
        self._table = None
        self._lock = threading.RLock()
        self._enrichment_mode = "heuristic"  # "heuristic" or "llm"
        self._llm_client = None
        self._llm_base_url = DEFAULT_LLM_BASE_URL
        self._llm_model = DEFAULT_LLM_MODEL
        self._llm_api_key: Optional[str] = None
        # Encryption at rest (AES-256-GCM passphrase) and optional credentials
        self._passphrase: Optional[str] = None
        self._db_username: Optional[str] = None
        self._db_password: Optional[str] = None
        # Background prefetch cache (queue_prefetch → next prefetch)
        self._prefetch_text = ""
        self._prefetch_thread: Optional[threading.Thread] = None

    @property
    def name(self) -> str:
        return "mongreldb_hermes"

    def _resolve_config(self, hermes_home: str) -> None:
        from hermes_cli.config import cfg_get, load_config

        config = load_config()
        self._mode = str(
            os.environ.get("MONGRELDB_MODE")
            or cfg_get(config, "memory", "mongreldb_hermes", "mode", default="native")
        ).lower()
        if self._mode not in {"native", "daemon"}:
            raise ValueError("MongrelDB mode must be 'native' or 'daemon'")
        self._encryption = _normalize_encryption(
            os.environ.get("MONGRELDB_ENCRYPTION")
            or cfg_get(
                config,
                "memory",
                "mongreldb_hermes",
                "encryption",
                default=DEFAULT_ENCRYPTION,
            )
        )
        if not self._db_dir:
            self._db_dir = (
                os.environ.get("MONGRELDB_EMBEDDED_DIR")
                or cfg_get(config, "memory", "mongreldb_hermes", "db_dir")
                or (hermes_home + "/mongreldb_hermes_data" if hermes_home else DEFAULT_DB_DIR)
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
        self._enrichment_mode = str(
            cfg_get(config, "memory", "mongreldb_hermes", "enrichment_mode", default="heuristic")
        ).lower()
        if self._enrichment_mode not in {"heuristic", "llm"}:
            raise ValueError("MongrelDB enrichment_mode must be 'heuristic' or 'llm'")
        self._llm_base_url = str(
            os.environ.get("MONGRELDB_LLM_BASE_URL")
            or cfg_get(config, "memory", "mongreldb_hermes", "llm_base_url", default=DEFAULT_LLM_BASE_URL)
        ).rstrip("/")
        self._llm_model = str(
            os.environ.get("MONGRELDB_LLM_MODEL")
            or cfg_get(config, "memory", "mongreldb_hermes", "llm_model", default=DEFAULT_LLM_MODEL)
        )
        self._llm_api_key = os.environ.get("MONGRELDB_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
        self._daemon_url = str(
            os.environ.get("MONGRELDB_DAEMON_URL")
            or cfg_get(config, "memory", "mongreldb_hermes", "daemon_url", default=DEFAULT_DAEMON_URL)
        ).rstrip("/")
        self._daemon_data_dir = str(
            os.environ.get("MONGRELDB_DAEMON_DATA_DIR")
            or cfg_get(config, "memory", "mongreldb_hermes", "daemon_data_dir")
            or self._db_dir
        )
        self._daemon_binary = str(
            os.environ.get("MONGRELDB_DAEMON_BINARY")
            or cfg_get(config, "memory", "mongreldb_hermes", "daemon_binary", default=DEFAULT_DAEMON_BINARY)
            or DEFAULT_DAEMON_BINARY
        )
        self._daemon_pidfile = str(
            cfg_get(config, "memory", "mongreldb_hermes", "daemon_pidfile", default=self._daemon_pidfile)
        )
        self._daemon_log = str(
            cfg_get(config, "memory", "mongreldb_hermes", "daemon_log", default=self._daemon_log)
        )
        self._daemon_auth_token = (
            os.environ.get("MONGRELDB_DAEMON_AUTH_TOKEN")
            or cfg_get(config, "memory", "mongreldb_hermes", "daemon_auth_token", default=None)
            or None
        )
        # Prefer env for secrets; config keys supported for local/dev only.
        configured_passphrase = os.environ.get("MONGRELDB_PASSPHRASE") or cfg_get(
            config, "memory", "mongreldb_hermes", "passphrase", default=None
        )
        if self._encryption == "enabled":
            from .install_mongreldb import load_or_create_passphrase

            self._passphrase = configured_passphrase or load_or_create_passphrase(hermes_home)
        else:
            self._passphrase = None
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
            from .install_mongreldb import _platform_key

            _platform_key()
            return True
        except Exception:
            return bool(
                os.environ.get("MONGRELDB_LIB")
                or os.environ.get("MONGRELDB_DAEMON_BINARY")
            )

    def get_config_schema(self) -> List[Dict[str, Any]]:
        from hermes_constants import get_hermes_home

        default_db_dir = str(get_hermes_home() / "mongreldb_hermes_data")
        return [
            {"key": "mode", "description": "Memory mode: native = default, one process; daemon = shared by multiple clients", "default": "native", "choices": ["native", "daemon"]},
            {"key": "db_dir", "description": "MongrelDB embedded data directory", "default": default_db_dir},
            {
                "key": "encryption",
                "description": "Encryption at rest: enabled = default; disabled = plaintext",
                "default": DEFAULT_ENCRYPTION,
                "choices": ["enabled", "disabled"],
            },
            {
                "key": "retrieval_mode",
                "description": "Retrieval mode: dense = semantic ANN with all-MiniLM-L6-v2; sparse = model-free",
                "default": "dense",
                "choices": ["dense", "sparse"],
            },
            {
                "key": "enrichment_mode",
                "description": "Enrichment: heuristic = local, fast, private; llm = slower, requires OpenAI-compatible API key",
                "default": "heuristic",
                "choices": ["heuristic", "llm"],
            },
            {
                "key": "llm_base_url",
                "description": "OpenAI-compatible base URL",
                "default": DEFAULT_LLM_BASE_URL,
                "when": {"enrichment_mode": "llm"},
            },
            {
                "key": "llm_model",
                "description": "OpenAI-compatible model name",
                "default": DEFAULT_LLM_MODEL,
                "when": {"enrichment_mode": "llm"},
            },
            {"key": "daemon_url", "description": "MongrelDB daemon URL", "default": DEFAULT_DAEMON_URL, "when": {"mode": "daemon"}},
            {"key": "daemon_data_dir", "description": "Daemon data directory (blank uses db_dir)", "default": "", "when": {"mode": "daemon"}},
            {"key": "daemon_binary", "description": "mongreldb-server path", "default": DEFAULT_DAEMON_BINARY, "when": {"mode": "daemon"}},
            {"key": "daemon_pidfile", "description": "Daemon PID file", "default": self._daemon_pidfile, "when": {"mode": "daemon"}},
            {"key": "daemon_log", "description": "Daemon startup log", "default": self._daemon_log, "when": {"mode": "daemon"}},
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        from hermes_cli.config import load_config, save_config

        _, server = _install_binaries()
        values = dict(values)
        retrieval_mode = values.get("retrieval_mode", "dense")
        if retrieval_mode not in {"dense", "sparse"}:
            raise ValueError("MongrelDB retrieval_mode must be 'dense' or 'sparse'")
        values["embedding_model"] = DEFAULT_EMBEDDING_MODEL if retrieval_mode == "dense" else ""
        values["dim"] = DEFAULT_DIM
        if retrieval_mode == "dense":
            print(f"  Installing/downloading embedding model: {DEFAULT_EMBEDDING_MODEL}")
            _Embedder(DEFAULT_EMBEDDING_MODEL)._load()
        values["encryption"] = _normalize_encryption(values.get("encryption"))
        if (
            values["encryption"] == "enabled"
            and not values.get("passphrase")
            and not os.environ.get("MONGRELDB_PASSPHRASE")
        ):
            from .install_mongreldb import load_or_create_passphrase

            load_or_create_passphrase(hermes_home)
        values["daemon_binary"] = values.get("daemon_binary") or server
        config = load_config()
        config.setdefault("memory", {})[self.name] = values
        save_config(config)

    def initialize(self, session_id: str, **kwargs) -> None:
        hermes_home = str(kwargs.get("hermes_home", ""))
        _install_binaries()
        self._resolve_config(hermes_home)
        if self._mode == "native":
            os.makedirs(self._db_dir, exist_ok=True)
        self._hermes_home = hermes_home
        self._session_id = session_id
        self._user_id = kwargs.get("user_id", "default") or "default"
        self._agent_context = str(kwargs.get("agent_context") or "primary")
        self._embedder = _Embedder(self._embedding_model_name)
        self._open_db()
        if self._enrichment_mode == "llm":
            self._llm_client = self._make_llm_client()

    def _writes_allowed(self) -> bool:
        return self._agent_context not in WRITE_SKIP_CONTEXTS

    def _make_llm_client(self):
        try:
            from openai import OpenAI
        except ImportError:
            _install_python_package("openai")
            from openai import OpenAI
        if not self._llm_api_key:
            raise ValueError("LLM enrichment requires MONGRELDB_LLM_API_KEY or OPENAI_API_KEY")
        return OpenAI(api_key=self._llm_api_key, base_url=self._llm_base_url)

    def _open_db(self) -> None:
        with self._lock:
            if self._table is not None:
                return
            if self._mode == "daemon":
                self._open_daemon()
                return
            ffi = _load_ffi()
            catalog = os.path.join(self._db_dir, "CATALOG")
            exists = os.path.exists(catalog)
            self._db = ffi.Database.open_or_create(
                self._db_dir,
                passphrase=self._passphrase,
                username=self._db_username,
                password=self._db_password,
            )
            if not exists:
                schema = ffi.Schema.build(
                    columns=[
                        {"id": 1, "name": "id", "ty": ffi.MDB_TYPE_INT64, "flags": ffi.MDB_COL_PRIMARY_KEY},
                        {"id": 2, "name": "raw_text", "ty": ffi.MDB_TYPE_BYTES},
                        {"id": 3, "name": "summary", "ty": ffi.MDB_TYPE_BYTES},
                        {"id": 4, "name": "memory_type", "ty": ffi.MDB_TYPE_BYTES},
                        {"id": 5, "name": "entities", "ty": ffi.MDB_TYPE_BYTES},
                        {"id": 6, "name": "projects", "ty": ffi.MDB_TYPE_BYTES},
                        {"id": 7, "name": "topics", "ty": ffi.MDB_TYPE_BYTES},
                        {"id": 8, "name": "tags", "ty": ffi.MDB_TYPE_BYTES},
                        {"id": 9, "name": "embedding", "ty": ffi.MDB_TYPE_EMBEDDING, "embedding_dim": self._dim, "flags": ffi.MDB_COL_NULLABLE},
                        {"id": 10, "name": "sparse", "ty": ffi.MDB_TYPE_BYTES},
                        {"id": 11, "name": "importance", "ty": ffi.MDB_TYPE_INT64},
                        {"id": 12, "name": "confidence", "ty": ffi.MDB_TYPE_INT64},
                        {"id": 13, "name": "created_at", "ty": ffi.MDB_TYPE_INT64},
                        {"id": 14, "name": "last_accessed_at", "ty": ffi.MDB_TYPE_INT64},
                        {"id": 15, "name": "reinforcement_count", "ty": ffi.MDB_TYPE_INT64},
                        {"id": 16, "name": "state", "ty": ffi.MDB_TYPE_BYTES},
                        {"id": 17, "name": "supersedes", "ty": ffi.MDB_TYPE_BYTES},
                        {"id": 18, "name": "metadata_json", "ty": ffi.MDB_TYPE_BYTES},
                    ],
                    indexes=[
                        {"name": "raw_text_fm", "column_id": 2, "kind": ffi.MDB_INDEX_FM},
                        {"name": "summary_fm", "column_id": 3, "kind": ffi.MDB_INDEX_FM},
                        {"name": "memory_type_bm", "column_id": 4, "kind": ffi.MDB_INDEX_BITMAP},
                        {"name": "entities_mh", "column_id": 5, "kind": ffi.MDB_INDEX_MIN_HASH},
                        {"name": "projects_mh", "column_id": 6, "kind": ffi.MDB_INDEX_MIN_HASH},
                        {"name": "topics_mh", "column_id": 7, "kind": ffi.MDB_INDEX_MIN_HASH},
                        {"name": "tags_mh", "column_id": 8, "kind": ffi.MDB_INDEX_MIN_HASH},
                        {"name": "embedding_ann", "column_id": 9, "kind": ffi.MDB_INDEX_ANN},
                        {"name": "sparse_idx", "column_id": 10, "kind": ffi.MDB_INDEX_SPARSE},
                        {"name": "importance_range", "column_id": 11, "kind": ffi.MDB_INDEX_LEARNED_RANGE},
                        {"name": "confidence_range", "column_id": 12, "kind": ffi.MDB_INDEX_LEARNED_RANGE},
                        {"name": "created_at_range", "column_id": 13, "kind": ffi.MDB_INDEX_LEARNED_RANGE},
                        {"name": "last_accessed_at_range", "column_id": 14, "kind": ffi.MDB_INDEX_LEARNED_RANGE},
                        {"name": "reinforcement_count_range", "column_id": 15, "kind": ffi.MDB_INDEX_LEARNED_RANGE},
                        {"name": "state_bm", "column_id": 16, "kind": ffi.MDB_INDEX_BITMAP},
                    ],
                )
                self._db.create_table(TABLE_NAME, schema)
            self._table = ffi.Table(self._db, TABLE_NAME)

    def _daemon_request(self, method: str, path: str, payload=None, timeout: float = 10):
        headers = {"Accept": "application/json"}
        if self._daemon_auth_token:
            headers["Authorization"] = f"Bearer {self._daemon_auth_token}"
        elif self._db_username and self._db_password:
            token = base64.b64encode(
                f"{self._db_username}:{self._db_password}".encode()
            ).decode()
            headers["Authorization"] = f"Basic {token}"
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self._daemon_url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            body = error.read().decode(errors="replace")
            raise RuntimeError(
                f"MongrelDB daemon {method} {path} failed: HTTP {error.code}: {body}"
            ) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise ConnectionError(
                f"MongrelDB daemon unavailable at {self._daemon_url}: {error}"
            ) from error
        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return body.decode(errors="replace")

    def _start_daemon(self) -> None:
        if not self._daemon_binary:
            raise ConnectionError(
                "MongrelDB daemon is not running and daemon_binary is not configured"
            )
        parsed = urllib.parse.urlsplit(self._daemon_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ConnectionError("daemon_binary can only start a local HTTP daemon")
        os.makedirs(self._daemon_data_dir, exist_ok=True)
        command = [
            self._daemon_binary,
            self._daemon_data_dir,
            "--port",
            str(parsed.port or 80),
            "--daemon",
            "--pidfile",
            self._daemon_pidfile,
        ]
        if self._passphrase:
            command.extend(["--passphrase", self._passphrase])
        if self._daemon_auth_token:
            command.extend(["--auth-token", self._daemon_auth_token])
        elif self._db_username and self._db_password:
            command.append("--auth-users")
        env = os.environ.copy()
        if self._db_username and self._db_password:
            env["MONGRELDB_DB_USERNAME"] = self._db_username
            env["MONGRELDB_DB_PASSWORD"] = self._db_password
        log_dir = os.path.dirname(self._daemon_log)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        with open(self._daemon_log, "ab") as log:
            result = subprocess.run(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                timeout=10,
                check=False,
            )
        if result.returncode:
            raise RuntimeError(
                f"mongreldb-server exited with {result.returncode}; see {self._daemon_log}"
            )
        for _ in range(50):
            try:
                self._daemon_request("GET", "/health", timeout=1)
                return
            except ConnectionError:
                time.sleep(0.1)
        raise ConnectionError(f"MongrelDB daemon did not start; see {self._daemon_log}")

    def _open_daemon(self) -> None:
        try:
            self._daemon_request("GET", "/health", timeout=2)
        except ConnectionError:
            self._start_daemon()
        tables = self._daemon_request("GET", "/tables")
        if TABLE_NAME not in tables:
            columns = [
                {"id": 1, "name": "id", "ty": "int64", "primary_key": True},
                {"id": 2, "name": "raw_text", "ty": "bytes"},
                {"id": 3, "name": "summary", "ty": "bytes"},
                {"id": 4, "name": "memory_type", "ty": "bytes"},
                {"id": 5, "name": "entities", "ty": "bytes"},
                {"id": 6, "name": "projects", "ty": "bytes"},
                {"id": 7, "name": "topics", "ty": "bytes"},
                {"id": 8, "name": "tags", "ty": "bytes"},
                {"id": 9, "name": "embedding", "ty": f"embedding({self._dim})", "nullable": True},
                {"id": 10, "name": "sparse", "ty": "bytes"},
                {"id": 11, "name": "importance", "ty": "int64"},
                {"id": 12, "name": "confidence", "ty": "int64"},
                {"id": 13, "name": "created_at", "ty": "int64"},
                {"id": 14, "name": "last_accessed_at", "ty": "int64"},
                {"id": 15, "name": "reinforcement_count", "ty": "int64"},
                {"id": 16, "name": "state", "ty": "bytes"},
                {"id": 17, "name": "supersedes", "ty": "bytes"},
                {"id": 18, "name": "metadata_json", "ty": "bytes"},
            ]
            # 0.61.x ANN defaults to BinarySign (Hamming). For dense MiniLM
            # retrieval, request full-precision Dense cosine quantization via
            # Kit index options (HTTP path supports options; C ABI does not yet).
            ann_index = {"name": "embedding_ann", "column_id": 9, "kind": "ann"}
            if self._embedding_model_name:
                ann_index["options"] = {
                    "ann": {
                        "quantization": "dense",
                        "m": 16,
                        "ef_construction": 64,
                        "ef_search": 64,
                    }
                }
            indexes = [
                {"name": "raw_text_fm", "column_id": 2, "kind": "fm"},
                {"name": "summary_fm", "column_id": 3, "kind": "fm"},
                {"name": "memory_type_bm", "column_id": 4, "kind": "bitmap"},
                {"name": "entities_mh", "column_id": 5, "kind": "minhash"},
                {"name": "projects_mh", "column_id": 6, "kind": "minhash"},
                {"name": "topics_mh", "column_id": 7, "kind": "minhash"},
                {"name": "tags_mh", "column_id": 8, "kind": "minhash"},
                ann_index,
                {"name": "sparse_idx", "column_id": 10, "kind": "sparse"},
                {"name": "importance_range", "column_id": 11, "kind": "learned_range"},
                {"name": "confidence_range", "column_id": 12, "kind": "learned_range"},
                {"name": "created_at_range", "column_id": 13, "kind": "learned_range"},
                {"name": "last_accessed_at_range", "column_id": 14, "kind": "learned_range"},
                {"name": "reinforcement_count_range", "column_id": 15, "kind": "learned_range"},
                {"name": "state_bm", "column_id": 16, "kind": "bitmap"},
            ]
            self._daemon_request(
                "POST",
                "/kit/create_table",
                {"name": TABLE_NAME, "columns": columns, "indexes": indexes},
            )
        self._table = TABLE_NAME

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
                model=self._llm_model,
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

    @staticmethod
    def _member_strings(enriched: dict, tags: Optional[List[str]] = None) -> List[str]:
        members: List[str] = []
        for key in ("topics", "entities", "projects"):
            members.extend(str(item) for item in (enriched.get(key) or []) if item)
        members.extend(str(tag) for tag in (tags or []) if tag)
        # Preserve order, drop empties/duplicates.
        return list(dict.fromkeys(m.strip() for m in members if str(m).strip()))

    @staticmethod
    def _jaccard(left: set, right: set) -> float:
        if not left and not right:
            return 1.0
        if not left or not right:
            return 0.0
        return len(left & right) / len(left | right)

    def _row_member_set(self, row: dict) -> set:
        members: set = set()
        for key in ("topics", "entities", "projects", "tags"):
            members.update(str(item) for item in (row.get(key) or []) if item)
        return members

    def _find_duplicates(
        self,
        enriched: dict,
        tags: Optional[List[str]] = None,
        exclude_id: int = 0,
    ) -> list[dict]:
        """Near-duplicate candidates via MinHash LSH + exact Jaccard verify."""
        members = self._member_strings(enriched, tags)
        if not members:
            return []
        query_set = set(members)
        # Prefer the densest MinHash column that has members for this write.
        topics = [str(t) for t in (enriched.get("topics") or []) if t]
        tag_list = [str(t) for t in (tags or []) if t]
        if topics:
            column_id, query_members = 7, topics
        elif tag_list:
            column_id, query_members = 8, tag_list
        else:
            column_id, query_members = 5, members
        query_members = query_members[:32]
        try:
            if self._mode == "daemon":
                result = self._daemon_request(
                    "POST",
                    "/kit/search",
                    {
                        "table": TABLE_NAME,
                        "must": [{"bitmap_eq": {"column_id": 16, "value": "active"}}],
                        "retrievers": [
                            {
                                "name": "minhash",
                                "weight": 1.0,
                                "min_hash": {
                                    "column_id": column_id,
                                    "members": query_members,
                                    "k": 8,
                                },
                            }
                        ],
                        "fusion": {"reciprocal_rank": {"constant": 60}},
                        "limit": 8,
                        "projection": RESULT_COLUMNS,
                    },
                )
                rows = self._rows_from_result(
                    [self._cells_from_http(hit["cells"]) for hit in result.get("hits") or []]
                )
            else:
                ffi = _load_ffi()
                result = self._table.search(
                    retrievers=[
                        {
                            "kind": ffi.MDB_RETRIEVER_MIN_HASH,
                            "column_id": column_id,
                            "name": "minhash",
                            "weight": 1.0,
                            "k": 8,
                            "members": query_members,
                        }
                    ],
                    must=[
                        {
                            "kind": ffi.MDB_COND_BITMAP_EQ,
                            "column_id": 16,
                            "bytes": b"active",
                        }
                    ],
                    fusion_kind=ffi.MDB_FUSION_RECIPROCAL_RANK,
                    fusion_constant=60,
                    limit=8,
                    projection=RESULT_COLUMNS,
                )
                rows = self._rows_from_result(result)
        except Exception:
            return []

        duplicates = []
        for row in rows:
            row_id = int(row.get("id") or 0)
            if exclude_id and row_id == exclude_id:
                continue
            score = self._jaccard(query_set, self._row_member_set(row))
            if score >= DUP_JACCARD_THRESHOLD:
                row = dict(row)
                row["jaccard"] = score
                duplicates.append(row)
        duplicates.sort(key=lambda item: (-float(item.get("jaccard", 0)), -int(item.get("id") or 0)))
        return duplicates

    def _build_row(
        self,
        memory_id: int,
        content: str,
        tags: List[str],
        enriched: dict,
        source: str,
        *,
        created_at: Optional[int] = None,
        reinforcement_count: int = 1,
        supersedes: Optional[List[int]] = None,
    ) -> list:
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
        created = int(created_at or now)
        state = b"active"
        supersedes_json = json.dumps([int(x) for x in (supersedes or [])]).encode("utf-8")

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
            (13, created),
            (14, now),
            (15, int(reinforcement_count)),
            (16, state),
            (17, supersedes_json),
            (18, metadata_json),
        ]

    def _put_row(self, row: list, content: str, tags: List[str], enriched: dict) -> None:
        if self._mode == "daemon":
            cells = []
            for column_id, value in row:
                if column_id == 9 and not value:
                    value = None
                elif column_id == 10:
                    value = _sparse_tokens(
                        content
                        + " "
                        + " ".join(tags)
                        + " "
                        + " ".join(enriched.get("topics", []))
                    )
                elif column_id in {5, 6, 7, 8, 17}:
                    value = json.loads(value)
                elif isinstance(value, bytes):
                    value = value.decode()
                cells.extend([column_id, value])
            self._daemon_request(
                "POST",
                "/kit/txn",
                {"ops": [{"put": {"table": TABLE_NAME, "cells": cells}}]},
            )
        else:
            self._table.put(row)

    def _reinforce(
        self,
        existing: dict,
        content: str,
        tags: List[str],
        enriched: dict,
        source: str,
    ) -> int:
        """Consolidate a near-duplicate into an existing memory (same PK)."""
        memory_id = int(existing["id"])
        old_content = str(existing.get("content") or "")
        # Keep the richer text when consolidating.
        if len(content.strip()) < len(old_content.strip()):
            content = old_content
            enriched = self._extract(content)
        merged_tags = list(
            dict.fromkeys([str(t) for t in (existing.get("tags") or []) if t] + [str(t) for t in tags if t])
        )
        for key in ("topics", "entities", "projects"):
            merged = list(
                dict.fromkeys(
                    [str(x) for x in (existing.get(key) or []) if x]
                    + [str(x) for x in (enriched.get(key) or []) if x]
                )
            )
            enriched[key] = merged
        count = int(existing.get("reinforcement_count") or 1) + 1
        created_at = existing.get("created_at")
        supersedes = []
        raw_super = existing.get("supersedes")
        if isinstance(raw_super, list):
            supersedes = [int(x) for x in raw_super if str(x).isdigit() or isinstance(x, int)]
        self._delete(memory_id)
        row = self._build_row(
            memory_id,
            content,
            merged_tags,
            enriched,
            source,
            created_at=created_at,
            reinforcement_count=count,
            supersedes=supersedes,
        )
        self._put_row(row, content, merged_tags, enriched)
        return memory_id

    @staticmethod
    def _content_tokens(text: str) -> set:
        return {w for w in re.findall(r"\w+", (text or "").lower()) if len(w) > 2}

    def _looks_like_conflict(self, left: dict, right_content: str, right_members: set) -> bool:
        """Heuristic contradiction: high set overlap, low content overlap, or negation flip."""
        left_content = str(left.get("content") or left.get("summary") or "")
        left_tokens = self._content_tokens(left_content)
        right_tokens = self._content_tokens(right_content)
        content_j = self._jaccard(left_tokens, right_tokens)
        member_j = self._jaccard(self._row_member_set(left), right_members)
        if member_j < DUP_JACCARD_THRESHOLD and content_j > 0.5:
            return False
        if content_j <= CONFLICT_CONTENT_JACCARD and member_j >= DUP_JACCARD_THRESHOLD:
            return True
        left_neg = bool(left_tokens & _NEGATION_CUES)
        right_neg = bool(right_tokens & _NEGATION_CUES)
        shared = left_tokens & right_tokens
        if left_neg != right_neg and len(shared) >= 2:
            return True
        return False

    def _get_by_pk(self, memory_id: int) -> Optional[dict]:
        memory_id = int(memory_id)
        if self._mode == "daemon":
            rows = self._daemon_query(
                [{"pk": {"value": memory_id}}],
                1,
            )
            return rows[0] if rows else None
        ffi = _load_ffi()
        key = memory_id.to_bytes(8, "big", signed=True)
        result = self._table.query(
            [{"kind": ffi.MDB_COND_PK, "bytes": key}],
            limit=1,
            projection=RESULT_COLUMNS,
        )
        rows = self._rows_from_result(result)
        return rows[0] if rows else None

    def _update(
        self,
        memory_id: int,
        *,
        content: Optional[str] = None,
        tags: Optional[List[str]] = None,
        source: str = "update",
        reinforce: bool = True,
    ) -> Optional[int]:
        """Replace fields of an existing memory in place (same primary key)."""
        if not self._writes_allowed():
            return None
        existing = self._get_by_pk(memory_id)
        if not existing:
            return None
        new_content = content if content is not None else str(existing.get("content") or "")
        if not new_content.strip():
            return None
        new_tags = (
            [str(t) for t in tags]
            if tags is not None
            else [str(t) for t in (existing.get("tags") or [])]
        )
        enriched = self._extract(new_content)
        # Preserve entity/project lineage from the prior row when enrichment is sparse.
        for key in ("entities", "projects"):
            if not enriched.get(key) and existing.get(key):
                enriched[key] = list(existing.get(key) or [])
        count = int(existing.get("reinforcement_count") or 1)
        if reinforce:
            count += 1
        supersedes = []
        raw_super = existing.get("supersedes")
        if isinstance(raw_super, list):
            supersedes = [int(x) for x in raw_super if isinstance(x, int) or str(x).isdigit()]
        self._delete(memory_id)
        row = self._build_row(
            memory_id,
            new_content,
            new_tags,
            enriched,
            source,
            created_at=existing.get("created_at"),
            reinforcement_count=count,
            supersedes=supersedes,
        )
        self._put_row(row, new_content, new_tags, enriched)
        return memory_id

    def _insert(self, content: str, *, tags: List[str], source: str) -> int:
        if not self._writes_allowed():
            return 0
        enriched = self._extract(content)
        members = set(self._member_strings(enriched, tags))
        duplicates = self._find_duplicates(enriched, tags=tags)
        if duplicates:
            best = duplicates[0]
            if self._looks_like_conflict(best, content, members):
                # Conflicting fact: write new memory that supersedes the old one,
                # and demote the older row's state so search prefers the new one.
                memory_id = _now_ms()
                row = self._build_row(
                    memory_id,
                    content,
                    tags,
                    enriched,
                    source,
                    supersedes=[int(best["id"])],
                )
                self._put_row(row, content, tags, enriched)
                try:
                    self._set_state(int(best["id"]), "superseded")
                except Exception:
                    pass
                return memory_id
            return self._reinforce(best, content, tags, enriched, source)
        memory_id = _now_ms()
        row = self._build_row(memory_id, content, tags, enriched, source)
        self._put_row(row, content, tags, enriched)
        return memory_id

    def _set_state(self, memory_id: int, state: str) -> bool:
        """Rewrite a row with a new lifecycle state (active/superseded/expired)."""
        existing = self._get_by_pk(memory_id)
        if not existing:
            return False
        content = str(existing.get("content") or "")
        tags = [str(t) for t in (existing.get("tags") or [])]
        enriched = {
            "summary": existing.get("summary") or content[:200],
            "memory_type": existing.get("memory_type") or "fact",
            "entities": existing.get("entities") or [],
            "projects": existing.get("projects") or [],
            "topics": existing.get("topics") or [],
            "importance": existing.get("importance", 0.5),
            "confidence": existing.get("confidence", 0.8),
        }
        supersedes = existing.get("supersedes") if isinstance(existing.get("supersedes"), list) else []
        source = "state"
        meta = existing.get("metadata") if isinstance(existing.get("metadata"), dict) else {}
        if isinstance(meta, dict) and meta.get("source"):
            source = str(meta.get("source"))
        self._delete(memory_id)
        row = self._build_row(
            memory_id,
            content,
            tags,
            enriched,
            source,
            created_at=existing.get("created_at"),
            reinforcement_count=int(existing.get("reinforcement_count") or 1),
            supersedes=[int(x) for x in supersedes if isinstance(x, int) or str(x).isdigit()],
        )
        # Patch state cell (column 16).
        row = [(col, (state.encode("utf-8") if col == 16 else val)) for col, val in row]
        # Keep prior last_accessed unless activating.
        if state != "active" and existing.get("last_accessed_at"):
            row = [
                (col, (int(existing["last_accessed_at"]) if col == 14 else val))
                for col, val in row
            ]
        self._put_row(row, content, tags, enriched)
        return True

    def _delete(self, memory_id: int) -> bool:
        """Delete one memory by primary key. Returns True if a row was removed."""
        if self._mode == "daemon":
            result = self._daemon_request(
                "POST",
                "/kit/txn",
                {"ops": [{"delete_by_pk": {"table": TABLE_NAME, "pk": memory_id}}]},
            )
            results = result.get("results") or []
            if not results:
                return False
            return results[0].get("kind") == "deleted"
        return bool(self._table.delete_by_pk(memory_id))

    def _delete_matching_content(self, content: str) -> int:
        """Delete active memories whose content or summary equals ``content``."""
        if not content:
            return 0
        needle = content.strip()
        matches = self._search(needle, top_k=20, apply_decay=False, touch=False)
        deleted = 0
        for row in matches:
            body = str(row.get("content") or "").strip()
            summary = str(row.get("summary") or "").strip()
            if body == needle or summary == needle or needle in body:
                if row.get("id") is not None and self._delete(int(row["id"])):
                    deleted += 1
        return deleted

    def _rank_with_recency(self, rows: List[dict]) -> List[dict]:
        """Blend retrieval order with recency, reinforcement, and importance.

        MongrelDB already stores created_at / last_accessed_at (range-indexed).
        Hybrid search fuses ANN + sparse only; this post-sort uses those
        timestamps as an additional ranking signal.
        """
        if not rows:
            return rows
        now = _now_ms()
        ranked = []
        for index, row in enumerate(rows):
            item = dict(row)
            position = 1.0 / (60.0 + index + 1.0)
            ts = int(item.get("last_accessed_at") or item.get("created_at") or 0)
            age = max(0, now - ts) if ts else RECENCY_HALF_LIFE_MS * 4
            recency = 0.5 ** (age / float(RECENCY_HALF_LIFE_MS))
            reinf = min(int(item.get("reinforcement_count") or 1), 20) / 20.0
            importance = float(item.get("importance") or 0.5)
            # Soft penalty when the row is annotated as conflicting with another hit.
            conflict_penalty = 0.15 if item.get("conflicts_with") else 0.0
            score = (
                0.50 * position
                + 0.30 * recency
                + 0.12 * reinf
                + 0.08 * importance
                - conflict_penalty
            )
            item["score"] = score
            item["recency"] = recency
            ranked.append(item)
        ranked.sort(key=lambda r: (-float(r.get("score") or 0), -int(r.get("id") or 0)))
        return ranked

    def _annotate_conflicts(self, rows: List[dict]) -> List[dict]:
        """Mark pairs in a result set that look contradictory."""
        if len(rows) < 2:
            return rows
        annotated = [dict(r) for r in rows]
        for i in range(len(annotated)):
            for j in range(i + 1, len(annotated)):
                a, b = annotated[i], annotated[j]
                if self._looks_like_conflict(
                    a, str(b.get("content") or ""), self._row_member_set(b)
                ):
                    a.setdefault("conflicts_with", [])
                    b.setdefault("conflicts_with", [])
                    if b.get("id") is not None and int(b["id"]) not in a["conflicts_with"]:
                        a["conflicts_with"].append(int(b["id"]))
                    if a.get("id") is not None and int(a["id"]) not in b["conflicts_with"]:
                        b["conflicts_with"].append(int(a["id"]))
        return annotated

    def _touch_last_accessed(self, rows: List[dict]) -> None:
        """Bump last_accessed_at for retrieved active rows (feeds recency)."""
        if not rows or not self._writes_allowed():
            return
        now = _now_ms()
        for row in rows[:5]:
            memory_id = row.get("id")
            if memory_id is None:
                continue
            try:
                existing = self._get_by_pk(int(memory_id))
                if not existing or existing.get("state", "active") != "active":
                    continue
                content = str(existing.get("content") or "")
                tags = [str(t) for t in (existing.get("tags") or [])]
                enriched = {
                    "summary": existing.get("summary") or content[:200],
                    "memory_type": existing.get("memory_type") or "fact",
                    "entities": existing.get("entities") or [],
                    "projects": existing.get("projects") or [],
                    "topics": existing.get("topics") or [],
                    "importance": existing.get("importance", 0.5),
                    "confidence": existing.get("confidence", 0.8),
                }
                supersedes = (
                    existing.get("supersedes")
                    if isinstance(existing.get("supersedes"), list)
                    else []
                )
                source = "touch"
                meta = existing.get("metadata") if isinstance(existing.get("metadata"), dict) else {}
                if isinstance(meta, dict) and meta.get("source"):
                    source = str(meta.get("source"))
                self._delete(int(memory_id))
                row_cells = self._build_row(
                    int(memory_id),
                    content,
                    tags,
                    enriched,
                    source,
                    created_at=existing.get("created_at"),
                    reinforcement_count=int(existing.get("reinforcement_count") or 1),
                    supersedes=[
                        int(x)
                        for x in supersedes
                        if isinstance(x, int) or str(x).isdigit()
                    ],
                )
                # Force last_accessed_at = now (column 14).
                row_cells = [(c, (now if c == 14 else v)) for c, v in row_cells]
                self._put_row(row_cells, content, tags, enriched)
            except Exception:
                continue

    def _expire_stale_memories(self, limit: int = 32) -> int:
        """Mark low-importance, never-reinforced, long-untouched memories expired."""
        if not self._writes_allowed():
            return 0
        cutoff = _now_ms() - DECAY_AGE_MS
        try:
            if self._mode == "daemon":
                candidates = self._daemon_query(
                    [
                        {"bitmap_eq": {"column_id": 16, "value": "active"}},
                        {"range": {"column_id": 14, "lo": 0, "hi": cutoff}},
                    ],
                    limit,
                )
            else:
                ffi = _load_ffi()
                result = self._table.query(
                    [
                        {
                            "kind": ffi.MDB_COND_BITMAP_EQ,
                            "column_id": 16,
                            "bytes": b"active",
                        },
                        {
                            "kind": ffi.MDB_COND_RANGE_INT,
                            "column_id": 14,
                            "lo": 0,
                            "hi": cutoff,
                        },
                    ],
                    limit=limit,
                    projection=RESULT_COLUMNS,
                )
                candidates = self._rows_from_result(result)
        except Exception:
            return 0
        expired = 0
        for row in candidates:
            importance = float(row.get("importance") or 0.5)
            reinf = int(row.get("reinforcement_count") or 1)
            if importance > DECAY_MAX_IMPORTANCE or reinf > DECAY_MAX_REINFORCEMENT:
                continue
            if row.get("id") is not None and self._set_state(int(row["id"]), "expired"):
                expired += 1
        return expired

    def _finalize_results(
        self,
        rows: List[dict],
        top_k: int,
        *,
        touch: bool = True,
    ) -> List[dict]:
        rows = self._annotate_conflicts(rows)
        rows = self._rank_with_recency(rows)
        rows = rows[:top_k]
        if touch:
            try:
                self._touch_last_accessed(rows)
            except Exception:
                pass
        return rows

    def _daemon_query(self, conditions: List[dict], top_k: int) -> List[dict]:
        result = self._daemon_request(
            "POST",
            "/kit/query",
            {
                "table": TABLE_NAME,
                "conditions": conditions,
                "projection": RESULT_COLUMNS,
                "limit": top_k,
            },
        )
        return self._rows_from_result(
            [self._cells_from_http(row["cells"]) for row in result["rows"]]
        )

    def _search_daemon(
        self,
        query: str,
        top_k: int,
        memory_type: Optional[str],
        state: str,
        project: Optional[str] = None,
        entity: Optional[str] = None,
        *,
        touch: bool = True,
    ) -> List[dict]:
        embedding = self._embedder.encode(query) if self._embedder else []
        sparse = _sparse_tokens(query)
        candidate_k = max(top_k * 8, 64)
        must = []
        if state:
            must.append({"bitmap_eq": {"column_id": 16, "value": state}})
        if memory_type:
            must.append({"bitmap_eq": {"column_id": 4, "value": memory_type}})
        retrievers = []
        if embedding and len(embedding) == self._dim:
            retrievers.append(
                {
                    "name": "ann",
                    "weight": 1.0,
                    "ann": {"column_id": 9, "query": embedding, "k": candidate_k},
                }
            )
        if sparse:
            retrievers.append(
                {
                    "name": "sparse",
                    "weight": 1.0,
                    "sparse": {"column_id": 10, "query": sparse, "k": candidate_k},
                }
            )
        # Overfetch so recency re-rank can promote newer hits that landed lower.
        fetch_k = max(top_k * 4, 16)
        if not retrievers:
            rows = self._daemon_query(
                must + [{"fm_contains": {"column_id": 2, "pattern": query}}], fetch_k
            )
            rows = self._filter_project_entity(rows, project, entity)
            return self._finalize_results(rows, top_k, touch=touch)
        payload = {
            "table": TABLE_NAME,
            "must": must,
            "retrievers": retrievers,
            "fusion": {"reciprocal_rank": {"constant": 60}},
            "limit": fetch_k,
            "projection": RESULT_COLUMNS,
        }
        if embedding and len(embedding) == self._dim:
            payload["rerank"] = {
                "exact_vector": {
                    "embedding_column": 9,
                    "query": embedding,
                    "metric": "cosine",
                    "candidate_limit": candidate_k,
                    "weight": 1.0,
                }
            }
        try:
            result = self._daemon_request("POST", "/kit/search", payload)
        except RuntimeError:
            if sparse and len(retrievers) > 1:
                payload.pop("rerank", None)
                payload["retrievers"] = [retrievers[-1]]
                try:
                    result = self._daemon_request("POST", "/kit/search", payload)
                except RuntimeError:
                    rows = self._daemon_query(
                        must + [{"fm_contains": {"column_id": 2, "pattern": query}}],
                        fetch_k,
                    )
                    rows = self._filter_project_entity(rows, project, entity)
                    return self._finalize_results(rows, top_k, touch=touch)
            else:
                rows = self._daemon_query(
                    must + [{"fm_contains": {"column_id": 2, "pattern": query}}], fetch_k
                )
                rows = self._filter_project_entity(rows, project, entity)
                return self._finalize_results(rows, top_k, touch=touch)
        rows = self._rows_from_result(
            [self._cells_from_http(hit["cells"]) for hit in result["hits"]]
        )
        rows = self._filter_project_entity(rows, project, entity)
        return self._finalize_results(rows, top_k, touch=touch)

    @staticmethod
    def _filter_project_entity(
        rows: List[dict],
        project: Optional[str],
        entity: Optional[str],
    ) -> List[dict]:
        """Hard membership filter after retrieval (exact set contains)."""
        if not project and not entity:
            return rows
        filtered = []
        for row in rows:
            if project and project not in [str(x) for x in (row.get("projects") or [])]:
                continue
            if entity and entity not in [str(x) for x in (row.get("entities") or [])]:
                continue
            filtered.append(row)
        return filtered

    @staticmethod
    def _cells_from_http(cells: List[Any]) -> Dict[int, Any]:
        return {int(cells[index]): cells[index + 1] for index in range(0, len(cells), 2)}

    def _search(
        self,
        query: str,
        top_k: int = 8,
        memory_type: Optional[str] = None,
        project: Optional[str] = None,
        entity: Optional[str] = None,
        state: str = "active",
        *,
        apply_decay: bool = True,
        touch: bool = True,
    ) -> List[dict]:
        if apply_decay:
            try:
                self._expire_stale_memories()
            except Exception:
                pass
        if self._mode == "daemon":
            return self._search_daemon(
                query,
                top_k,
                memory_type,
                state,
                project=project,
                entity=entity,
                touch=touch,
            )
        ffi = _load_ffi()
        embedding = self._embedder.encode(query) if self._embedder else []
        sparse = _sparse_tokens(query)
        candidate_k = max(top_k * 8, 64)
        # Overfetch so recency re-rank can promote newer hits that landed lower.
        fetch_k = max(top_k * 4, 16)

        must = []
        if state:
            must.append({"kind": ffi.MDB_COND_BITMAP_EQ, "column_id": 16, "bytes": state.encode("utf-8")})
        if memory_type:
            must.append({"kind": ffi.MDB_COND_BITMAP_EQ, "column_id": 4, "bytes": memory_type.encode("utf-8")})

        retrievers = []
        if embedding and len(embedding) == self._dim:
            retrievers.append({
                "kind": ffi.MDB_RETRIEVER_ANN,
                "column_id": 9,
                "name": "ann",
                "weight": 1.0,
                "k": candidate_k,
                "embedding": [float(x) for x in embedding],
            })
        if sparse:
            retrievers.append({
                "kind": ffi.MDB_RETRIEVER_SPARSE,
                "column_id": 10,
                "name": "sparse",
                "weight": 1.0,
                "k": candidate_k,
                "sparse": sparse,
            })
        if not retrievers:
            must.append({"kind": ffi.MDB_COND_FM_CONTAINS, "column_id": 2, "pattern": query})
            result = self._table.query(must, limit=fetch_k, projection=RESULT_COLUMNS)
            rows = self._filter_project_entity(
                self._rows_from_result(result), project, entity
            )
            return self._finalize_results(rows, top_k, touch=touch)

        rerank = None
        if embedding and len(embedding) == self._dim:
            rerank = {
                "embedding_column": 9,
                "query": [float(x) for x in embedding],
                "metric": ffi.MDB_SEARCH_METRIC_COSINE,
                "candidate_limit": candidate_k,
                "weight": 1.0,
            }

        try:
            result = self._table.search(
                retrievers=retrievers,
                must=must,
                fusion_kind=ffi.MDB_FUSION_RECIPROCAL_RANK,
                fusion_constant=60,
                rerank=rerank,
                limit=fetch_k,
                projection=RESULT_COLUMNS,
            )
            rows = self._filter_project_entity(
                self._rows_from_result(result), project, entity
            )
            return self._finalize_results(rows, top_k, touch=touch)
        except Exception:
            # Fall back when ANN is missing from an older empty checkpoint, or
            # embeddings were never written: sparse-only, then FM contains.
            if sparse:
                try:
                    result = self._table.search(
                        retrievers=[{
                            "kind": ffi.MDB_RETRIEVER_SPARSE,
                            "column_id": 10,
                            "name": "sparse",
                            "weight": 1.0,
                            "k": candidate_k,
                            "sparse": sparse,
                        }],
                        must=must,
                        fusion_kind=ffi.MDB_FUSION_RECIPROCAL_RANK,
                        fusion_constant=60,
                        rerank=None,
                        limit=fetch_k,
                        projection=RESULT_COLUMNS,
                    )
                    rows = self._filter_project_entity(
                        self._rows_from_result(result), project, entity
                    )
                    return self._finalize_results(rows, top_k, touch=touch)
                except Exception:
                    pass
            must_fm = list(must)
            must_fm.append({"kind": ffi.MDB_COND_FM_CONTAINS, "column_id": 2, "pattern": query})
            result = self._table.query(
                must_fm,
                limit=fetch_k,
                projection=RESULT_COLUMNS,
            )
            rows = self._filter_project_entity(
                self._rows_from_result(result), project, entity
            )
            return self._finalize_results(rows, top_k, touch=touch)

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
                        row[key] = json.loads(value.decode() if isinstance(value, bytes) else value)
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
                elif col_id == 17:
                    try:
                        row["supersedes"] = json.loads(
                            value.decode() if isinstance(value, bytes) else value
                        )
                    except Exception:
                        row["supersedes"] = value
                elif col_id == 18:
                    try:
                        row["metadata"] = json.loads(value.decode() if isinstance(value, bytes) else value)
                    except Exception:
                        row["metadata"] = value
            rows.append(row)
        return rows

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return list(ALL_SCHEMAS)

    def handle_tool_call(self, tool_name: str, params: dict, **kwargs) -> str:
        # Hermes MemoryProvider contract: tool results are JSON strings.
        if tool_name == "mongreldb_search":
            query = params.get("query", "")
            top_k = min(int(params.get("top_k", 8) or 8), 20)
            memory_type = params.get("memory_type")
            project = params.get("project")
            entity = params.get("entity")
            results = self._search(
                query,
                top_k=top_k,
                memory_type=memory_type,
                project=project,
                entity=entity,
            )
            return json.dumps({"results": results, "count": len(results)})

        if tool_name == "mongreldb_remember":
            content = params.get("content", "")
            tags = params.get("tags", []) or []
            if not content:
                return json.dumps({"error": "content is required"})
            source = params.get("source", "tool")
            memory_id = self._insert(content, tags=tags, source=source)
            return json.dumps({"success": True, "memory_id": memory_id})

        if tool_name == "mongreldb_forget":
            memory_id = params.get("memory_id")
            if memory_id is None:
                return json.dumps({"error": "memory_id is required"})
            try:
                memory_id = int(memory_id)
            except (TypeError, ValueError):
                return json.dumps({"error": "memory_id must be an integer"})
            deleted = self._delete(memory_id)
            return json.dumps({"success": deleted, "memory_id": memory_id})

        if tool_name == "mongreldb_update":
            memory_id = params.get("memory_id")
            if memory_id is None:
                return json.dumps({"error": "memory_id is required"})
            try:
                memory_id = int(memory_id)
            except (TypeError, ValueError):
                return json.dumps({"error": "memory_id must be an integer"})
            content = params.get("content")
            tags = params.get("tags")
            if content is None and tags is None:
                return json.dumps({"error": "content or tags is required"})
            updated = self._update(
                memory_id,
                content=content,
                tags=tags,
                source="tool",
            )
            if updated is None:
                return json.dumps({"success": False, "memory_id": memory_id, "error": "not found"})
            return json.dumps({"success": True, "memory_id": updated})

        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    def system_prompt_block(self) -> str:
        location = self._daemon_url if self._mode == "daemon" else self._db_dir
        return (
            "# MongrelDB Memory\n"
            f"Active in {self._mode} mode at {location}. "
            "Dense ANN + sparse + MinHash + bitmap + range indexes + recency ranking.\n"
            "Use mongreldb_search to recall facts (results may include conflicts_with annotations), "
            "mongreldb_remember to store facts, mongreldb_update to patch an existing memory by ID, "
            "and mongreldb_forget to delete a memory by ID. Prefer update over writing a parallel fact."
        )

    def _format_prefetch(self, results: List[dict]) -> str:
        if not results:
            return ""
        lines = ["[MongrelDB Memory]"]
        for row in results:
            lines.append(f"- {row.get('summary', row.get('content', ''))}")
        return "\n".join(lines)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        with self._lock:
            cached = self._prefetch_text
            self._prefetch_text = ""
        if cached:
            return cached
        try:
            return self._format_prefetch(self._search(query, top_k=5))
        except Exception:
            return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Background recall for the next turn's prefetch()."""
        if not query or not query.strip():
            return
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=0.5)

        def _run():
            try:
                text = self._format_prefetch(self._search(query, top_k=5))
                with self._lock:
                    self._prefetch_text = text
            except Exception:
                pass

        thread = threading.Thread(
            target=_run, daemon=True, name="mongreldb-prefetch"
        )
        self._prefetch_thread = thread
        thread.start()

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self._writes_allowed():
            return
        metadata = metadata or {}
        try:
            if action == "remove":
                self._delete_matching_content(content or str(metadata.get("old_text") or ""))
                return
            if action == "replace":
                old_text = str(metadata.get("old_text") or "")
                if old_text:
                    self._delete_matching_content(old_text)
            if action in {"add", "replace"} and content:
                tags = [target] if target else []
                self._insert(content, tags=tags, source="memory_tool")
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
        if not self._writes_allowed():
            return
        try:
            content = f"User: {user_content}\nAssistant: {assistant_content}"
            tags = ["turn", session_id or self._session_id]
            self._insert(content, tags=tags, source="turn")
        except Exception:
            pass

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Heuristic extraction of sticky facts from the closing transcript."""
        if not self._writes_allowed() or not messages:
            return
        try:
            self._expire_stale_memories()
        except Exception:
            pass
        preference = re.compile(
            r"\bI\s+(?:prefer|like|love|use|want|need|always|never|usually)\s+(.+)",
            re.IGNORECASE,
        )
        decision = re.compile(
            r"\b(?:we|let's|lets)\s+(?:decided|agreed|chose|should|will)\s+(?:to\s+)?(.+)",
            re.IGNORECASE,
        )
        seen: set = set()
        for message in messages[-40:]:
            if message.get("role") != "user":
                continue
            content = message.get("content") or ""
            if not isinstance(content, str) or not content.strip():
                continue
            for pattern, memory_type in ((preference, "preference"), (decision, "decision")):
                match = pattern.search(content)
                if not match:
                    continue
                fact = content.strip()
                if len(fact) < 12 or fact in seen:
                    continue
                seen.add(fact)
                try:
                    # Force type via tags; enrichment still runs.
                    self._insert(
                        fact,
                        tags=["session_end", memory_type, self._session_id or ""],
                        source="session_end",
                    )
                except Exception:
                    pass

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        self._session_id = new_session_id or self._session_id
        if reset or rewound:
            with self._lock:
                self._prefetch_text = ""

    def on_delegation(
        self,
        task: str,
        result: str,
        *,
        child_session_id: str = "",
        **kwargs,
    ) -> None:
        if not self._writes_allowed():
            return
        task = (task or "").strip()
        result = (result or "").strip()
        if not task and not result:
            return
        content = f"Delegated: {task[:500]}\nResult: {result[:1000]}"
        try:
            self._insert(
                content,
                tags=["delegation", child_session_id or "", self._session_id or ""],
                source="delegation",
            )
        except Exception:
            pass

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Surface top recalled lines so the compressor can keep them."""
        if not messages:
            return ""
        snippets = []
        for message in messages[-12:]:
            role = message.get("role")
            content = message.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, str):
                continue
            text = content.strip()
            if text:
                snippets.append(text[:240])
        if not snippets:
            return ""
        query = " ".join(snippets)[:500]
        try:
            results = self._search(query, top_k=5)
        except Exception:
            return ""
        if not results:
            return ""
        lines = ["MongrelDB memories relevant to discarded context:"]
        for row in results:
            lines.append(f"- {row.get('summary', row.get('content', ''))}")
        return "\n".join(lines)

    def backup_paths(self) -> List[str]:
        """Declare data/key paths when they live outside HERMES_HOME."""
        paths = []
        home = os.path.expanduser(self._hermes_home or os.environ.get("HERMES_HOME") or "")
        for path in (self._db_dir, self._daemon_data_dir):
            if not path:
                continue
            abs_path = os.path.abspath(os.path.expanduser(path))
            if home and abs_path.startswith(os.path.abspath(home) + os.sep):
                continue
            paths.append(abs_path)
        key_path = os.path.join(
            os.path.expanduser(home or "~/.hermes"), "mongreldb_hermes.key"
        )
        if home:
            key_abs = os.path.abspath(key_path)
            if not key_abs.startswith(os.path.abspath(home) + os.sep):
                paths.append(key_abs)
        return paths

    def shutdown(self) -> None:
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=2.0)
        with self._lock:
            if self._mode == "native" and self._db is not None:
                self._db.close()
            self._db = None
            self._table = None
            self._prefetch_text = ""


def register(ctx) -> None:
    ctx.register_memory_provider(MongrelDBHermesMemoryProvider())
