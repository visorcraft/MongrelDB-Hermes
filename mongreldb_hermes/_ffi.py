"""Low-level Python ctypes wrapper for MongrelDB C FFI.

Matches the C ABI in mongreldb/crates/mongreldb-ffi/include/mongreldb.h.
Only the subset needed for the memory provider is exposed.
"""
import ctypes
from ctypes import c_char, c_char_p, c_double, c_float, c_int8, c_int32, c_int64, c_size_t, c_uint8, c_uint16, c_uint32, c_uint64, c_void_p, POINTER, Structure, Union, byref
import os

class MongrelDBError(Exception):
    pass

LIB_PATH = os.environ.get("MONGRELDB_LIB", "/path/to/mongreldb/crates/mongreldb-ffi/target/release/libmongreldb.so")
_lib = ctypes.CDLL(LIB_PATH)

MDB_TYPE_INT64 = 4
MDB_TYPE_BYTES = 19
MDB_TYPE_EMBEDDING = 20

MDB_INDEX_BITMAP = 0
MDB_INDEX_FM = 1
MDB_INDEX_ANN = 2
MDB_INDEX_LEARNED_RANGE = 3
MDB_INDEX_MIN_HASH = 4
MDB_INDEX_SPARSE = 5

MDB_COND_PK = 0
MDB_COND_BITMAP_EQ = 1
MDB_COND_ANN = 4
MDB_COND_FM_CONTAINS = 5
MDB_COND_SPARSE_MATCH = 9
MDB_COND_MIN_HASH_SIMILAR = 10

MDB_VECTOR_COSINE = 0
MDB_COL_NULLABLE = 1
MDB_COL_PRIMARY_KEY = 2

MDB_VALUE_NULL = 0
MDB_VALUE_INT64 = 2
MDB_VALUE_BYTES = 4
MDB_VALUE_EMBEDDING = 5

MDB_RETRIEVER_ANN = 0
MDB_RETRIEVER_SPARSE = 1
MDB_RETRIEVER_MIN_HASH = 2

MDB_SEARCH_METRIC_COSINE = 0
MDB_SEARCH_METRIC_DOT = 1
MDB_SEARCH_METRIC_EUCLIDEAN = 2

MDB_FUSION_RECIPROCAL_RANK = 0



class _ByteSlice(Structure):
    _fields_ = [("data", POINTER(c_uint8)), ("len", c_size_t)]


class _EmbeddingSlice(Structure):
    _fields_ = [("data", POINTER(c_float)), ("len", c_size_t)]


class _SparseTerm(Structure):
    _fields_ = [("token", c_uint32), ("weight", c_float)]


class _SparseTermArray(Structure):
    _fields_ = [("items", POINTER(_SparseTerm)), ("len", c_size_t)]


class _MinHashMembers(Structure):
    _fields_ = [("items", POINTER(c_char_p)), ("len", c_size_t)]


class _ByteSliceArray(Structure):
    _fields_ = [("items", POINTER(_ByteSlice)), ("len", c_size_t)]


class _ValueUnion(Union):
    _fields_ = [
        ("b", c_uint8),
        ("i64", c_int64),
        ("f64", c_double),
        ("bytes", _ByteSlice),
        ("embedding", _EmbeddingSlice),
        ("decimal", c_uint8 * 16),
        ("interval", c_int8 * 24),
        ("uuid", c_uint8 * 16),
        ("json", c_uint8 * 16),
    ]


class _CValue(Structure):
    _anonymous_ = ("v",)
    _fields_ = [("tag", c_int32), ("v", _ValueUnion)]


class _CellInput(Structure):
    _fields_ = [("column_id", c_uint16), ("value", _CValue)]


class _CellInputArray(Structure):
    _fields_ = [("data", POINTER(_CellInput)), ("len", c_size_t)]


class _ByteSliceArray(Structure):
    _fields_ = [("items", POINTER(_ByteSlice)), ("len", c_size_t)]


