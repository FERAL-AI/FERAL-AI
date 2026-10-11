# Accessibility value and URL inspection

Actual native9.60 Chrome inspection retained the address bar label but omitted
its current value. `find` searched only the resolved label, and `describe`
reported attribute names without reading their values. The agent could not
verify the document URL directly and spent extra tool rounds on that omission.

Snapshot, find and describe now report nonsecure current values and URLs
separately from labels. Each is bounded to2048 characters with explicit
truncation; text-tree previews are escaped and bounded to240 characters. Find
matches those values while preserving the selected window and exact AX element
reference. Real NSURL attributes are supported; unsupported objects are not
stringified. Secure role/subrole values and URLs are never read or serialized.
The data remains an untrusted observation, not input or navigation authority.
No action, permission or locator protocol changes.

Failure-first tests reproduce missing address values, URL search misses and
describe omissions. Parent integration passes99 tests with14 live checks
skipped and five existing warnings; full-core CI Ruff passes. Tests cover
secure never-read guards, forged secure serialization, stale references,
duplicate URLs in different windows, truncation, finite numeric values and
unsupported attributes. Live Chrome verification remains a separate gate.
