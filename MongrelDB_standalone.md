# Using MongrelDB standalone

This guide shows how to use MongrelDB directly in Rust, without Hermes. This is useful for understanding the engine or building your own applications.

## 1. Add the dependency

```toml
[dependencies]
mongreldb-core = { path = "/path/to/mongreldb/crates/mongreldb-core" }
```

## 2. Open or create a database

```rust
use mongreldb_core::database::{Database, DatabaseConfig};

fn main() {
    let db = Database::create_or_open(
        "/path/to/my_mongreldb_data",
        DatabaseConfig::default(),
    )
    .expect("open database");
}
```

## 3. Define a schema

```rust
use mongreldb_core::schema::{Column, ColumnFlags, Index, IndexKind, Schema, ValueType};

let schema = Schema::builder()
    .column(Column::new("id", ValueType::Int64).with_flag(ColumnFlags::PRIMARY_KEY))
    .column(Column::new("content", ValueType::Bytes))
    .column(Column::new("tags", ValueType::Bytes))
    .column(Column::new("embedding", ValueType::Embedding { dim: 384 }).nullable())
    .column(Column::new("sparse", ValueType::Bytes))
    .column(Column::new("created_at", ValueType::Int64))
    .index("content_fm", "content", IndexKind::Fm)
    .index("tags_mh", "tags", IndexKind::MinHash)
    .index("embedding_ann", "embedding", IndexKind::Ann)
    .index("sparse_idx", "sparse", IndexKind::Sparse)
    .index("created_at_range", "created_at", IndexKind::LearnedRange)
    .build();
```

## 4. Create a table

```rust
let table = db.create_table("my_table", schema).expect("create table");
```

## 5. Insert a row

```rust
use mongreldb_core::value::Value;

let embedding: Vec<f32> = vec![0.1; 384]; // normally from an embedding model
let sparse = vec![(1u32, 1.0f32), (42, 1.0)]; // token id -> weight

let row = vec![
    ("id", Value::Int64(1)),
    ("content", Value::Bytes(b"MongrelDB standalone example".to_vec())),
    ("tags", Value::Bytes(b"[\"example\",\"rust\"]".to_vec())),
    ("embedding", Value::Embedding(embedding)),
    ("sparse", Value::Bytes(encode_sparse(&sparse))),
    ("created_at", Value::Int64(1721234567890)),
];

table.put(row).expect("insert row");
```

## 6. Sparse-only search

```rust
use mongreldb_core::condition::Condition;

let query_sparse = vec![(1u32, 1.0f32), (42, 1.0)];
let results = table
    .query()
    .and_condition(Condition::SparseMatch {
        column: "sparse",
        query: query_sparse,
        k: 10,
        weight: 1.0,
    })
    .limit(5)
    .execute()
    .expect("search");
```

## 7. Hybrid dense + sparse search with RRF

```rust
use mongreldb_core::search::{Retriever, Fusion, Rerank, SearchMetric, SearchRequest};

let query_embedding: Vec<f32> = vec![0.1; 384]; // from your embedding model
let query_sparse = vec![(1u32, 1.0f32), (42, 1.0)];

let request = SearchRequest::new()
    .must(Condition::BitmapEq { column: "state", value: b"active".to_vec() })
    .retriever(Retriever::Ann {
        column: "embedding",
        query: query_embedding,
        k: 50,
        weight: 1.0,
    })
    .retriever(Retriever::SparseMatch {
        column: "sparse",
        query: query_sparse,
        k: 50,
        weight: 1.0,
    })
    .fusion(Fusion::ReciprocalRank { constant: 60 })
    .rerank(Rerank::ExactVector {
        column: "embedding",
        query: vec![0.1; 384],
        metric: SearchMetric::Cosine,
        candidate_limit: 50,
        weight: 1.0,
    })
    .limit(5);

let hits = table.search(request).expect("hybrid search");
```

## 8. Bitmap and range filters

```rust
let results = table
    .query()
    .and_condition(Condition::BitmapEq { column: "memory_type", value: b"decision".to_vec() })
    .and_condition(Condition::Range {
        column: "created_at",
        lo: Some(1721234567890),
        hi: Some(1721239999999),
        lo_inclusive: true,
        hi_inclusive: true,
    })
    .limit(10)
    .execute()
    .expect("filtered search");
```

## 9. FM exact substring

```rust
let results = table
    .query()
    .and_condition(Condition::FmContains {
        column: "content",
        pattern: "WAL recovery",
    })
    .limit(10)
    .execute()
    .expect("exact substring search");
```

## 10. With an embedding model

In real code, replace the dummy `vec![0.1; 384]` with embeddings from a model like `sentence-transformers/all-MiniLM-L6-v2`. For example, using the `rust-bert` or `ort` crate, or by calling a Python embedding service.

```rust
// pseudo-code
let embedding = embed_model.encode("What was the WAL recovery issue?").await;
let request = SearchRequest::new()
    .retriever(Retriever::Ann { column: "embedding", query: embedding, k: 50, weight: 1.0 })
    .limit(5);
let hits = table.search(request).expect("dense search");
```

## 11. HTTP daemon equivalent

The same operations can be sent to `mongreldb-server` via the Kit HTTP API. See `MongrelDB_modes.md` for the daemon setup, and the daemon endpoint examples in the MongrelDB repository documentation.

```bash
# insert via HTTP (simplified)
curl -X POST http://127.0.0.1:8453/kit/txn \
  -H "Content-Type: application/json" \
  -d '{
    "table": "my_table",
    "ops": [{"op": "put", "row": {"id": 1, "content": "example", "tags": ["example"], "sparse": [[1, 1.0]]}}
  }'

# search via HTTP
# see mongreldb-server docs for the exact Kit search JSON shape
```
