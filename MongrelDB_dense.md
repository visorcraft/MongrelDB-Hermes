# Dense ANN with MongrelDB

By default, `mongreldb-hermes` uses dense ANN with `all-MiniLM-L6-v2`. Choose sparse mode during setup when lowest latency matters more than semantic recall.

## What dense ANN adds

A query like:

```
"What was that thing I was worried might break after an update?"
```

can retrieve a memory:

```
"MongrelDB rejects multi-session WAL histories after an unclean shutdown."
```

even though the two phrases share no exact words. Dense embeddings capture meaning rather than token overlap.

This is the main capability that vector-only databases like ChromaDB advertise, but MongrelDB combines it with sparse, exact-text, bitmap, and range signals in the same query.

## Dense setup

Dense mode is the setup default. Hermes installs `sentence-transformers`, and the plugin downloads `all-MiniLM-L6-v2` automatically. The provider supplies vectors directly to MongrelDB; the engine itself does not depend on any specific embedding vendor.

```yaml
memory:
  provider: mongreldb_hermes
  mongreldb_hermes:
    mode: native
    db_dir: /home/user/.hermes/mongreldb_hermes_data
    retrieval_mode: dense
    embedding_model: "all-MiniLM-L6-v2"
    dim: 384
```

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

With `all-MiniLM-L6-v2` on a modern CPU, expect roughly 20-30 ms per insert or search. The database itself is under 1 ms; the embedding model dominates the latency.

Measured on a 50-entry synthetic dataset, 5 topics, top_k=5, native FFI, warm model cache:

| Mode | Insert (ms) | Search (ms) | P@5 | R@5 |
|------|------------:|------------:|----:|----:|
| Model-free | 0.94 | 0.63 | 1.00 | 0.50 |
| Dense ANN (`all-MiniLM-L6-v2`) | 25.88 | 19.3 | 1.00 | 0.50 |

On a lexical benchmark the two modes score identically, because the queries are exact topic strings. The value of dense ANN appears on vague or paraphrased queries, where sparse-only retrieval would miss the connection.

If you want semantic recall but lower latency, the best paths are:
1. Use a smaller/faster embedding model.
2. Batch embeddings in the background.
3. Run a local embedding model server.

## Switching from sparse to dense

Set `retrieval_mode: dense` and `embedding_model: "all-MiniLM-L6-v2"`, then restart Hermes. The existing schema already has a nullable embedding column and ANN index. New memories receive embeddings; existing memories are not backfilled automatically.

## Dense ANN (native and daemon)

When an embedding model is configured, **new tables** create the `embedding_ann` index with **Dense** quantization (full-precision f32 cosine ANN) on the default **HNSW** algorithm: `m=16`, `ef_construction=64`, `ef_search=64`. Native mode uses `mongreldb_schema_add_index_v2` / `MDB_ANN_QUANTIZATION_DENSE`; daemon mode passes the same options through `/kit/create_table`.

MongrelDB 0.63+ also offers DiskANN, IVF, and product quantization via `mongreldb_schema_add_index_v3` / Kit options; this plugin keeps Dense HNSW as the memory default.

The embedding column is tagged `supplied_by_application`: Hermes computes vectors with `sentence-transformers` and writes them on insert. MongrelDB also supports server-configured sources (`configured_model` with `provider_id` / `model_id` / `model_version`); that path is not the default for this plugin.

Sparse setup (no embedding model) leaves ANN at the engine default (BinarySign HNSW) and does not set an embedding source.

```yaml
memory:
  provider: mongreldb_hermes
  mongreldb_hermes:
    mode: daemon
    daemon_url: http://127.0.0.1:8453
    retrieval_mode: dense
    embedding_model: "all-MiniLM-L6-v2"
    dim: 384
```

## When sparse mode fits

- Most memories are well-tagged or contain exact technical terms.
- Users ask with specific keywords.
- Latency is critical.
- You can add a lightweight dense model later when needed.

## When dense ANN fits

- Users ask vague, conversational questions.
- Memories are long and varied in wording.
- You can tolerate 20-30 ms per operation.
- You want to retrieve memories the user did not explicitly tag.
