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
DEFAULT_DAEMON_BINARY = os.path.join(os.path.dirname(__file__), "vendor", "0.60.3", "mongreldb-server")
DEFAULT_ENCRYPTION = "enabled"
TABLE_NAME = "hermes_memories"
RESULT_COLUMNS = [1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 13, 14, 15, 16, 18]


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
        self._session_id = session_id
        self._user_id = kwargs.get("user_id", "default") or "default"
        self._embedder = _Embedder(self._embedding_model_name)
        self._open_db()
        if self._enrichment_mode == "llm":
            self._llm_client = self._make_llm_client()

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
            indexes = [
                {"name": "raw_text_fm", "column_id": 2, "kind": "fm"},
                {"name": "summary_fm", "column_id": 3, "kind": "fm"},
                {"name": "memory_type_bm", "column_id": 4, "kind": "bitmap"},
                {"name": "entities_mh", "column_id": 5, "kind": "minhash"},
                {"name": "projects_mh", "column_id": 6, "kind": "minhash"},
                {"name": "topics_mh", "column_id": 7, "kind": "minhash"},
                {"name": "tags_mh", "column_id": 8, "kind": "minhash"},
                {"name": "embedding_ann", "column_id": 9, "kind": "ann"},
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
                elif column_id in {5, 6, 7, 8}:
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
        return memory_id

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
        if not retrievers:
            return self._daemon_query(
                must + [{"fm_contains": {"column_id": 2, "pattern": query}}], top_k
            )
        payload = {
            "table": TABLE_NAME,
            "must": must,
            "retrievers": retrievers,
            "fusion": {"reciprocal_rank": {"constant": 60}},
            "limit": top_k,
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
                    return self._daemon_query(
                        must + [{"fm_contains": {"column_id": 2, "pattern": query}}],
                        top_k,
                    )
            else:
                return self._daemon_query(
                    must + [{"fm_contains": {"column_id": 2, "pattern": query}}], top_k
                )
        return self._rows_from_result(
            [self._cells_from_http(hit["cells"]) for hit in result["hits"]]
        )

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
    ) -> List[dict]:
        if self._mode == "daemon":
            return self._search_daemon(query, top_k, memory_type, state)
        ffi = _load_ffi()
        embedding = self._embedder.encode(query) if self._embedder else []
        sparse = _sparse_tokens(query)
        candidate_k = max(top_k * 8, 64)

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
            result = self._table.query(must, limit=top_k, projection=RESULT_COLUMNS)
            return self._rows_from_result(result)

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
                limit=top_k,
                projection=RESULT_COLUMNS,
            )
            return self._rows_from_result(result)
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
                        limit=top_k,
                        projection=RESULT_COLUMNS,
                    )
                    return self._rows_from_result(result)
                except Exception:
                    pass
            must_fm = list(must)
            must_fm.append({"kind": ffi.MDB_COND_FM_CONTAINS, "column_id": 2, "pattern": query})
            result = self._table.query(
                must_fm,
                limit=top_k,
                projection=RESULT_COLUMNS,
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
                elif col_id == 18:
                    try:
                        row["metadata"] = json.loads(value.decode() if isinstance(value, bytes) else value)
                    except Exception:
                        row["metadata"] = value
            rows.append(row)
        return rows

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return list(ALL_SCHEMAS)

    def handle_tool_call(self, tool_name: str, params: dict, **kwargs) -> Any:
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
            if self._mode == "daemon":
                result = self._daemon_request(
                    "POST",
                    "/kit/txn",
                    {"ops": [{"delete_by_pk": {"table": TABLE_NAME, "pk": memory_id}}]},
                )
                deleted = result["results"][0]["kind"] == "deleted"
                return {"success": deleted, "memory_id": memory_id}
            return {"error": "delete not implemented in embedded FFI provider"}

        return {"error": f"Unknown tool: {tool_name}"}

    def system_prompt_block(self) -> str:
        location = self._daemon_url if self._mode == "daemon" else self._db_dir
        return (
            "# MongrelDB Memory\n"
            f"Active in {self._mode} mode at {location}. Dense ANN + sparse + MinHash + bitmap + range + FM.\n"
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

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            if action != "remove":
                self._insert(content, tags=[target], source="memory_tool")
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
            if self._mode == "native" and self._db is not None:
                self._db.close()
            self._db = None
            self._table = None


def register(ctx) -> None:
    ctx.register_memory_provider(MongrelDBHermesMemoryProvider())
