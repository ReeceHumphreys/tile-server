# Hosting survey: PMTiles delivery

Researched 2026-10-25 by parallel reviews of managed cloud, alternative CDN,
and self-hosted options. This is a source-based shortlist, not a hosted performance
benchmark. No cloud resources were created or data uploaded.

## Workload checked locally

- 71 immutable archives, totaling 28,550,357,219 bytes (28.55 GB / 26.59 GiB).
- Largest archive: 2,327,552,567 bytes (2.33 GB / 2.17 GiB), a Mars Viking face.
- 16 archives exceed 512 MiB; 17 exceed 512,000,000 bytes; 13 exceed 1 GiB.
  Vendor documentation often labels limits “MB”; retain margin when designing shards.
- 474,186 addressed tiles; 20 archives have leaf directories.
- Consumers read raw archive ranges with the existing PMTiles library, including
  cube imagery and numeric elevation. A provider's geographic XYZ endpoint is
  not automatically a replacement for this transport.

The important distinction is between being able to store an archive, serve a
range from it, and cache that range at an edge. Those are separate capabilities.

## Shortlist

| Option | Fit for the existing archive layout | Principal tradeoff |
| --- | --- | --- |
| S3 + CloudFront | Strong managed default to benchmark | Delivery/request pricing and AWS configuration |
| Bunny Storage + CDN | Worth a cost/performance trial | Verify cache slicing, validators, and cold-range behavior |
| SSD-backed VPS + existing Caddy image | Strong simple baseline, especially for nearby consumers | One region, finite uplink, host operations and backups |
| GCS + Cloud CDN | Good when already on Google Cloud | Load balancer setup and range-fill amplification |
| Azure Blob + Front Door | Viable when already on Azure | Base fee and 8 MB origin fill chunks |
| R2 + ordinary Cloudflare cache | Storage/delivery work; large archives encounter cache limits | Needs smaller shards, a higher limit, or custom range/tile caching |
| Vercel Blob | Transport is plausible; weak default for current large archives | 512 MB whole-blob cache limit and delivery economics |
| B2 + CDN | Potential inexpensive origin | CDN behavior still determines range performance and caching |

These rankings reflect integration fit and operating effort. They do not establish
which provider has the lowest latency for our consumers.

## Managed cloud delivery

**S3 + CloudFront.** CloudFront documents caching requested ranges and filling
missing ranges from an origin, sometimes fetching a larger interval. Its documented
large-object range behavior comfortably accommodates our individual archives.
Use a private S3 origin with Origin Access Control and expose the distribution.
Origin privacy is separate from requiring consumers to authenticate.
[Range behavior](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/RangeGETs.html),
[private S3 origins](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-restricting-access-to-s3.html).

