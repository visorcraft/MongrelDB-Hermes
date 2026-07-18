# MongrelDB setup for Hermes

This guide covers installing the `mongreldb-hermes` plugin and its MongrelDB binaries.

## 1. Install the plugin

```bash
hermes plugins install visorcraft/MongrelDB-Hermes --no-enable
hermes memory setup
```

Select `mongreldb_hermes` in memory setup. The plugin is an exclusive memory provider. Ignore Hermes' generic `hermes plugins enable mongreldb_hermes` message; memory setup selects, configures, and activates it.

Saving memory setup downloads both MongrelDB 0.60.2 runtime files for the current platform. If setup is skipped, first provider startup performs the same install. Downloads are SHA-256 verified and deleted after extraction. Only these files remain:

```
vendor/0.60.2/libmongreldb.so
vendor/0.60.2/mongreldb-server
```

macOS uses `libmongreldb.dylib` instead. Both files are installed even when native mode is selected, so changing modes requires no later download.

Directory layout after install:

```
/home/user/.hermes/plugins/mongreldb_hermes/
├── __init__.py
├── _ffi.py
├── after-install.md
├── install_mongreldb.py
├── plugin.yaml
├── vendor/0.60.2/
├── README.md
├── MongrelDB_setup.md
├── MongrelDB_modes.md
├── MongrelDB_dense.md
├── MongrelDB_standalone.md
├── start_daemon.sh
└── stop_daemon.sh
```

## 2. Configure Hermes

The interactive setup command above writes the configuration. To configure it manually,
edit `/home/user/.hermes/config.yaml`:

```yaml
memory:
  provider: mongreldb_hermes
  mongreldb_hermes:
    mode: native
    db_dir: /home/user/.hermes/mongreldb_hermes_data
    encryption: enabled
    embedding_model: ""
    dim: 384
    enrichment_mode: heuristic
```

Encryption is enabled by default. If no passphrase is configured, the plugin creates `~/.hermes/mongreldb_hermes.key` with mode `0600`. Back up that key with the database. Set `encryption: disabled` only to opt into plaintext storage.

## 3. Verify installation

```bash
hermes memory status
```

## 4. Common issues

### `libmongreldb.so: cannot open shared object file`

Run `python ~/.hermes/plugins/mongreldb_hermes/install_mongreldb.py`. Set `MONGRELDB_LIB` only when overriding the bundled library.

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

## 5. Switching from native to daemon

```bash
# Stop Hermes, then start the daemon manually
export MONGRELDB_PASSPHRASE="$(cat ~/.hermes/mongreldb_hermes.key)"
/home/user/.hermes/plugins/mongreldb_hermes/vendor/0.60.2/mongreldb-server \
    /home/user/.hermes/mongreldb_hermes_data \
    8453 \
    --passphrase "$MONGRELDB_PASSPHRASE" \
    --daemon \
    --pidfile /tmp/mongreldb-hermes.pid
```

Then change config:

```yaml
memory:
  provider: mongreldb_hermes
  mongreldb_hermes:
    mode: daemon
    encryption: enabled
    daemon_url: http://127.0.0.1:8453
    daemon_data_dir: /home/user/.hermes/mongreldb_hermes_data
    daemon_pidfile: /tmp/mongreldb-hermes.pid
    daemon_binary: /home/user/.hermes/plugins/mongreldb_hermes/vendor/0.60.2/mongreldb-server
```

Restart Hermes.

## 6. Rebuilding after a MongrelDB upgrade

MongrelDB's C ABI is still evolving. This plugin targets MongrelDB 0.60.2. Update `_ffi.py` against the new `mongreldb.h` before changing that requirement.