class _Condition(Structure):
    _fields_ = [
        ("kind", c_int32),
        ("column_id", c_uint16),
        ("int64_lo", c_int64),
        ("int64_hi", c_int64),
        ("float64_lo", c_double),
        ("float64_hi", c_double),
        ("lo_inclusive", c_uint8),
        ("hi_inclusive", c_uint8),
        ("k", c_uint32),
        ("bytes", _ByteSlice),
        ("byte_values", _ByteSliceArray),
        ("embedding", _EmbeddingSlice),
        ("sparse", _SparseTermArray),
        ("minhash_members", _MinHashMembers),
    ]


class _ColumnDef(Structure):
    _fields_ = [
        ("id", c_uint16),
        ("name", c_char_p),
        ("ty", c_int32),
        ("flags", c_uint32),
        ("embedding_dim", c_uint32),
        ("decimal_precision", c_uint8),
        ("decimal_scale", c_int8),
        ("enum_variants", c_void_p),
    ]


class _IndexDef(Structure):
    _fields_ = [("name", c_char_p), ("column_id", c_uint16), ("kind", c_int32)]


class _Cell(Structure):
    _fields_ = [("column_id", c_uint16), ("value", _CValue)]


class _CellSlice(Structure):
    _fields_ = [("data", POINTER(_Cell)), ("len", c_size_t)]


class _Row(Structure):
    _fields_ = [("row_id", c_uint64), ("cells", _CellSlice)]

class _Retriever(Structure):
    _fields_ = [
        ("kind", c_int32),
        ("column_id", c_uint16),
        ("name", c_char_p),
        ("weight", c_double),
        ("k", c_uint32),
        ("embedding", _EmbeddingSlice),
        ("sparse_terms", _SparseTermArray),
        ("minhash_members", _MinHashMembers),
    ]


class _RetrieverArray(Structure):
    _fields_ = [("data", POINTER(_Retriever)), ("len", c_size_t)]


class _Fusion(Structure):
    _fields_ = [("kind", c_int32), ("reciprocal_rank_constant", c_uint32)]


class _Rerank(Structure):
    _fields_ = [
        ("kind", c_int32),
        ("embedding_column", c_uint16),
        ("query", _EmbeddingSlice),
        ("metric", c_int32),
        ("candidate_limit", c_uint32),
        ("weight", c_double),
    ]


class _ConditionArray(Structure):
    _fields_ = [("data", POINTER(_Condition)), ("len", c_size_t)]


class _Projection(Structure):
    _fields_ = [("data", POINTER(c_uint16)), ("len", c_size_t)]


class _SearchRequest(Structure):
    _fields_ = [
        ("must", _ConditionArray),
        ("retrievers", _RetrieverArray),
        ("fusion", _Fusion),
        ("rerank", POINTER(_Rerank)),
        ("limit", c_size_t),
        ("projection", _Projection),
    ]



# Function signatures
_lib.mongreldb_create.restype = c_void_p
_lib.mongreldb_create.argtypes = [c_char_p]
_lib.mongreldb_open.restype = c_void_p
_lib.mongreldb_open.argtypes = [c_char_p]
_lib.mongreldb_create_with_credentials.restype = c_void_p
_lib.mongreldb_create_with_credentials.argtypes = [c_char_p, c_char_p, c_char_p]
_lib.mongreldb_open_with_credentials.restype = c_void_p
_lib.mongreldb_open_with_credentials.argtypes = [c_char_p, c_char_p, c_char_p]
_lib.mongreldb_create_encrypted.restype = c_void_p
_lib.mongreldb_create_encrypted.argtypes = [c_char_p, c_char_p]
_lib.mongreldb_open_encrypted.restype = c_void_p
_lib.mongreldb_open_encrypted.argtypes = [c_char_p, c_char_p]
_lib.mongreldb_create_encrypted_with_credentials.restype = c_void_p
_lib.mongreldb_create_encrypted_with_credentials.argtypes = [c_char_p, c_char_p, c_char_p, c_char_p]
_lib.mongreldb_open_encrypted_with_credentials.restype = c_void_p
_lib.mongreldb_open_encrypted_with_credentials.argtypes = [c_char_p, c_char_p, c_char_p, c_char_p]
_lib.mongreldb_database_close.restype = c_int32
_lib.mongreldb_database_close.argtypes = [c_void_p]
_lib.mongreldb_database_free.restype = None
_lib.mongreldb_database_free.argtypes = [c_void_p]
_lib.mongreldb_last_error.restype = c_char_p

