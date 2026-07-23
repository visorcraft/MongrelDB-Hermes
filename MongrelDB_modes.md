# MongrelDB modes for Hermes

The `mongreldb-hermes` plugin supports two execution modes. Both give the same hybrid retrieval features; the difference is where the database process lives.

## Native mode (default)

In native mode, the Hermes process loads `libmongreldb.so` directly and calls the C ABI. The database lives in the directory configured as `db_dir`.

### Why native mode fits Hermes

For a typical single-user Hermes deployment, native mode is the simplest and fastest choice. There is no extra process to manage, no HTTP overhead, and the database is created automatically on first use. The FFI path produced these benchmark numbers on a 50-entry synthetic dataset: **0.94 ms / 0.63 ms** model-free and **25.88 ms / 19.3 ms** with dense ANN (P@5 1.00, R@5 0.50 on exact topic queries).

### Configuration

```yaml
memory:
  provider: mongreldb_hermes
  mongreldb_hermes:
    mode: native
    db_dir: /home/user/.hermes/mongreldb_hermes_data
    encryption: enabled
    retrieval_mode: dense
    embedding_model: "all-MiniLM-L6-v2"
    dim: 384
```

### What happens on startup

1. The provider checks whether `/home/user/.hermes/mongreldb_hermes_data/CATALOG` exists.
2. If it does not, it creates the database.
3. It creates the `hermes_memories` table with all the AI-native indexes: sparse, bitmap, PGM range, FM, MinHash, and HNSW ANN.
4. It opens the table for reads and writes.

### Important limitation

**Storage rule (still true in 0.64.x):** one process owns the exclusive open of a given data directory. Multiple threads may share that open **in-process**. A second independent open of the same root (another Hermes native process, or native + daemon on the same path) fails with `DatabaseLocked`.

**Multi-process access** is the job of **daemon mode**: only `mongreldb-server` opens the root; any number of HTTP clients (Hermes included) share it.

## Daemon mode

In daemon mode, the provider connects to a running `mongreldb-server` process over HTTP. The daemon owns the database lock and can keep the page cache, result cache, and memtable warm between Hermes restarts.

Run `hermes memory setup`, select `mongreldb_hermes`, and choose `daemon`, or
set the configuration below manually.

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
    encryption: enabled
    daemon_url: http://127.0.0.1:8453
    daemon_data_dir: /home/user/.hermes/mongreldb_hermes_data
    daemon_pidfile: /tmp/mongreldb-hermes.pid
    daemon_log: /tmp/mongreldb-hermes.log
    daemon_binary: /home/user/.hermes/plugins/mongreldb_hermes/vendor/0.64.4/mongreldb-server
    retrieval_mode: dense
    embedding_model: "all-MiniLM-L6-v2"
    dim: 384
```

### Starting the daemon manually

```bash
export MONGRELDB_PASSPHRASE="$(cat ~/.hermes/mongreldb_hermes.key)"
/home/user/.hermes/plugins/mongreldb_hermes/vendor/0.64.4/mongreldb-server \
    /home/user/.hermes/mongreldb_hermes_data \
    --port 8453 \
    --passphrase "$MONGRELDB_PASSPHRASE" \
    --daemon \
    --pidfile /tmp/mongreldb-hermes.pid
```

Or use the helper script included in this plugin:

```bash
~/.hermes/plugins/mongreldb_hermes/start_daemon.sh
```

### Starting the daemon from the plugin

If `mode: daemon` is set and the daemon is not reachable at `daemon_url`, the provider launches the bundled `mongreldb-server` using the configured `daemon_data_dir`, `daemon_pidfile`, and `daemon_log`. Set `MONGRELDB_DAEMON_BINARY` to override it.

The provider uses the typed `/kit/create_table`, `/kit/txn`, `/kit/query`, and `/kit/search` endpoints. Set `MONGRELDB_DAEMON_AUTH_TOKEN` when the server uses `--auth-token`.

### Important notes

- The installer downloads the daemon before saving memory configuration, or on first provider start if setup was skipped.
- Encryption is enabled by default for new data directories. The plugin creates `~/.hermes/mongreldb_hermes.key` with mode `0600` when no passphrase is supplied. Back it up with the database.
- Existing directories open by on-disk layout (`_meta/keys` = encrypted). A passphrase is required only for encrypted roots; plaintext roots open without encryption even if a passphrase is configured.
- Set `encryption: disabled` or `MONGRELDB_ENCRYPTION=disabled` only to opt into plaintext storage for new creates.
- The daemon and native modes cannot share the same `db_dir` at the same time.
- The daemon keeps the database open, so its shutdown is managed by the daemon, not by Hermes.

## Pluggable embedding providers on the daemon

MongrelDB includes a pluggable embedding layer. The daemon can register local or remote embedding providers, so the server itself can compute vectors rather than requiring the client to supply them. This is useful if you want to centralize model management in the daemon.

For the `mongreldb-hermes` provider, the default is still client-supplied vectors (`supplied_by_application`): the provider computes embeddings locally with `sentence-transformers` and writes them on insert (native FFI and daemon). A daemon-registered `configured_model` / generated-column source is supported by the engine but is not the plugin default.

## Switching modes

To switch from native to daemon:

1. Stop Hermes.
2. Change `mode: native` to `mode: daemon` in `config.yaml`.
3. Restart Hermes. The plugin starts the bundled daemon if needed.

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
