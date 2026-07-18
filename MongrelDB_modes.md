# MongrelDB modes for Hermes

The `mongreldb-hermes` plugin supports two execution modes. Both give the same hybrid retrieval features; the difference is where the database process lives.

## Native mode (default)

In native mode, the Hermes process loads `libmongreldb.so` directly and calls the C ABI. The database lives in the directory configured as `db_dir`.

### Why native mode fits Hermes

For a typical single-user Hermes deployment, native mode is the simplest and fastest choice. There is no extra process to manage, no HTTP overhead, and the database is created automatically on first use. The FFI path is what produced the benchmark numbers in the README: **0.94 ms / 0.63 ms** model-free and **25.88 ms / 30.64 ms** with dense ANN.

### Configuration

```yaml
memory:
  provider: mongreldb_hermes
  mongreldb_hermes:
    mode: native
    db_dir: /home/user/.hermes/mongreldb_hermes_data
    embedding_model: ""
    dim: 384
```

### What happens on startup

1. The provider checks whether `/home/user/.hermes/mongreldb_hermes_data/CATALOG` exists.
2. If it does not, it creates the database.
3. It creates the `hermes_memories` table with all the AI-native indexes: sparse, bitmap, PGM range, FM, MinHash, and HNSW ANN.
4. It opens the table for reads and writes.

### Important limitation

**Storage rule (still true in 0.60.x):** one process owns the exclusive open of a given data directory. Multiple threads may share that open **in-process**. A second independent open of the same root (another Hermes native process, or native + daemon on the same path) fails with `DatabaseLocked`.

**Multi-process access** is the job of **daemon mode**: only `mongreldb-server` opens the root; any number of HTTP clients (Hermes included) share it.

## Daemon mode

In daemon mode, the provider connects to a running `mongreldb-server` process over HTTP. The daemon owns the database lock and can keep the page cache, result cache, and memtable warm between Hermes restarts.

### Why daemon mode fits Hermes

Use daemon mode when:

- You want Hermes restarts to be fast because the database is already warm.
- Multiple Hermes processes or other tools need to share the same memory.
- You want the memory database to outlive the Hermes session.
- You plan to use the new pluggable embedding layer on the server side (bundled local models or remote providers registered with the daemon).

The trade-off is a small amount of HTTP overhead per request and the need to manage the daemon process.

### Configuration

```yaml
memory:
  provider: mongreldb_hermes
  mongreldb_hermes:
    mode: daemon
    daemon_url: http://127.0.0.1:8453
    daemon_data_dir: /home/user/.hermes/mongreldb_hermes_data
    daemon_pidfile: /tmp/mongreldb-hermes.pid
    daemon_log: /tmp/mongreldb-hermes.log
    daemon_binary: /path/to/mongreldb-server
    embedding_model: ""
    dim: 384
```

### Starting the daemon manually

```bash
/path/to/mongreldb-server \
    /home/user/.hermes/mongreldb_hermes_data \
    8453 \
    --daemon \
    --pidfile /tmp/mongreldb-hermes.pid
```

Or use the helper script included in this plugin:

```bash
/path/to/mongreldb-hermes/start_daemon.sh
```

### Starting the daemon from the plugin

If `mode: daemon` is set and the daemon is not reachable at `daemon_url`, the provider will try to launch it using `daemon_binary` with the configured `daemon_data_dir`, `daemon_pidfile`, and `daemon_log`. The daemon will log to the configured log file and run in the background.

> **Note:** The daemon client code exists in the provider but is not fully tested end-to-end yet. For production use, start the daemon manually with the helper script.

### Important notes

- The daemon must be built before the plugin can start it. See `MongrelDB_setup.md`.
- The daemon and native modes cannot share the same `db_dir` at the same time.
- The daemon keeps the database open, so its shutdown is managed by the daemon, not by Hermes.

## Pluggable embedding providers on the daemon

MongrelDB 0.60.0 introduced a pluggable embedding layer. The daemon can register local or remote embedding providers, so the server itself can compute vectors rather than requiring the client to supply them. This is useful if you want to centralize model management in the daemon.

For the `mongreldb-hermes` provider, the default behavior is still client-supplied vectors: the provider computes embeddings locally with `sentence-transformers` and sends them to the daemon. If you want to use a daemon-registered provider instead, you would configure the `hermes_memories` table with an `EmbeddingSource::GeneratedColumn` source and leave the embedding column empty on insert. That path is not yet wired in the plugin.

## Switching modes

To switch from native to daemon:

1. Stop Hermes.
2. Start the daemon (or use `start_daemon.sh`).
3. Change `mode: native` to `mode: daemon` in `config.yaml`.
4. Restart Hermes.

To switch from daemon to native:

1. Stop Hermes.
2. Stop the daemon (`stop_daemon.sh`).
3. Change `mode: daemon` to `mode: native`.
4. Restart Hermes.

You can reuse the same `db_dir` as long as only one mode holds the lock at a time.

## Helper scripts

### `start_daemon.sh`

Starts the daemon if it is not already running, then waits for `/health` to respond.

### `stop_daemon.sh`

Stops the daemon by reading the pidfile and sending `SIGTERM`.
