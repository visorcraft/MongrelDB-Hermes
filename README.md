<p align="center">
  <img src="assets/mongrel.png" alt="MongrelDB logo" width="250" />
</p>

<h1 align="center">MongrelDB plugin for Hermes</h1>

<p align="center">
  <b>MongrelDB-backed memory for Hermes Agent - hybrid long-term memory with dense ANN, sparse retrieval, exact text, bitmaps, learned ranges, and MinHash dedup in one engine.</b>
</p>

<p align="center">
  <a href="https://github.com/visorcraft/MongrelDB"><img src="https://img.shields.io/badge/engine-MongrelDB-blue.svg" alt="MongrelDB" /></a>
  <a href="#license"><img src="https://img.shields.io/badge/license-MIT%20OR%20Apache--2.0-blue.svg" alt="License" /></a>
</p>

## Quick Start

```bash
hermes plugins install visorcraft/MongrelDB-Hermes --no-enable
hermes memory setup
```

Select `mongreldb_hermes` in memory setup. It is an exclusive memory provider, not a general Hermes plugin. It does not need `hermes plugins enable mongreldb_hermes`. Ignore Hermes' generic plugin-enable and gateway-restart instructions after installation; memory setup selects the provider, shows its configuration choices, and downloads both MongrelDB runtimes.

## Why MongrelDB for Hermes?

Hermes memory needs more than a plain vector store. Useful long-term memory for an agent must:

- Recall facts the user explicitly mentions, including exact wording.
- Catch vague or paraphrased references to older conversations.
- Filter by memory type, project, entity, state, and time.
- Detect near-duplicate memories so the store does not grow forever.
- Return results fast enough that the agent still feels responsive.
- Keep agent memories **encrypted at rest** when the host disk or backups are not fully trusted.

MongrelDB is built around multiple AI-native indexes in one engine, so a single query can combine all of these signals. Most alternatives only provide dense vector search; frameworks like Mem0 compose several separate databases to approximate the same thing.

**Encryption at rest is on by default:** the plugin creates a random passphrase in `~/.hermes/mongreldb_hermes.key` with mode `0600`, then uses MongrelDB's **AES-256-GCM** encrypted create/open path in both native and daemon modes. Set `encryption: disabled` only when plaintext storage is intentional.

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
| Confidentiality on disk | **AES-256-GCM encryption at rest** | On by default; a passphrase is generated when none is supplied; plaintext is the opt-out |
| Logical access control | **Username/password credentials** | Orthogonal to encryption - who may open the DB handle; can stack with encryption |

Two execution modes (configure `mode: native` or `mode: daemon`):

- **Native Rust FFI** (default): Hermes loads `libmongreldb.so` and opens the data directory **in-process** - lowest latency; that process owns the exclusive storage open for `db_dir`.
- **HTTP daemon**: Hermes is an HTTP client of `mongreldb-server`. The **daemon** owns the exclusive open; many processes can share the warm cache over HTTP. Do not also open the same data directory with native FFI while the daemon is running.

Optional **dense ANN** when `embedding_model` is set (for example `all-MiniLM-L6-v2`, 384-d). Leave it empty for model-free hybrid sparse + lexical retrieval. MongrelDB core keeps embedding generation as a pluggable layer: applications may supply vectors, Kit/server may register providers, and ANN indexes operate only on stored vectors plus model metadata.

**Locking (0.60.x):** only one process may exclusively open a given MongrelDB data directory. In-process multi-handle/thread sharing is fine. Multi-process sharing goes through the daemon, not multiple native opens of the same path.

## Requirements

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) with standalone plugin support
- Python 3.10+ for the plugin runtime
- Linux x64 glibc/musl, Linux arm64 glibc, or macOS x64/arm64
- Optional: `sentence-transformers` when dense ANN is enabled

