# django-iceberg as a Serverless, Object-Storage Engine

> **Role.** django-iceberg is the **database storage layer of
> [React API Stack](https://github.com/theserverkid/reactapistack)** — the
> bottom of a full-stack framework whose system of record is object storage. It
> turns the Django ORM into reads and writes against Apache Iceberg tables on S3,
> so Django can run as **stateless, elastic compute** (AWS Lambda) over durable
> object storage. The model is [SurrealDB Gen-3 / SurrealDS](https://surrealdb.com/platform/surrealds):
> separate compute from storage, keep transactional data in commodity object
> storage, and let compute scale to zero.

## Why this design

A traditional RDBMS couples compute and storage on one server. By putting the
**system of record in S3** and treating compute as disposable, the engine inherits
object storage's economics and durability (11 nines, effectively unbounded
capacity) and enables **scale-to-zero serverless Django**: when no request is in
flight, nothing runs; state persists in S3 and the Iceberg catalog.

```
AWS Lambda (stateless Django)
        │  Django ORM
        ▼
polars_iceberg.backend     ← this package
        │
        ├── Iceberg REST catalog   (metadata pointer; Aurora/DynamoDB)
        └── S3 warehouse           (Parquet + manifests = system of record)
            └── EFS (optional)      (hot-tier IO for latency-sensitive tables)
```

## Rust-package-first hot path

The engine is deliberately built on **Rust / Arrow-native** packages, not
pure-Python data plumbing. The cheapest S3 byte is the one never fetched, and the
cheapest CPU cycle is the one spent in native code:

| Concern | Package | Why |
| --- | --- | --- |
| DataFrame execution | **Polars** (Rust) | vectorised, multi-threaded; ORM SQL compiles to Polars ops |
| Columnar interchange | **Arrow / PyArrow** | zero-copy between Polars ↔ Iceberg ↔ Parquet |
| Table format | **PyIceberg** | snapshots, schema evolution, partition pruning |

Predicate pushdown and column pruning run in Rust before bytes cross into
Python, which is what makes "the database is a pile of Parquet files" perform.
The planned custom S3 backend (below) extends this with a **Rust S3 client**
(`object_store`-style) for range-coalescing and parallel manifest fetch.

## Latency & IO: S3 vs EFS

Object storage trades latency for durability. For a framework that must
**scale to millions**, the IO path must be explicit:

- **S3 first-byte latency** (tens of ms) dominates small point-lookup reads and
  is invisible on analytical scans. Hot `GET …/{id}/` reads feel it most.
- **Metadata round-trips are the tax.** snapshot → manifest list → manifests →
  data files can be several sequential calls before the first row. Collapsing
  these is the highest-leverage optimisation.
- **EFS as a hot tier.** Lambda can mount **Amazon EFS**. Mounting the warehouse
  (or a hot subset / local catalog) on EFS converts S3 first-byte latency into
  NFS latency for frequently-touched tables and gives a shared POSIX filesystem
  across concurrent invocations. EFS fronts S3 for hot data; **S3 remains the
  system of record.** This should be a per-table deployment knob, not all-or-nothing.
- **Partition for pruning.** Partition on the filtered dimension (`tenant_id`,
  date) so Iceberg skips whole files before any GET.
- **Reuse warm state.** Cache catalog clients and resolved table handles in
  module scope so warm invocations skip the metadata walk.

## The serverless engine (implemented)

The serverless engine ships in
[`polars_iceberg/backend/serverless.py`](../polars_iceberg/backend/serverless.py)
and is selected automatically. The Django backend (`base.py`) routes to it when
the database is configured for serverless operation — explicitly via
`"SERVERLESS": True` or a `dynamodb`/`glue` `CATALOG_TYPE`, or implicitly by
**auto-detecting an AWS Lambda runtime** (`AWS_LAMBDA_FUNCTION_NAME`). Otherwise
the plain `IcebergManager` is used unchanged, so local/dev behaviour is
untouched.

What it adds today:

- **Low-latency catalog tier.** A **DynamoDB-backed Iceberg catalog** (also Glue
  and REST) replaces the SQLite/REST default — single-digit-ms metadata pointer
  lookups instead of a multi-call S3 metadata walk.
- **Warm-state reuse.** A process-global, thread-safe `WarmCache` keeps catalog
  clients (indefinitely) and resolved table handles (TTL'd, default 30 s) alive
  **across warm Lambda invocations**, so repeat requests skip the metadata walk.
  Writers (`append`/`overwrite`/`delete`/`drop`) invalidate the handle so the next
  read sees the new snapshot.
- **EFS hot tier.** Setting `EFS_MOUNT` hosts a shared POSIX catalog/warehouse on
  Amazon EFS for latency-sensitive tables; S3 stays the durable system of record.

```python
# settings.py — production, serverless
DATABASES = {
    "default": {
        "ENGINE": "polars_iceberg.backend",
        "WAREHOUSE": "s3://reactapistack-prod/warehouse",  # system of record
        "CATALOG_URI": "dynamodb://iceberg-metadata",       # low-latency catalog
        "NAMESPACE": "production",
        # "SERVERLESS": True,        # force; otherwise auto-detected on Lambda
        # "CATALOG_TYPE": "dynamodb" # inferred from the dynamodb:// scheme
        # "DYNAMODB_TABLE": "iceberg-metadata",
        # "EFS_MOUNT": "/mnt/warehouse",  # optional hot tier
        # "CACHE_TTL": 30,                # warm table-handle TTL (seconds)
        "OPTIONS": {"s3.region": "us-east-1"},  # auth via Lambda IAM role
    }
}
```

Configuration resolution and the warm cache are covered by
[`tests/test_serverless.py`](../tests/test_serverless.py) (runs without the
cloud SDKs installed).

## Performance roadmap (remaining)

Iceberg-over-S3 is the **portable, correct-by-default** path; the DynamoDB
catalog + warm cache + EFS above close most of the metadata-latency gap. What's
left lives entirely behind the `ENGINE` boundary, so no application code (or
React API Stack fat class) changes:

- [x] **DynamoDB-backed catalog + warm metadata cache**
- [x] **EFS hot tier** (per-deployment mount)
- [ ] **Custom S3 backend (Rust).** Purpose-built reader/writer with request
      hedging, byte-range coalescing, and parallel manifest fetch — replacing
      general-purpose fsspec on the hot path.
- [ ] **Write-ahead + async compaction.** Buffer writes, commit snapshots in the
      background, and compact off the request path to bound small-file counts
      (and thus metadata size).

The goal is to **move the floor without moving the ceiling**: the Django ORM API
above stays identical while hot-read latency keeps dropping.

See [React API Stack — Serverless Architecture](https://github.com/theserverkid/reactapistack/blob/master/docs/architecture.md)
for the full-stack picture (API exposure + storage + devops).
