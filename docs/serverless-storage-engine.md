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

## Performance roadmap (custom S3 + DynamoDB caching backend)

Iceberg-over-S3 is currently the **portable, correct-by-default** path — not the
**fastest**. These optimisations live entirely behind the `ENGINE` boundary, so
no application code (or React API Stack fat class) changes:

1. **DynamoDB-backed catalog + metadata cache.** Serve snapshot/manifest
   resolution from single-digit-ms DynamoDB lookups instead of multi-call S3
   metadata walks. DynamoDB becomes the low-latency index over Iceberg metadata
   whose bytes still live in S3.
2. **Custom S3 backend (Rust).** Purpose-built reader/writer with request
   hedging, byte-range coalescing, and parallel manifest fetch — replacing
   general-purpose fsspec on the hot path.
3. **EFS hot tier.** Opt-in, per-table EFS mount for latency-sensitive tables and
   the local catalog.
4. **Write-ahead + async compaction.** Buffer writes, commit snapshots in the
   background, and compact off the request path to bound small-file counts (and
   thus metadata size).

The goal is to **move the floor without moving the ceiling**: the Django ORM API
above stays identical while hot-read latency drops toward DynamoDB/EFS territory.

## Configuration recap

```python
DATABASES = {
    "default": {
        "ENGINE": "polars_iceberg.backend",
        "WAREHOUSE": "s3://reactapistack-prod/warehouse",
        "CATALOG_URI": "https://catalog.prod.internal",  # → DynamoDB-backed later
        "NAMESPACE": "production",
        "OPTIONS": {"s3.region": "us-east-1"},  # auth via Lambda IAM role
    }
}
```

See [React API Stack — Serverless Architecture](https://github.com/theserverkid/reactapistack/blob/master/docs/architecture.md)
for the full-stack picture (API exposure + storage + devops).
