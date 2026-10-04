# DIST-01A: frozen packaged-runtime source comparison

October 2, 2026. A staged backend can differ from the current source even when
the native executable builds successfully. Candidate9.27 was manually compared
against all478 production Python files. The new read-only
[`check_packaged_core.py`](../../../scripts/check_packaged_core.py) makes this
comparison repeatable for future assembled candidates.

```sh
python3 scripts/check_packaged_core.py 'desktop-native/build/FERAL Native Preview.app' --revision 34a43e640597ca721fb915149f29c9b73b51394a
python3 -m unittest discover -s scripts/tests -p test_check_packaged_core.py -v
```

Actual immutable9.27: **478 checked, zero issues**, exit0. The tool compares
Git blob identities from the full frozen commit, independent of working-tree
edits. Missing, changed, unexpected or external/symlinked runtime source fails.
It rejects symbolic/abbreviated revision arguments and non-commit objects,
ignores ambient Git overrides/replacement refs, and neither reads file contents
into its report nor checks out, executes or modifies the payload.

**Nine real disposable Git tests passed** in0.804s, including stale/uncommitted
source, missing files, unexpected code, external symlink, invalid/absent commit
and empty inventory. Native CI runs these independently of the existing bundle
auditor fixtures. Targeted Ruff and actual CLI comparison passed.

This certifies tracked production Python payload equality only. Tests/build/dist
directories and compiled caches are excluded; the separate bundle audit governs
critical resources and suspicious files. It does not certify native compiler
inputs, dashboard assets, dependencies, secret absence, signing, provenance
authentication, clean-machine install, migration or a complete release. Retain
both source receipt and candidate hash alongside actual GUI evidence.