_lib.mongreldb_schema_begin.restype = c_void_p
_lib.mongreldb_schema_add_column.restype = c_int32
_lib.mongreldb_schema_add_column.argtypes = [c_void_p, POINTER(_ColumnDef)]
_lib.mongreldb_schema_add_index.restype = c_int32
_lib.mongreldb_schema_add_index.argtypes = [c_void_p, POINTER(_IndexDef)]
_lib.mongreldb_schema_build.restype = c_void_p
_lib.mongreldb_schema_build.argtypes = [c_void_p]
_lib.mongreldb_schema_free.restype = None
_lib.mongreldb_schema_free.argtypes = [c_void_p]
_lib.mongreldb_create_table.restype = c_int32
_lib.mongreldb_create_table.argtypes = [c_void_p, c_char_p, c_void_p, POINTER(c_uint64)]
_lib.mongreldb_database_table.restype = c_void_p
_lib.mongreldb_database_table.argtypes = [c_void_p, c_char_p]
_lib.mongreldb_table_free.restype = None
_lib.mongreldb_table_free.argtypes = [c_void_p]
_lib.mongreldb_table_put.restype = c_int32
_lib.mongreldb_table_put.argtypes = [c_void_p, POINTER(_CellInputArray), POINTER(c_uint64)]
_lib.mongreldb_query_begin.restype = c_void_p
_lib.mongreldb_query_add.restype = c_int32
_lib.mongreldb_query_add.argtypes = [c_void_p, POINTER(_Condition)]
_lib.mongreldb_query_set_projection.restype = c_int32
_lib.mongreldb_query_set_projection.argtypes = [c_void_p, POINTER(c_uint16), c_size_t]
_lib.mongreldb_query_set_limit.restype = c_int32
_lib.mongreldb_query_set_limit.argtypes = [c_void_p, c_size_t]
_lib.mongreldb_query_set_offset.restype = c_int32
_lib.mongreldb_query_set_offset.argtypes = [c_void_p, c_size_t]
_lib.mongreldb_query_free.restype = None
_lib.mongreldb_query_free.argtypes = [c_void_p]
_lib.mongreldb_table_query.restype = c_void_p
_lib.mongreldb_table_query.argtypes = [c_void_p, c_void_p]
_lib.mongreldb_result_count.restype = c_size_t
_lib.mongreldb_result_count.argtypes = [c_void_p]
_lib.mongreldb_result_row.restype = c_int32
_lib.mongreldb_result_row.argtypes = [c_void_p, c_size_t, POINTER(_Row)]
_lib.mongreldb_result_free.restype = None
_lib.mongreldb_result_free.argtypes = [c_void_p]

_lib.mongreldb_search_request_begin.restype = c_void_p
_lib.mongreldb_search_request_begin.argtypes = []
_lib.mongreldb_search_request_free.restype = None
_lib.mongreldb_search_request_free.argtypes = [c_void_p]
_lib.mongreldb_table_search.restype = c_void_p
_lib.mongreldb_table_search.argtypes = [c_void_p, POINTER(_SearchRequest)]



def _check(code, msg="MongrelDB"):
    if code != 0:
        err = _lib.mongreldb_last_error().decode("utf-8", "replace")
        raise MongrelDBError(f"{msg}: {err} (code {code})")


