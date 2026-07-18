# Contributing to MongrelDB plugin for Hermes

Thanks for taking the time to help MongrelDB-backed memory for Hermes Agent.
This document describes how to propose a change, what we expect from a pull
request, and the coding standards that apply to the codebase.

If anything here is unclear or out of date, open an issue or a PR.

## Code of conduct

Be kind, be specific, assume good faith. Disagree about the technical
details, not the person. Public reviews stay focused on the diff.

## How to propose a change

MongrelDB Hermes uses a standard **fork → branch → pull request** workflow on
GitHub.

1. **Fork** [`visorcraft/MongrelDB-Hermes`](https://github.com/visorcraft/MongrelDB-Hermes)
   to your GitHub account.
2. **Clone** your fork and add the upstream remote:

   ```sh
   git clone git@github.com:<you>/MongrelDB-Hermes.git
   cd MongrelDB-Hermes
   git remote add upstream https://github.com/visorcraft/MongrelDB-Hermes.git
   ```

3. **Branch** from `master`. Pick a descriptive, kebab-case branch name:
   `fix-ffi-search`, `feature/daemon-retry`, `docs/dense-ann`.

   ```sh
   git fetch upstream
   git switch -c my-change upstream/master
   ```

4. **Make focused commits.** One logical change per commit. Run the
   preflight (see below) before pushing.
5. **Open a pull request** against `master` on `visorcraft/MongrelDB-Hermes`.
   Include:
   - **What.** One paragraph summary of the change.
   - **Why.** Bug fix? New feature? Doc fix? Link the issue if one exists.
   - **How to test.** The exact commands a reviewer should run.
   - **Risk.** What might break? What did you not test?

## Before you push: preflight

This repo is a Hermes memory plugin (Python) that loads MongrelDB via
`libmongreldb.so` (native) and/or talks to `mongreldb-server` (daemon).

### Plugin package

```sh
# Syntax check
python3 -m py_compile mongreldb_hermes/__init__.py mongreldb_hermes/_ffi.py

# Optional: unit/smoke against a local MongrelDB build
export MONGRELDB_LIB=/path/to/MongrelDB/crates/mongreldb-ffi/target/release/libmongreldb.so
# Install the package under a Hermes plugins/memory/mongreldb_hermes tree and
# exercise mongreldb_remember / mongreldb_search with embedding_model empty
# and with all-MiniLM-L6-v2.
```

### Docs and manifests

- Keep generic paths in docs (`/home/user/.hermes`, `/path/to/...`) — do not
  commit machine-specific home directories.
- Keep `plugin.yaml` and `mongreldb_hermes/plugin.yaml` aligned (name, version,
  hooks, pip_dependencies).
- README layout follows the MongrelDB Kit style (centered mascot, tables,
  dual license).

## Scope

- Prefer small, reviewable PRs over large refactors.
- Engine behavior belongs in [visorcraft/MongrelDB](https://github.com/visorcraft/MongrelDB);
  this repo is the Hermes integration layer.
- Do not invent weak hashed dense vectors to claim ANN. Prefer sparse when no
  real embedding model is available (see engine embeddings docs).

## License

By contributing, you agree that your contributions are dual-licensed under
**MIT OR Apache-2.0**, the same terms as this project (see `LICENSE-MIT` and
`LICENSE-APACHE`).
