# Historical wearable sample time

Historical BP and HRV relay data previously used nested data.ts for measurement
time. The HUP sender supplies envelope ts as send time, and the Mac's reserved
identity merge correctly gives that envelope priority. The resulting reading was
therefore interpreted as current even when it was historical.

The sender now uses the existing supported sample_ts field for historical BP/HRV.
It rejects nonfinite, nonpositive and future resolved epochs before selecting the
newest candidate, advancing its relay marker or emitting. Envelope identity and
send time retain their existing semantics; generic reserved-field merging is
unchanged. The existing BP fallback for omitted SDK timestamps remains inferred
and must not be described as a measured timestamp.

Verification compiles actual selected Foundation relay helpers, sender emit and
wire encoder blocks, then decodes their synthetic frames with the actual Mac
sample-time functions. Two original-shape counterexamples reproduce the defect;
two corrected frames preserve the sample epoch. Twenty-one Python wire checks
and ten Swift boundary checks pass. The original integrated source also passes
the full signed physical-device app build, strict deep signing verification and
in-place development-device installation. Source, index, project and vendor
preservation checks pass. The iOS source is local4a6b9d9cc8769b76714dd76519f8d39de6383a9a;
it is not published. Physical sensor, reconnect and audio acceptance remain open.

An offline phone with zero registered Mac nodes cannot supply fresh readings.
Stored source names alone do not prove fresh values or sample receipt. The phone's
seven-day local HR/SpO2/step backfill is not a durable transfer to the Mac. Required
remaining work includes authenticated idempotent history transfer with receipt
and cursor, canonical typed history ingestion and per-measurement timestamps in
current summaries. Current snapshot build time must not stand in for sample time.
Unsupported hardware metrics remain unavailable; no derived baseline is certified
by this sender correction.