def _check_ptr(ptr, msg="MongrelDB"):
    if not ptr:
        err = _lib.mongreldb_last_error().decode("utf-8", "replace")
        raise MongrelDBError(f"{msg}: {err}")
    return ptr


class Database:
    def __init__(self, db_ptr):
        self._ptr = db_ptr

    @classmethod
    def create(cls, path):
        return cls(_check_ptr(_lib.mongreldb_create(path.encode("utf-8")), "Database.create"))

    @classmethod
    def open(cls, path):
        return cls(_check_ptr(_lib.mongreldb_open(path.encode("utf-8")), "Database.open"))

    @classmethod
    def create_with_credentials(cls, path, username, password):
        return cls(
            _check_ptr(
                _lib.mongreldb_create_with_credentials(
                    path.encode("utf-8"),
                    username.encode("utf-8"),
                    password.encode("utf-8"),
                ),
                "Database.create_with_credentials",
            )
        )

    @classmethod
    def open_with_credentials(cls, path, username, password):
        return cls(
            _check_ptr(
                _lib.mongreldb_open_with_credentials(
                    path.encode("utf-8"),
                    username.encode("utf-8"),
                    password.encode("utf-8"),
                ),
                "Database.open_with_credentials",
            )
        )

    @classmethod
    def create_encrypted(cls, path, passphrase):
        return cls(
            _check_ptr(
                _lib.mongreldb_create_encrypted(
                    path.encode("utf-8"),
                    passphrase.encode("utf-8"),
                ),
                "Database.create_encrypted",
            )
        )

    @classmethod
    def open_encrypted(cls, path, passphrase):
        return cls(
            _check_ptr(
                _lib.mongreldb_open_encrypted(
                    path.encode("utf-8"),
                    passphrase.encode("utf-8"),
                ),
                "Database.open_encrypted",
            )
        )

    @classmethod
    def create_encrypted_with_credentials(cls, path, passphrase, username, password):
        return cls(
            _check_ptr(
                _lib.mongreldb_create_encrypted_with_credentials(
                    path.encode("utf-8"),
                    passphrase.encode("utf-8"),
                    username.encode("utf-8"),
                    password.encode("utf-8"),
                ),
                "Database.create_encrypted_with_credentials",
            )
        )

    @classmethod
    def open_encrypted_with_credentials(cls, path, passphrase, username, password):
        return cls(
            _check_ptr(
                _lib.mongreldb_open_encrypted_with_credentials(
                    path.encode("utf-8"),
                    passphrase.encode("utf-8"),
                    username.encode("utf-8"),
                    password.encode("utf-8"),
                ),
                "Database.open_encrypted_with_credentials",
            )
        )

    @classmethod
    def open_or_create(cls, path, *, passphrase=None, username=None, password=None):
        """Open existing or create missing, with optional encryption and credentials.

        Encryption (passphrase) and logical auth (username/password) are orthogonal.
        Prefer passphrase set for at-rest protection; credentials enforce who may open.
        """
        catalog = os.path.join(path, "CATALOG")
        exists = os.path.exists(catalog)
        has_pass = bool(passphrase)
        has_user = bool(username) and bool(password)
        if exists:
            if has_pass and has_user:
                return cls.open_encrypted_with_credentials(path, passphrase, username, password)
            if has_pass:
                return cls.open_encrypted(path, passphrase)
            if has_user:
                return cls.open_with_credentials(path, username, password)
            return cls.open(path)
        if has_pass and has_user:
            return cls.create_encrypted_with_credentials(path, passphrase, username, password)
        if has_pass:
            return cls.create_encrypted(path, passphrase)
        if has_user:
            return cls.create_with_credentials(path, username, password)
        return cls.create(path)

    def close(self):
        if self._ptr:
            _lib.mongreldb_database_close(self._ptr)
            _lib.mongreldb_database_free(self._ptr)
            self._ptr = None

    def __del__(self):
        self.close()

    def create_table(self, name, schema_obj):
        out = c_uint64(0)
        _check(_lib.mongreldb_create_table(self._ptr, name.encode("utf-8"), schema_obj._ptr, byref(out)), "create_table")
        schema_obj.mark_consumed()
        return out.value

    def table(self, name):
        return _check_ptr(_lib.mongreldb_database_table(self._ptr, name.encode("utf-8")), "Database.table")


