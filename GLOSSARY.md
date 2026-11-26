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
