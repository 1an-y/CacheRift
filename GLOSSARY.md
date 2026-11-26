# CacheRift

Core vocabulary for diagnosing multi-turn prompt cache behavior across LLM dialects and agent harnesses.

## Language

**Capture Record**:
An immutable snapshot of a single model request and its observed response metadata, headers, and raw or parsed body.
_Avoid_: Log entry, trace item

**Offset Space**:
The coordinate system (`wire_raw` for captured HTTP UTF-8 bytes, or `projected` for reconstructed canonical bytes) in which a byte span is measured.
_Avoid_: Byte index, string position

**Ordered Unit**:
A logically addressable segment of a model request projected in the dialect's and model generation's cache evaluation order.
_Avoid_: Segment, chunk, block

**Session Lineage**:
The directed parent-child relationship linking a turn to the prior turn whose prompt prefix it continues, tracked separately from cache reuse.
_Avoid_: Session ID, cache key

**Observed Change**:
A verifiable structural or wire-level difference between a turn and its matched parent, categorized by how it affects the prefix.
_Avoid_: Mutation, break cause

**Token Buckets**:
Non-overlapping input and output token counters (`uncached_input`, `cache_read`, tiered `cache_writes`, `output`) whose sum defines `total_input`.
_Avoid_: Usage stats, token count

**Evidence Grade**:
The provenance tier (`observed`, `provider_reported`, `inferred`, `unresolved`) attached to a candidate diagnostic conclusion.
_Avoid_: Confidence score

**Candidate Cause**:
A graded explanation hypothesis supported by observed changes, provider diagnostics, or timing, without forcing single-label exclusivity.
_Avoid_: Root cause, miss cause

**Probe Scenario**:
A controlled experiment made of a few byte-exact requests with declared baselines. It is admitted as a whole group or not at all.
_Avoid_: Benchmark, test case

**Budget Fuse**:
The check made before each probe request. It refuses to send anything that would cross the request cap or the worst-case cost cap; a cost that cannot be estimated blocks sending.
_Avoid_: Quota, limit

**Check Policy**:
The resolved severity of every rule for one build: error, warning, ignore, or allowed. Configuration names rules; rules it does not name keep their defaults.
_Avoid_: Threshold, SLA

**Finding**:
One policy verdict on one turn: a rule ID, its severity, a JSON path, and the record's line in the capture.
_Avoid_: Error log, violation count

**Repair Rule**:
One of four whitelisted rewrites that are provably equivalent for the model. Nothing outside the list may rewrite a request body.
_Avoid_: Fix, patch, optimization

**Repair Audit Entry**:
The hash trail of one repaired or refused record: whole-body fingerprints before and after, labelled with their coordinate space, plus a fingerprint pair for each rewritten node.
_Avoid_: Diff, changelog