Plugin 1.0.0 targets MongrelDB 0.60.2. When memory settings are saved, or on first provider start if setup was skipped, it downloads the matching native archive and daemon binary from the [MongrelDB 0.60.2 release](https://github.com/visorcraft/MongrelDB/releases/tag/v0.60.2). It verifies both SHA-256 digests, keeps only the shared library and `mongreldb-server`, and deletes the downloads. Linux x64 glibc downloads about 349 MB once and keeps about 397 MB. No Rust toolchain or separate `libmongreldb_kit` install is needed.

Dense ANN additionally needs:

```bash
pip install --user sentence-transformers
```

## Manual Configuration

Run `hermes memory setup`, select `mongreldb_hermes`, then choose native or daemon mode. For
manual or advanced configuration, edit `/home/user/.hermes/config.yaml`:

```yaml
memory:
  provider: mongreldb_hermes
  mongreldb_hermes:
    mode: native                       # native | daemon
    db_dir: /home/user/.hermes/mongreldb_hermes_data
    encryption: enabled                # enabled by default; disabled = plaintext
    embedding_model: ""                # "" = model-free; "all-MiniLM-L6-v2" = dense ANN
    dim: 384
    enrichment_mode: heuristic         # heuristic | llm

    # Blank/missing passphrase uses ~/.hermes/mongreldb_hermes.key (mode 0600)
    # Prefer MONGRELDB_PASSPHRASE when supplying your own passphrase.

    # Optional logical auth ON TOP OF encryption (storage-layer credentials)
    # username: admin
    # password: "${MONGRELDB_DB_PASSWORD}"

    # daemon-only settings
    daemon_url: http://127.0.0.1:8453
    daemon_data_dir: /home/user/.hermes/mongreldb_hermes_data
    daemon_pidfile: /tmp/mongreldb-hermes.pid
    daemon_log: /tmp/mongreldb-hermes.log
    daemon_binary: /home/user/.hermes/plugins/mongreldb_hermes/vendor/0.60.2/mongreldb-server
```

The bundled daemon is the default. Set `MONGRELDB_DAEMON_BINARY` to use another binary.

Enable dense ANN in config:

```yaml
    embedding_model: "all-MiniLM-L6-v2"
    dim: 384
```

### Encryption key and credentials

Encryption at rest and username/password credentials are **orthogonal**. With encryption enabled, a missing passphrase creates and reuses `~/.hermes/mongreldb_hermes.key` in both modes. Back up this file with the database. Losing it makes the encrypted database unreadable.

| Layer | What it protects | How you set it |
|-------|------------------|----------------|
| **Encryption passphrase / key** | Bytes on disk (pages, WAL, caches) - AES-256-GCM | Generated key file by default, or `MONGRELDB_PASSPHRASE` / `passphrase` |
| **Username + password credentials** | Who may open/use the DB handle (logical access) | Storage-layer credentials; can be combined with encryption |

You can (and usually should) use **both**: losing the passphrase means the on-disk bytes are unreadable; losing the credentials means the process cannot open an auth-enforced database even with the passphrase.

#### Daemon mode (`mongreldb-server`)

Start the server with a passphrase so the data directory is encrypted, and optionally with DB credentials:

```bash
export MONGRELDB_PASSPHRASE='choose-a-long-random-passphrase'
export MONGRELDB_DB_USERNAME='admin'
export MONGRELDB_DB_PASSWORD='choose-a-strong-password'

/home/user/.hermes/plugins/mongreldb_hermes/vendor/0.60.2/mongreldb-server \
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

See the engine docs: [Encryption](https://github.com/visorcraft/MongrelDB/blob/v0.60.2/docs/07-encryption.md) and [Credential enforcement](https://github.com/visorcraft/MongrelDB/blob/v0.60.2/docs/15-credential-enforcement.md).

#### Native mode (`libmongreldb.so`)

Native opens go through the encrypted C ABI by default. The plugin generates or loads the passphrase before opening the database, so a missing secret never silently creates plaintext. Set `encryption: disabled` or `MONGRELDB_ENCRYPTION=disabled` to opt out explicitly. Prefer environment variables (`MONGRELDB_PASSPHRASE`, `MONGRELDB_DB_USERNAME`, `MONGRELDB_DB_PASSWORD`) over committing secrets in `config.yaml`.

### Restart Hermes

The plugin creates the database and table on first use.

## Documentation

- [MongrelDB setup](MongrelDB_setup.md) - install and configure
- [Modes](MongrelDB_modes.md) - native FFI vs HTTP daemon
- [Dense ANN](MongrelDB_dense.md) - enabling `all-MiniLM-L6-v2` and model policy
- [Standalone Rust](MongrelDB_standalone.md) - using MongrelDB directly outside Hermes
- [Engine embeddings & retrieval](https://github.com/visorcraft/MongrelDB/blob/v0.60.2/docs/22-embeddings-and-retrieval.md) - pluggable `EmbeddingSource` / provider registry

## Modes

| Mode | How it works | Best for |
|------|----------------|----------|
| **Native** | Loads `libmongreldb.so` in-process | Lowest latency; single Hermes process owns the DB |
| **Daemon** | Talks to `mongreldb-server` over HTTP | Warm cache; DB shared across processes / restarts |

See [MongrelDB_modes.md](MongrelDB_modes.md). Helper scripts: `start_daemon.sh`, `stop_daemon.sh`.

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
| `after-install.md` | Memory-provider activation instructions shown by Hermes |
| `__init__.py` | Provider implementation and registration |
| `_ffi.py` | ctypes wrapper for `libmongreldb.so` |
| `install_mongreldb.py` | Verified platform binary installer |
| `MongrelDB_setup.md` | Install and configure |
| `MongrelDB_modes.md` | Native vs daemon |
| `MongrelDB_dense.md` | Dense ANN |
| `MongrelDB_standalone.md` | Direct Rust usage notes |
| `start_daemon.sh` / `stop_daemon.sh` | Daemon lifecycle helpers |
| `assets/mongrel.png` | Logo |

## Current State

- **Native FFI** path is the primary, tested mode (model-free and dense ANN with MiniLM).
- **Daemon mode** uses the server's typed `/kit/*` HTTP API for schema creation, writes, hybrid search, filtering, and deletion. The bundled daemon starts automatically when needed.
- Native mode requires MongrelDB 0.60.2. Daemon mode requires `mongreldb-server` 0.60.2 and uses its 0.60.2 Kit HTTP API.

## Development Notes

- Set `MONGRELDB_LIB` only to override the bundled shared library.
- A MongrelDB path is a **data directory**, not a single file.
- Do not invent weak hashed dense vectors to “fake” ANN; prefer sparse retrieval when no real model is available.

## License

MIT OR Apache-2.0