**Google Cloud Storage + Cloud CDN.** Supports objects up to 100 GiB with
range-capable origins. Its documentation describes an initial range-capability
discovery response and subsequent aligned fills of approximately 2 MiB. Test sparse
tile reads as well as warm reuse; load-balancing configuration is part of the setup.
[Caching details](https://docs.cloud.google.com/cdn/docs/caching).

**Azure Blob + Front Door Standard/Premium.** Front Door documents uncapped
large-file delivery with cached 8 MB origin chunks. This could amplify cold
origin traffic for small, scattered tile requests; it is not a measured client
latency disadvantage. Use current Standard/Premium products, not the retiring
Classic offering.
[Large-file behavior](https://learn.microsoft.com/en-us/azure/frontdoor/front-door-caching).

## Cloudflare and Vercel size limits

Cloudflare's ordinary R2 cache integration lists a 512 MB limit for Free/Pro/Business
and a larger Enterprise default. Its range documentation explicitly applies the
limit to the full size in `Content-Range`; requesting a small interval does not
bypass it. Our 16 archives over 512 MiB are therefore a real fit concern. This does
not prevent R2 from storing or delivering them. A Worker that caches chunks or
decoded tiles is another architecture with code and request costs to evaluate.
[R2 cache integration](https://developers.cloudflare.com/cache/interaction-cloudflare-products/r2/),
[range cache limitations](https://developers.cloudflare.com/cache/reference/range-requests/).

R2 Standard lists $0.015/GB-month, $0.36/million Class B operations, and no internet
egress fees, with included monthly allowances. This can make direct R2 economical
even when some objects do not edge-cache, but it does not prove good cold latency.
[R2 pricing](https://developers.cloudflare.com/r2/pricing/).

**Vercel Blob** accepts files up to 5 TB, but its docs say blobs over 512 MB always
miss the cache. It charges storage, operations and delivery, including origin
transfer on misses. This is the same practical large-archive issue, despite the
much higher upload limit. Range reads are documented; signed URLs also permit
direct private delivery.
[Blob limits/pricing](https://vercel.com/docs/vercel-blob/usage-and-pricing),
[range examples](https://vercel.com/docs/vercel-blob/examples),
[signed URLs](https://vercel.com/docs/vercel-blob/vercel-signed-urls).

Vercel's Flat Rate CDN eligibility excludes bulk file distribution, file-hosting
use, and workloads primarily transferring large amounts of data. Whether an
independent planetary tile endpoint qualifies needs provider confirmation; do not
budget from the advertised transfer allowance alone.
[Flat Rate CDN eligibility](https://vercel.com/docs/pricing/flat-rate-cdn).

For either platform, sharding archives below cache limits would require explicit
shard routing in the manifest/reader. It is feasible, but our current face-to-archive
mapping does not implement it. It should earn its complexity through measured gains.

## Bunny and self-hosting

Bunny is a useful alternative to benchmark because its CDN provides cache slicing
for range-oriented large files. Verify the relevant delivery optimization settings,
ETag behavior, CORS, and the actual PMTiles reader. A missing exposed ETag is not
automatically disqualifying for strictly immutable content-hash URLs, but archive
consistency and conditional behavior still need explicit testing.
[Range delivery](https://bunny.net/docs/cdn/frequently-asked-questions/range-requests),
[ETag behavior](https://bunny.net/docs/cdn/frequently-asked-questions/etag-support),
[CDN pricing](https://bunny.net/pricing),
[Storage pricing](https://bunny.net/pricing/storage/).

Bunny lists single-region storage from $0.01/GB-month and SSD storage from
$0.02/GB-month, without API fees. Standard CDN delivery starts at $0.01/GB in
Europe/North America, with regional rates elsewhere and a $1 monthly minimum.
These are service rates, not a measured workload bill. Its published limits do
not establish that any arbitrary archive size will cache; test our 2.33 GB file.

Backblaze B2 supports range reads and can be an inexpensive durable origin. It
lists storage from $6.95/TB-month, free transactions, ordinary free egress up to
three times average stored data, and selected CDN partner egress arrangements.
B2 behind Cloudflare still encounters Cloudflare's cache-object limit. It is
useful when already adopted, but adds a vendor compared with integrated Bunny.
[B2 pricing](https://www.backblaze.com/cloud-storage/pricing),
[S3 GetObject](https://www.backblaze.com/apidocs/s3-get-object).

A VPS with SSD/NVMe and the existing Caddy image is a practical baseline. The
dataset is small enough for a single disk, and concurrent clients do not require
an object-storage API. Provision roughly 80–100 GB if retaining a fully changed
second release and operating-system space, rather than choosing the smallest disk
that barely fits the current release. This is planning headroom, not a benchmark.
Manage OS updates, TLS, monitoring and backups; host/network failures are our
responsibility. A CDN can be placed in front later.

DigitalOcean's Basic listings include an 80 GiB / 4,000 GiB-transfer configuration
at $24/month before backups and extras; region, taxes, excess transfer and current
availability must be checked when ordering. Hetzner is another useful quote, but
plan availability and included transfer differ by region. Do not extrapolate an
EU price to a US-west deployment.
[DigitalOcean pricing](https://www.digitalocean.com/pricing/droplets),
[Hetzner cloud](https://www.hetzner.com/cloud/).

Do not add a self-hosted S3 system merely to serve this 28.55 GB static release.
It adds storage administration without removing the need for storage capacity,
backup, or HTTP delivery. Shared/NAS storage is reasonable when already available.

## Costs and benchmark before selection

Storage size alone is not a monthly-bill estimate. Compare:

1. Stored bytes, including retained releases and replicas.
2. Client request count and bytes, including archive directories and retries.
3. Origin request count and fetched bytes, including CDN fill expansion.
4. Regional delivery rates, fixed fees, free allowances, and eligibility.
5. Operating time, backups, monitoring, and recovery requirements.

Illustrative traffic profiles: one million 100 KB responses is about 100 GB of
client delivery; ten million is about 1 TB. These are scenarios, not measured
viewer usage. A high byte cache-hit ratio does not necessarily imply the same
request hit ratio, and origin bytes can exceed client bytes on sparse cold reads.

Benchmark the exact same archives and camera paths on CloudFront, Bunny, and an
SSD-backed Caddy origin first. Include geographically close and distant consumers;
1/10/50/100 independent sessions; synchronized and scattered views; cold origin,
warm CDN, and warm browser-directory states. Disable browser payload cache when
measuring edge behavior and record the headers proving hit/miss status.

Measure p50/p95/p99 time to useful detail, request latency, sustained request
throughput, errors/timeouts/retries, client/origin bytes, cache-fill amplification,
cost per session, and operating effort. Check exact tile hashes, 206/416 behavior,
CORS, content length, validators, compression, cancellation, and release switches.
Do not compare a warm CDN path with a cold server path.

Keep PMTiles archive HTTP transport compression off; compression inside the file
is separate. For private consumers, test token lifetime and renewal without
changing cache identity or exposing origin credentials.

## Recommendation

For broadly distributed consumers, start the hosted trial with **S3 + CloudFront**
and **Bunny Storage + CDN**, retaining **VPS + Caddy** as a low-complexity baseline.
Use an existing cloud account or regional customer concentration to adjust that
shortlist. R2 and Vercel deserve trials if their operational convenience matters,
but do not assume their ordinary edge caches accelerate our largest archives.

The service's Python pipeline and published release contract can remain unchanged
across these storage choices. FastAPI and a database are separate management-plane
decisions, not prerequisites for concurrent tile downloads.
