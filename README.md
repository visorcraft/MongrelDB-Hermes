<p align="center">
  <img src="assets/mongrel.png" alt="MongrelDB logo" width="250" />
</p>

<h1 align="center">MongrelDB plugin for Hermes</h1>

<p align="center">
  <b>MongrelDB-backed memory for Hermes Agent - hybrid long-term memory with dense ANN, sparse retrieval, exact text, bitmaps, learned ranges, and MinHash dedup in one engine.</b>
</p>

<p align="center">
  <a href="https://github.com/visorcraft/MongrelDB"><img src="https://img.shields.io/badge/engine-MongrelDB-blue.svg" alt="MongrelDB" /></a>
  <a href="https://github.com/visorcraft/MongrelDB-Hermes/actions"><img src="https://img.shields.io/badge/CI-pending-lightgrey.svg" alt="CI" /></a>
  <a href="#license"><img src="https://img.shields.io/badge/license-MIT%20OR%20Apache--2.0-blue.svg" alt="License" /></a>
</p>

## Package

| Surface | Location | Install |
|---|---|---|
| Hermes memory provider | Repository root | `hermes plugins install visorcraft/MongrelDB-Hermes` |

## Why MongrelDB for Hermes?

Hermes memory needs more than a plain vector store. Useful long-term memory for an agent must:

- Recall facts the user explicitly mentions, including exact wording.
- Catch vague or paraphrased references to older conversations.
- Filter by memory type, project, entity, state, and time.
- Detect near-duplicate memories so the store does not grow forever.
- Return results fast enough that the agent still feels responsive.
- Keep agent memories **encrypted at rest** when the host disk or backups are not fully trusted.

MongrelDB is built around multiple AI-native indexes in one engine, so a single query can combine all of these signals. Most alternatives only provide dense vector search; frameworks like Mem0 compose several separate databases to approximate the same thing.

**Encryption at rest is on by default:** MongrelDB uses **AES-256-GCM** for sorted-run pages, WAL frames, and related on-disk artifacts (passphrase- or key-based open). You can opt out into a plaintext database if you explicitly choose to; you do not have to bolt on volume encryption just to get sealed agent memory on disk - a meaningful gap versus many vector-store and SQLite-backed memory stacks.

## What It Provides

A hybrid memory store with multiple index types:

| Signal | MongrelDB index | Used for |
|--------|-----------------|----------|
| Semantic meaning | **HNSW ANN** on dense embeddings | Vague or paraphrased recall |
| Exact keywords | **Sparse** index | Specific terms, topics, tags |
| Exact substrings | **FM-index** | Quoted phrases, error fragments |
| Metadata filtering | **Bitmap** indexes | `memory_type`, `state`, `project`, `user` |
| Time / importance / score | **PGM learned range** | Recency, importance, confidence, reinforcement |
| Duplicate detection | **MinHash** | Near-duplicate consolidation at ingestion |
| Confidentiality on disk | **AES-256-GCM encryption at rest** | On by default for sorted runs, WAL, and caches; set a passphrase/key; plaintext is the opt-out |
| Logical access control | **Username/password credentials** | Orthogonal to encryption - who may open the DB handle; can stack with encryption |

Two execution modes (configure `mode: native` or `mode: daemon`):

- **Native Rust FFI** (default): Hermes loads `libmongreldb.so` and opens the data directory **in-process** - lowest latency; that process owns the exclusive storage open for `db_dir`.
- **HTTP daemon**: Hermes is an HTTP client of `mongreldb-server`. The **daemon** owns the exclusive open; many processes can share the warm cache over HTTP. Do not also open the same data directory with native FFI while the daemon is running.

Optional **dense ANN** when `embedding_model` is set (for example `all-MiniLM-L6-v2`, 384-d). Leave it empty for model-free hybrid sparse + lexical retrieval. MongrelDB core keeps embedding generation as a pluggable layer: applications may supply vectors, Kit/server may register providers, and ANN indexes operate only on stored vectors plus model metadata.

**Locking (0.60.x):** only one process may exclusively open a given MongrelDB data directory. In-process multi-handle/thread sharing is fine. Multi-process sharing goes through the daemon, not multiple native opens of the same path.

