<p align="center">
  <img src="assets/mongrel.png" alt="MongrelDB logo" width="250" />
</p>

<h1 align="center">MongrelDB Hermes</h1>

<p align="center">
  <b>Hermes Agent memory provider plugin for MongrelDB - hybrid long-term memory with dense ANN, sparse retrieval, exact text, bitmaps, learned ranges, and MinHash dedup in one engine.</b>
</p>

<p align="center">
  <a href="https://github.com/visorcraft/MongrelDB"><img src="https://img.shields.io/badge/engine-MongrelDB-blue.svg" alt="MongrelDB" /></a>
  <a href="https://github.com/visorcraft/MongrelDB-Hermes/actions"><img src="https://img.shields.io/badge/CI-pending-lightgrey.svg" alt="CI" /></a>
  <a href="#license"><img src="https://img.shields.io/badge/license-MIT%20OR%20Apache--2.0-blue.svg" alt="License" /></a>
</p>

## Package

| Surface | Location | Install |
|---|---|---|
| Hermes memory provider | `mongreldb_hermes/` | Copy into `~/.hermes/hermes-agent/plugins/memory/` (see [Quick start](#quick-start)) |

## Why MongrelDB for Hermes?

Hermes memory needs more than a plain vector store. Useful long-term memory for an agent must:

- Recall facts the user explicitly mentions, including exact wording.
- Catch vague or paraphrased references to older conversations.
- Filter by memory type, project, entity, state, and time.
- Detect near-duplicate memories so the store does not grow forever.
- Return results fast enough that the agent still feels responsive.

MongrelDB is built around multiple AI-native indexes in one engine, so a single query can combine all of these signals. Most alternatives only provide dense vector search; frameworks like Mem0 compose several separate databases to approximate the same thing.

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

Two execution modes (configure `mode: native` or `mode: daemon`):

- **Native Rust FFI** (default): Hermes loads `libmongreldb.so` and opens the data directory **in-process** - lowest latency; that process owns the exclusive storage open for `db_dir`.
- **HTTP daemon**: Hermes is an HTTP client of `mongreldb-server`. The **daemon** owns the exclusive open; many processes can share the warm cache over HTTP. Do not also open the same data directory with native FFI while the daemon is running.

Optional **dense ANN** when `embedding_model` is set (for example `all-MiniLM-L6-v2`, 384-d). Leave it empty for model-free hybrid sparse + lexical retrieval. MongrelDB core keeps embedding generation as a pluggable layer: applications may supply vectors, Kit/server may register providers, and ANN indexes operate only on stored vectors plus model metadata.

**Locking (0.60.x):** only one process may exclusively open a given MongrelDB data directory. In-process multi-handle/thread sharing is fine. Multi-process sharing goes through the daemon, not multiple native opens of the same path.

## Requirements

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) (or compatible Hermes install) with a `plugins/memory/` directory
- [MongrelDB](https://github.com/visorcraft/MongrelDB) built as:
  - `libmongreldb.so` for native mode, and/or
  - `mongreldb-server` for daemon mode
- Python 3.10+ for the plugin runtime
- Optional: `sentence-transformers` when dense ANN is enabled

## Quick Start

### 1. Build MongrelDB

```bash
git clone https://github.com/visorcraft/MongrelDB.git
cd MongrelDB

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

Hermes loads memory providers from `plugins/memory/<name>/` (directory must
contain `__init__.py`). Install the package tree as that directory:

```bash
git clone https://github.com/visorcraft/MongrelDB-Hermes.git
mkdir -p /home/user/.hermes/hermes-agent/plugins/memory
# Copy the provider package (not the whole repo root) so __init__.py sits at the plugin root:
cp -a MongrelDB-Hermes/mongreldb_hermes /home/user/.hermes/hermes-agent/plugins/memory/mongreldb_hermes
```

Optional dense ANN dependency:

```bash
pip install --user sentence-transformers
```

### 4. Configure Hermes

Edit `/home/user/.hermes/config.yaml`:

```yaml
memory:
  provider: mongreldb_hermes
  mongreldb_hermes:
    mode: native                       # native | daemon
    db_dir: /home/user/.hermes/mongreldb_hermes_data
    embedding_model: ""                # "" = model-free; "all-MiniLM-L6-v2" = dense ANN
    dim: 384
    enrichment_mode: heuristic         # heuristic | llm

    # daemon-only settings
    daemon_url: http://127.0.0.1:8453
    daemon_data_dir: /home/user/.hermes/mongreldb_hermes_data
    daemon_pidfile: /tmp/mongreldb-hermes.pid
    daemon_log: /tmp/mongreldb-hermes.log
    daemon_binary: /path/to/mongreldb-server
```

Enable dense ANN in config:

```yaml
    embedding_model: "all-MiniLM-L6-v2"
    dim: 384
```

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
| `plugin.yaml` | Hermes provider manifest (package entrypoint) |
| `mongreldb_hermes/__init__.py` | Provider implementation |
| `mongreldb_hermes/_ffi.py` | ctypes wrapper for `libmongreldb.so` |
| `mongreldb_hermes/plugin.yaml` | Manifest next to the package (for in-tree installs) |
| `MongrelDB_setup.md` | Build and install |
| `MongrelDB_modes.md` | Native vs daemon |
| `MongrelDB_dense.md` | Dense ANN |
| `MongrelDB_standalone.md` | Direct Rust usage notes |
| `start_daemon.sh` / `stop_daemon.sh` | Daemon lifecycle helpers |
| `assets/mongrel.png` | Logo |

## Current State

- **Native FFI** path is the primary, tested mode (model-free and dense ANN with MiniLM).
- **Daemon mode** is the supported way to share one data directory across processes (or keep a warm cache across Hermes restarts). Use a current `mongreldb-server` build; start it with `start_daemon.sh` or configure `daemon_*` keys and `mode: daemon`.
- Align the linked `libmongreldb.so` / `mongreldb-server` with a MongrelDB release that ships the embedding provider registry (see engine docs).

## Development Notes

- Set `MONGRELDB_LIB` to a release build of `libmongreldb.so` from a MongrelDB checkout.
- A MongrelDB path is a **data directory**, not a single file.
- Switching from model-free to dense may require a fresh `db_dir` if the table was created without an embedding column / ANN index (see [MongrelDB_dense.md](MongrelDB_dense.md)).
- Do not invent weak hashed dense vectors to “fake” ANN; prefer sparse retrieval when no real model is available.

## License

MIT OR Apache-2.0