class Schema:
    def __init__(self, ptr):
        self._ptr = ptr

    @classmethod
    def build(cls, columns, indexes):
        b = _check_ptr(_lib.mongreldb_schema_begin(), "schema_begin")
        for col in columns:
            d = _ColumnDef()
            d.id = col["id"]
            d.name = col["name"].encode("utf-8")
            d.ty = col["ty"]
            d.flags = col.get("flags", 0)
            d.embedding_dim = col.get("embedding_dim", 0)
            d.decimal_precision = 0
            d.decimal_scale = 0
            d.enum_variants = None
            _check(_lib.mongreldb_schema_add_column(b, byref(d)), "schema_add_column")
        for idx in indexes:
            i = _IndexDef()
            i.name = idx["name"].encode("utf-8")
            i.column_id = idx["column_id"]
            i.kind = idx["kind"]
            _check(_lib.mongreldb_schema_add_index(b, byref(i)), "schema_add_index")
        ptr = _lib.mongreldb_schema_build(b)
        if not ptr:
            raise MongrelDBError("schema_build failed")
        obj = cls(ptr)
        obj._consumed = False
        return obj

    def mark_consumed(self):
        self._consumed = True
        self._ptr = None

    def __del__(self):
        if getattr(self, "_consumed", False):
            return
        if self._ptr:
            _lib.mongreldb_schema_free(self._ptr)
            self._ptr = None