## Requirements

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) with standalone plugin support
- [MongrelDB 0.60.2](https://github.com/visorcraft/MongrelDB/releases/tag/v0.60.2) built as:
  - `libmongreldb.so` for native mode, and/or
  - `mongreldb-server` for daemon mode
- Python 3.10+ for the plugin runtime
- Optional: `sentence-transformers` when dense ANN is enabled

No separate `libmongreldb_kit` install is needed. Native mode uses MongrelDB's C ABI; daemon mode uses the 0.60.2 Kit HTTP API shipped by `mongreldb-server`.

## Quick Start

### 1. Build MongrelDB

```bash
git clone https://github.com/visorcraft/MongrelDB.git
cd MongrelDB
git checkout v0.60.2

# C FFI shared library (native mode)
cd crates/mongreldb-ffi
cargo build --release

# HTTP daemon (daemon mode)
cd ../mongreldb-server
cargo build --release
```

### 2. Make `libmongreldb.so` discoverable

```bash
export MONGRELDB_LIB=/path/to/MongrelDB/crates/mongreldb-ffi/target/release/libmongreldb.so
```

Or install system-wide:

```bash
sudo cp /path/to/MongrelDB/crates/mongreldb-ffi/target/release/libmongreldb.so /usr/local/lib/
sudo ldconfig
```

### 3. Install the plugin

Hermes installs standalone plugins into `~/.hermes/plugins/`:

```bash
hermes plugins install visorcraft/MongrelDB-Hermes --no-enable
hermes memory setup mongreldb_hermes
```

Optional dense ANN dependency:

```bash
pip install --user sentence-transformers
```

### 4. Configure Hermes

`hermes memory setup mongreldb_hermes` prompts for native or daemon mode. For
manual or advanced configuration, edit `/home/user/.hermes/config.yaml`:

```yaml
memory:
  provider: mongreldb_hermes
  mongreldb_hermes:
    mode: native                       # native | daemon
    db_dir: /home/user/.hermes/mongreldb_hermes_data
    embedding_model: ""                # "" = model-free; "all-MiniLM-L6-v2" = dense ANN
    dim: 384
    enrichment_mode: heuristic         # heuristic | llm

    # Encryption at rest (AES-256-GCM) - set a strong passphrase; never commit it
    passphrase: "${MONGRELDB_PASSPHRASE}"

    # Optional logical auth ON TOP OF encryption (storage-layer credentials)
    # username: admin
    # password: "${MONGRELDB_DB_PASSWORD}"

    # daemon-only settings
    daemon_url: http://127.0.0.1:8453
    daemon_data_dir: /home/user/.hermes/mongreldb_hermes_data
    daemon_pidfile: /tmp/mongreldb-hermes.pid
    daemon_log: /tmp/mongreldb-hermes.log
    daemon_binary: /path/to/mongreldb-server
```

For daemon mode, leave `daemon_binary` empty to connect to a daemon you manage.
Set it to `mongreldb-server` to let the plugin start a local daemon when needed.

Enable dense ANN in config:

```yaml
    embedding_model: "all-MiniLM-L6-v2"
    dim: 384
```

### Encryption key and credentials

Encryption at rest and username/password credentials are **orthogonal**:

| Layer | What it protects | How you set it |
|-------|------------------|----------------|
| **Encryption passphrase / key** | Bytes on disk (pages, WAL, caches) - AES-256-GCM | Passphrase (or raw key file on the engine/server) when creating/opening the DB |
| **Username + password credentials** | Who may open/use the DB handle (logical access) | Storage-layer credentials; can be combined with encryption |

You can (and usually should) use **both**: losing the passphrase means the on-disk bytes are unreadable; losing the credentials means the process cannot open an auth-enforced database even with the passphrase.

#### Daemon mode (`mongreldb-server`)

Start the server with a passphrase so the data directory is encrypted, and optionally with DB credentials:

```bash
export MONGRELDB_PASSPHRASE='choose-a-long-random-passphrase'
export MONGRELDB_DB_USERNAME='admin'
export MONGRELDB_DB_PASSWORD='choose-a-strong-password'

/path/to/mongreldb-server \
  /home/user/.hermes/mongreldb_hermes_data \
  --port 8453 \
  --passphrase "$MONGRELDB_PASSPHRASE" \
  --daemon \
  --pidfile /tmp/mongreldb-hermes.pid
```

- **`--passphrase`** (or equivalent env) selects the encryption key material for create/open.
- **`MONGRELDB_DB_USERNAME` / `MONGRELDB_DB_PASSWORD`** (when set together) create/open an **encrypted+credentialed** database - auth on top of encryption.
- HTTP-facing daemon auth is separate again: e.g. **`--auth-token`** (Bearer) or **`--auth-users`** (Basic) for the Kit/SQL HTTP surface. Prefer not exposing the daemon off loopback without a reverse proxy + TLS.
- Set `MONGRELDB_DAEMON_AUTH_TOKEN` when the daemon uses `--auth-token`.

See the engine docs: [Encryption](https://github.com/visorcraft/MongrelDB/blob/master/docs/07-encryption.md) and [Credential enforcement](https://github.com/visorcraft/MongrelDB/blob/master/docs/15-credential-enforcement.md).

#### Native mode (`libmongreldb.so`)

Native opens go through the C ABI (`mongreldb_create` / `mongreldb_open` and the encrypted / with-credentials variants). Configure the same **passphrase** (encryption) and optional **username/password** (credentials) in the plugin config or environment so Hermes does not open a cleartext root by accident. Prefer injecting secrets via env (`MONGRELDB_PASSPHRASE`, `MONGRELDB_DB_USERNAME`, `MONGRELDB_DB_PASSWORD`) rather than committing them in `config.yaml`.

### 5. Restart Hermes

The plugin creates the database and table on first use.

## Documentation

- [MongrelDB setup](MongrelDB_setup.md) - build and install steps
- [Modes](MongrelDB_modes.md) - native FFI vs HTTP daemon
- [Dense ANN](MongrelDB_dense.md) - enabling `all-MiniLM-L6-v2` and model policy
- [Standalone Rust](MongrelDB_standalone.md) - using MongrelDB directly outside Hermes
- [Engine embeddings & retrieval](https://github.com/visorcraft/MongrelDB/blob/master/docs/22-embeddings-and-retrieval.md) - pluggable `EmbeddingSource` / provider registry

## Modes

| Mode | How it works | Best for |
|------|----------------|----------|
| **Native** | Loads `libmongreldb.so` in-process | Lowest latency; single Hermes process owns the DB |
| **Daemon** | Talks to `mongreldb-server` over HTTP | Warm cache; DB shared across processes / restarts |

See [MongrelDB_modes.md](MongrelDB_modes.md). Helper scripts: `start_daemon.sh`, `stop_daemon.sh` (edit paths before use).

## Comparison with Alternatives

| Capability | MongrelDB | Mem0 | ChromaDB |
|------------|-----------|------|----------|
| Dense ANN | Native HNSW | Via configured vector store | Native HNSW |
| Sparse retrieval | Native | Framework-level or store-dependent | Limited |
| Exact substring (FM) | Native | Not native | Not native |
| Bitmap metadata filtering | Native | Depends on store | Basic `where` |
| Learned range (time/score) | Native | Depends on store | Not native |
| MinHash deduplication | Native | Not native | Not native |
| Unified query planner | Yes | No | No |

- **Mem0** is a memory framework, not a database. Hybrid sparse + exact text + MinHash are not engine-native.
- **ChromaDB** is primarily a vector database; metadata and exact text are secondary; no native sparse / MinHash / learned-range stack.
- MongrelDB combines the signals Hermes needs in one engine and one query planner.

## Layout

| Path | Purpose |
|------|---------|
| `plugin.yaml` | Hermes provider manifest |
| `__init__.py` | Provider implementation and registration |
| `_ffi.py` | ctypes wrapper for `libmongreldb.so` |
| `MongrelDB_setup.md` | Build and install |
| `MongrelDB_modes.md` | Native vs daemon |
| `MongrelDB_dense.md` | Dense ANN |
| `MongrelDB_standalone.md` | Direct Rust usage notes |
| `start_daemon.sh` / `stop_daemon.sh` | Daemon lifecycle helpers |
| `assets/mongrel.png` | Logo |

## Current State

- **Native FFI** path is the primary, tested mode (model-free and dense ANN with MiniLM).
- **Daemon mode** uses the server's typed `/kit/*` HTTP API for schema creation, writes, hybrid search, filtering, and deletion. Start the server yourself or configure `daemon_binary` for local auto-start.
- Native mode requires MongrelDB 0.60.2. Daemon mode requires `mongreldb-server` 0.60.2 and uses its 0.60.2 Kit HTTP API.

## Development Notes

- Set `MONGRELDB_LIB` to a release build of `libmongreldb.so` from a MongrelDB checkout.
- A MongrelDB path is a **data directory**, not a single file.
- Switching from model-free to dense may require a fresh `db_dir` if the table was created without an embedding column / ANN index (see [MongrelDB_dense.md](MongrelDB_dense.md)).
- Do not invent weak hashed dense vectors to “fake” ANN; prefer sparse retrieval when no real model is available.

## License

MIT OR Apache-2.0
