# MongrelDB memory provider installed

1. At `Enable 'mongreldb_hermes' now? [y/N]:`, press `Enter` or answer `n`.

2. Ignore the generic `hermes plugins enable mongreldb_hermes` and `hermes gateway restart` instructions printed afterward.

3. After the installer returns to the shell, run:

```bash
hermes memory setup
```

Select `mongreldb_hermes`, listed as `local`. No API key is required. Memory setup configures and activates it.