class Table:
    def __init__(self, db, name):
        self._db = db
        self._name = name
        self._ptr = None
        self._ptr = db.table(name)

    def put(self, cells):
        arr = _CellInputArray()
        arr.len = len(cells)
        inputs = (_CellInput * len(cells))()
        backing = []
        for i, (col_id, value) in enumerate(cells):
            inp = inputs[i]
            inp.column_id = col_id
            if isinstance(value, int):
                inp.value.tag = MDB_VALUE_INT64
                inp.value.i64 = value
            elif isinstance(value, bytes):
                inp.value.tag = MDB_VALUE_BYTES
                arr_data = (c_uint8 * len(value))(*value)
                backing.append(arr_data)
                inp.value.bytes.data = ctypes.cast(arr_data, POINTER(c_uint8))
                inp.value.bytes.len = len(value)
            elif isinstance(value, (list, tuple)):
                if value and isinstance(value[0], float):
                    inp.value.tag = MDB_VALUE_EMBEDDING
                    arr_data = (c_float * len(value))(*value)
                    backing.append(arr_data)
                    inp.value.embedding.data = ctypes.cast(arr_data, POINTER(c_float))
                    inp.value.embedding.len = len(value)
                else:
                    inp.value.tag = MDB_VALUE_NULL
                    inp.value.b = 0
            else:
                raise MongrelDBError(f"unsupported cell value type: {type(value)}")
        arr.data = inputs
        backing.append(inputs)
        out = c_uint64(0)
        _check(_lib.mongreldb_table_put(self._ptr, byref(arr), byref(out)), "table_put")
        return out.value

    def query(self, conditions, limit=None, projection=None, offset=0):
        q = _lib.mongreldb_query_begin()
        if not q:
            raise MongrelDBError("query_begin failed")
        try:
            backing = []
            for cond in conditions:
                c = _make_condition(cond, backing)
                _check(_lib.mongreldb_query_add(q, byref(c)), "query_add")
            if limit is not None:
                _check(_lib.mongreldb_query_set_limit(q, limit), "query_set_limit")
            if offset:
                _check(_lib.mongreldb_query_set_offset(q, offset), "query_set_offset")
            if projection:
                proj = (c_uint16 * len(projection))(*projection)
                backing.append(proj)
                _check(_lib.mongreldb_query_set_projection(q, proj, len(projection)), "query_set_projection")
            res = _check_ptr(_lib.mongreldb_table_query(self._ptr, q), "table_query")
            return Result(res)
        finally:
            _lib.mongreldb_query_free(q)

    def search(
        self,
        *,
        retrievers,
        must=None,
        fusion_kind=MDB_FUSION_RECIPROCAL_RANK,
        fusion_constant=60,
        rerank=None,
        limit=10,
        projection=None,
    ):
        """Hybrid search: retrievers + fusion + optional exact-vector rerank.

        ``retrievers`` is a list of dicts with keys:
          kind, column_id, name, weight, k, and kind-specific payload
          (embedding / sparse / members).

        ``must`` is optional hard filters (same shape as ``query`` conditions).
        ``rerank`` is optional: embedding_column, query, metric, candidate_limit, weight.
        """
        backing = []
        req = _SearchRequest()

        must = must or []
        if must:
            conds = (_Condition * len(must))()
            for i, cond in enumerate(must):
                conds[i] = _make_condition(cond, backing)
            backing.append(conds)
            req.must.data = conds
            req.must.len = len(must)
        else:
            req.must.data = None
            req.must.len = 0

        if not retrievers:
            raise MongrelDBError("search requires at least one retriever")
        ret_arr = (_Retriever * len(retrievers))()
        for i, r in enumerate(retrievers):
            ret_arr[i] = _make_retriever(r, backing)
        backing.append(ret_arr)
        req.retrievers.data = ret_arr
        req.retrievers.len = len(retrievers)

        req.fusion.kind = fusion_kind
        req.fusion.reciprocal_rank_constant = fusion_constant

        if rerank:
            rr = _Rerank()
            rr.kind = 0
            rr.embedding_column = rerank["embedding_column"]
            emb = rerank["query"]
            emb_arr = (c_float * len(emb))(*[float(x) for x in emb])
            backing.append(emb_arr)
            rr.query.data = ctypes.cast(emb_arr, POINTER(c_float))
            rr.query.len = len(emb)
            rr.metric = rerank.get("metric", MDB_SEARCH_METRIC_COSINE)
            rr.candidate_limit = rerank.get("candidate_limit", max(limit, 64))
            rr.weight = float(rerank.get("weight", 1.0))
            backing.append(rr)
            req.rerank = ctypes.pointer(rr)
        else:
            req.rerank = None

        req.limit = limit
        if projection:
            proj = (c_uint16 * len(projection))(*projection)
            backing.append(proj)
            req.projection.data = proj
            req.projection.len = len(projection)
        else:
            req.projection.data = None
            req.projection.len = 0

        res = _check_ptr(_lib.mongreldb_table_search(self._ptr, byref(req)), "table_search")
        # Keep backing alive until the call returns; Result owns the engine side.
        del backing
        return Result(res)

    def __del__(self):
        if self._ptr:
            _lib.mongreldb_table_free(self._ptr)
            self._ptr = None


class Result:
    def __init__(self, ptr):
        self._ptr = ptr
        self._count = _lib.mongreldb_result_count(ptr)

    def __len__(self):
        return self._count

    def __iter__(self):
        for i in range(self._count):
            row = _Row()
            _check(_lib.mongreldb_result_row(self._ptr, i, byref(row)), f"result_row {i}")
            cells = {}
            for j in range(row.cells.len):
                cell = row.cells.data[j]
                cells[cell.column_id] = _value_from_c(cell.value)
            yield cells

    def __del__(self):
        if self._ptr:
            _lib.mongreldb_result_free(self._ptr)
            self._ptr = None


