# MongrelDB setup for Hermes

This guide covers building MongrelDB and installing the `mongreldb-hermes` plugin.

## 1. Build MongrelDB

### FFI shared library (required for native mode)

```bash
git clone https://github.com/visorcraft/MongrelDB.git
cd MongrelDB
git checkout v0.60.2
cd crates/mongreldb-ffi
cargo build --release
```

Expected output:

```
mongreldb/crates/mongreldb-ffi/target/release/libmongreldb.so
mongreldb/crates/mongreldb-ffi/include/mongreldb.h
```

### HTTP daemon (required for daemon mode)

```bash
cd /path/to/mongreldb/crates/mongreldb-server
cargo build --release
```

Expected output:

```
mongreldb/crates/mongreldb-server/target/release/mongreldb-server
```

## 2. Make libmongreldb.so discoverable

### Option A: environment variable

```bash
export MONGRELDB_LIB=/path/to/mongreldb/crates/mongreldb-ffi/target/release/libmongreldb.so
```

### Option B: install system-wide

```bash
sudo cp /path/to/mongreldb/crates/mongreldb-ffi/target/release/libmongreldb.so /usr/local/lib/
sudo ldconfig
```

## 3. Install the plugin

```bash
hermes plugins install visorcraft/MongrelDB-Hermes --no-enable
hermes memory setup mongreldb_hermes
```

Directory layout after install:

```
/home/user/.hermes/plugins/mongreldb_hermes/
├── __init__.py
├── _ffi.py
├── plugin.yaml
├── README.md
├── MongrelDB_setup.md
├── MongrelDB_modes.md
├── MongrelDB_dense.md
├── MongrelDB_standalone.md
├── start_daemon.sh
└── stop_daemon.sh
```

## 4. Configure Hermes

The setup command above writes the configuration. To configure it manually,
edit `/home/user/.hermes/config.yaml`:

```yaml
memory:
  provider: mongreldb_hermes
  mongreldb_hermes:
    mode: native
    db_dir: /home/user/.hermes/mongreldb_hermes_data
    embedding_model: ""
    dim: 384
    enrichment_mode: heuristic
```

## 5. Verify installation

```bash
hermes plugins list --user
hermes memory status
```

## 6. Common issues

### `libmongreldb.so: cannot open shared object file`

Set `MONGRELDB_LIB` explicitly or copy the library to `/usr/local/lib` and run `ldconfig`.

### `database is locked`

MongrelDB allows **one exclusive open of a given data directory** (storage root). A second independent open of the same root fails with `DatabaseLocked`.

That does **not** mean only one client can use MongrelDB:

- **Native mode:** Hermes holds the exclusive open. Many threads inside Hermes can share it; a second Hermes process (or a daemon) pointing at the **same** `db_dir` will fail.
- **Daemon mode:** `mongreldb-server` holds the exclusive open. Many Hermes (or other) clients talk to it over HTTP and share the cache — they must **not** also open the same directory with native FFI.

If you switch native → daemon, stop the native Hermes process (or use a different data dir) before starting the server on that path.

### `table "hermes_memories" not found`

The `db_dir` exists but was created without the memory schema. Delete the directory and restart Hermes to recreate it.

### Slow inserts

If `embedding_model` is set, the embedding model is the bottleneck. Set it to `""` for model-free operation, or choose a smaller model.

## 7. Switching from native to daemon

```bash
# Stop Hermes, then start the daemon manually
/path/to/mongreldb-server \
    /home/user/.hermes/mongreldb_hermes_data \
    8453 \
    --daemon \
    --pidfile /tmp/mongreldb-hermes.pid
```

Then change config:

```yaml
memory:
  provider: mongreldb_hermes
  mongreldb_hermes:
    mode: daemon
    daemon_url: http://127.0.0.1:8453
    daemon_data_dir: /home/user/.hermes/mongreldb_hermes_data
    daemon_pidfile: /tmp/mongreldb-hermes.pid
    daemon_binary: /path/to/mongreldb-server
```

Restart Hermes.

## 8. Rebuilding after a MongrelDB upgrade

MongrelDB's C ABI is still evolving. This plugin targets MongrelDB 0.60.2. Update `_ffi.py` against the new `mongreldb.h` before changing that requirement.
