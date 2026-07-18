# Security

This document describes the security properties of MongrelDB-backed memory for
Hermes Agent and how to report vulnerabilities.

## Overview

This repository is a Hermes Agent **memory provider plugin** backed by
MongrelDB. It stores and retrieves agent memories using either:

- **Native mode** — in-process `libmongreldb.so` (C FFI), or
- **Daemon mode** — HTTP client to `mongreldb-server`.

Encryption at rest is enabled by default. When no passphrase is supplied, the
plugin generates one in `~/.hermes/mongreldb_hermes.key` with mode `0600` and
uses it for native and daemon opens. Data at rest lives in the MongrelDB data
directory. Dense embeddings are enabled by default and produced locally with
`sentence-transformers`; that inference runs in the Hermes process.

## Plugin security properties

- **Filesystem.** Native mode opens a MongrelDB data directory under a
  configured path (default under Hermes home). Secure that directory with
  host permissions appropriate for memory content.
- **Encryption key.** Back up `mongreldb_hermes.key` with the database. Losing
  the key makes encrypted data unreadable. Set `MONGRELDB_PASSPHRASE` to
  supply your own secret. Plaintext requires the explicit
  `encryption: disabled` or `MONGRELDB_ENCRYPTION=disabled` opt-out.
- **Network.** Daemon mode talks to `mongreldb-server` over plain HTTP. The
  daemon binds to `127.0.0.1` by default — traffic stays on the loopback
  interface. For remote or multi-tenant deployments, terminate TLS in a
  reverse proxy (nginx, Caddy) in front of the daemon and enable daemon
  authentication.
- **Auth to the daemon.** When the server requires Bearer token or HTTP
  Basic auth, configure the plugin/client accordingly. Credentials must not
  be logged or committed to this repository.
- **Embeddings.** Enabling `embedding_model` loads a local model into the
  Hermes process. Treat model weights and any remote model download as
  supply-chain surface; pin versions where practical.
- **LLM enrichment.** Optional `enrichment_mode: llm` sends memory text to the
  configured OpenAI-compatible API. It requires an API key. Do not enable it
  with untrusted content without understanding that egress path.

## Engine / daemon security (MongrelDB)

The plugin is a client of MongrelDB. Typical `mongreldb-server` posture:

- Binds to `127.0.0.1` only by default — not accessible from other machines.
- **No authentication by default** — any local process can query, write, or
  delete data. Enable daemon auth on any shared host.
- No TLS on the raw daemon port — use a reverse proxy for remote access.

Do not expose the daemon directly to an untrusted network.

## Input and memory content

- Memories may contain user or agent secrets. Treat the data directory and
  backups as sensitive.
- Query and write paths use the MongrelDB Kit/FFI APIs with typed cells and
  structured search requests — not string-concatenated SQL from this plugin’s
  hybrid search path. Prefer the documented APIs over ad-hoc SQL when
  integrating.

## Dependency security

Direct runtime dependencies may include Hermes, MongrelDB (`libmongreldb` /
`mongreldb-server`), `sentence-transformers`, and optional Python packages
such as `openai`. Report dependency issues via Dependabot or the
private vulnerability flow below. Engine vulnerabilities should also be
reported against [visorcraft/MongrelDB](https://github.com/visorcraft/MongrelDB)
when they are not specific to this plugin.

## Reporting a vulnerability

**Do not file a public GitHub issue, discussion, or pull request for
security problems.** Report privately through **GitHub’s private
vulnerability reporting** on this repository:

1. Open https://github.com/visorcraft/MongrelDB-Hermes/security/advisories/new  
2. Describe the issue, impact, and a minimal reproduction if possible.  
3. Allow reasonable time for assessment and a fix before public disclosure.

If private reporting is unavailable, contact the maintainers via the
organization listed on https://github.com/visorcraft with a subject line
that makes the security nature clear.

## Preferred languages

English is preferred for security reports.

## License

This project is dual-licensed under **MIT OR Apache-2.0**. See `LICENSE-MIT`
and `LICENSE-APACHE`.