def _make_condition(cond, backing):
    c = _Condition()
    c.kind = cond["kind"]
    c.column_id = cond.get("column_id", 0)
    c.k = cond.get("k", 64)
    c.int64_lo = 0
    c.int64_hi = 0
    c.float64_lo = 0.0
    c.float64_hi = 0.0
    c.lo_inclusive = 1
    c.hi_inclusive = 1
    c.byte_values = _ByteSliceArray()
    if "pattern" in cond:
        b = cond["pattern"].encode("utf-8")
        arr = (c_uint8 * len(b))(*b)
        backing.append(arr)
        c.bytes.data = ctypes.cast(arr, POINTER(c_uint8))
        c.bytes.len = len(b)
    elif "bytes" in cond:
        b = cond["bytes"]
        arr = (c_uint8 * len(b))(*b)
        backing.append(arr)
        c.bytes.data = ctypes.cast(arr, POINTER(c_uint8))
        c.bytes.len = len(b)
    else:
        c.bytes.data = None
        c.bytes.len = 0
    if "embedding" in cond:
        emb = (c_float * len(cond["embedding"]))(*cond["embedding"])
        backing.append(emb)
        c.embedding.data = ctypes.cast(emb, POINTER(c_float))
        c.embedding.len = len(cond["embedding"])
    else:
        c.embedding.data = None
        c.embedding.len = 0
    if "sparse" in cond:
        terms = cond["sparse"]
        arr = (_SparseTerm * len(terms))()
        for i, (t, w) in enumerate(terms):
            arr[i].token = t
            arr[i].weight = w
        backing.append(arr)
        c.sparse.items = arr
        c.sparse.len = len(terms)
    else:
        c.sparse.items = None
        c.sparse.len = 0
    if "members" in cond:
        members = cond["members"]
        c_strs = [m.encode("utf-8") for m in members]
        ptrs = (c_char_p * len(members))(*c_strs)
        backing.extend(c_strs)
        backing.append(ptrs)
        c.minhash_members.items = ptrs
        c.minhash_members.len = len(members)
    else:
        c.minhash_members.items = None
        c.minhash_members.len = 0
    return c


def _make_retriever(r, backing):
    out = _Retriever()
    out.kind = r["kind"]
    out.column_id = r["column_id"]
    name = r.get("name") or f"retriever_{r['kind']}"
    name_b = name.encode("utf-8")
    backing.append(name_b)
    out.name = name_b
    out.weight = float(r.get("weight", 1.0))
    out.k = int(r.get("k", 64))
    out.embedding.data = None
    out.embedding.len = 0
    out.sparse_terms.items = None
    out.sparse_terms.len = 0
    out.minhash_members.items = None
    out.minhash_members.len = 0
    if "embedding" in r:
        emb = (c_float * len(r["embedding"]))(*[float(x) for x in r["embedding"]])
        backing.append(emb)
        out.embedding.data = ctypes.cast(emb, POINTER(c_float))
        out.embedding.len = len(r["embedding"])
    if "sparse" in r:
        terms = r["sparse"]
        arr = (_SparseTerm * len(terms))()
        for i, (t, w) in enumerate(terms):
            arr[i].token = int(t)
            arr[i].weight = float(w)
        backing.append(arr)
        out.sparse_terms.items = arr
        out.sparse_terms.len = len(terms)
    if "members" in r:
        members = r["members"]
        c_strs = [m.encode("utf-8") for m in members]
        ptrs = (c_char_p * len(members))(*c_strs)
        backing.extend(c_strs)
        backing.append(ptrs)
        out.minhash_members.items = ptrs
        out.minhash_members.len = len(members)
    return out


def _value_from_c(value):
    if value.tag == MDB_VALUE_INT64:
        return value.i64
    if value.tag == MDB_VALUE_BYTES:
        return bytes(value.bytes.data[:value.bytes.len])
    if value.tag == MDB_VALUE_EMBEDDING:
        return [value.embedding.data[i] for i in range(value.embedding.len)]
    return None
