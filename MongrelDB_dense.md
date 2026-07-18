# Enabling dense ANN with MongrelDB

By default, `mongreldb-hermes` runs in model-free mode. This gives the lowest latency because it skips the neural embedding model. If you need semantic retrieval for vague or paraphrased queries, enable dense ANN.

## What dense ANN adds

A query like:

```
"What was that thing I was worried might break after an update?"
```

can retrieve a memory:

```
"MongrelDB 0.58.4 rejects multi-session WAL histories after an unclean shutdown."
```

even though the two phrases share no exact words. Dense embeddings capture meaning rather than token overlap.

This is the main capability that vector-only databases like ChromaDB advertise, but MongrelDB combines it with sparse, exact-text, bitmap, and range signals in the same query.

## Choose an embedding model

Set `embedding_model` in `config.yaml`. Any sentence-transformers model name works. The provider supplies vectors directly to MongrelDB; the engine itself does not depend on any specific embedding vendor.

```yaml
memory:
  provider: mongreldb_hermes
  mongreldb_hermes:
    mode: native
    db_dir: /home/user/.hermes/mongreldb_hermes_data
    embedding_model: "all-MiniLM-L6-v2"
    dim: 384
```

Other options:

| Model | Dimensions | Notes |
|-------|------------|-------|
| `all-MiniLM-L6-v2` | 384 | Good balance of quality and speed |
| `bge-small-en` | 384 | Similar speed, different training data |
| `gtr-t5-base` | 768 | Higher quality, slower |
| `bge-micro-v2` | 384 | Smaller, faster, lower quality |

For a faster local model, consider quantizing with ONNX or using a smaller model.

## Install the embedding dependency

```bash
pip install --user sentence-transformers
```

The first time a model is used, it downloads automatically from HuggingFace.

## How the provider uses dense ANN

When `embedding_model` is non-empty:

1. Each memory is embedded using the model at insert time.
2. The embedding is stored in the `embedding` column.
3. The ANN index `embedding_ann` is built automatically.
4. At search time, the query is embedded and passed to a `SearchRequest` with:
   - `Ann` retriever on the `embedding` column
   - `SparseMatch` retriever on the `sparse` column
   - Reciprocal-rank fusion of the two signals
   - Optional `ExactVector` rerank

If you also add `memory_type` or `state` filters, they are applied as hard `must` conditions before ranking.

## Performance expectations

With `all-MiniLM-L6-v2` on a modern CPU, expect roughly 20–30 ms per insert or search. The database itself is under 1 ms; the embedding model dominates the latency.

Measured on a 50-entry synthetic dataset, 5 topics, top_k=5, native FFI, warm model cache:

|| Mode | Insert (ms) | Search (ms) | P@5 | R@5 |
||------|------------:|------------:|----:|----:|
|| Model-free | 0.94 | 0.63 | 1.00 | 0.50 |
|| Dense ANN (`all-MiniLM-L6-v2`) | 25.88 | 19.3 | 1.00 | 0.50 |

On a lexical benchmark the two modes score identically, because the queries are exact topic strings. The value of dense ANN appears on vague or paraphrased queries, where sparse-only retrieval would miss the connection.

If you want semantic recall but lower latency, the best paths are:
1. Use a smaller/faster embedding model.
2. Batch embeddings in the background.
3. Run a local embedding model server.

## Switching from model-free to dense

Currently, the schema is created on first database creation. To switch from model-free to dense, delete or rename the existing `db_dir` and let the plugin recreate it with the embedding column and ANN index.

```bash
mv /home/user/.hermes/mongreldb_hermes_data \
   /home/user/.hermes/mongreldb_hermes_data_model_free_backup
```

Then set `embedding_model: "all-MiniLM-L6-v2"` and restart Hermes.

## Dense ANN with the daemon

Dense ANN works in daemon mode too. The provider sends the computed embedding vector to the daemon, so the same model and `dim` settings apply. The daemon itself can also register local or remote embedding providers in MongrelDB's pluggable embedding layer, but the current provider always computes embeddings client-side before sending them.

```yaml
memory:
  provider: mongreldb_hermes
  mongreldb_hermes:
    mode: daemon
    daemon_url: http://127.0.0.1:8453
    embedding_model: "all-MiniLM-L6-v2"
    dim: 384
```

## When to stay model-free

- Most memories are well-tagged or contain exact technical terms.
- Users ask with specific keywords.
- Latency is critical.
- You can add a lightweight dense model later when needed.

## When to enable dense ANN

- Users ask vague, conversational questions.
- Memories are long and varied in wording.
- You can tolerate 20–30 ms per operation.
- You want to retrieve memories the user did not explicitly tag.
