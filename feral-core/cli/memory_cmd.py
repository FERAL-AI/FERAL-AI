"""`feral memory` subcommand — view + switch the memory backend.

Usage
-----
    feral memory status          show which backend is active, counts, dim
    feral memory list            list known backend ids + whether deps are installed
    feral memory switch chroma   switch to the Chroma backend (persists to config)
    feral memory reembed check   report every stored vector's width, write nothing
    feral memory reembed         rewrite EVERY table's vectors at the active
                                 provider's dimension (see memory/reembed.py)

The config key is ``memory.backend`` in ``~/.feral/settings.json``.
Switching is cheap: it's just a config change. The brain reloads the
backend on next start.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

from config.loader import feral_home

_KNOWN_BACKENDS = {
    "sqlite_vec": ("memory.backends.sqlite_vec", "built-in (default)"),
    "chroma": ("memory.backends.chroma", "pip install feral-ai[memory-chroma]"),
    "qdrant": ("memory.backends.qdrant", "pip install feral-ai[memory-qdrant]"),
}


def _settings_path() -> Path:
    return feral_home() / "settings.json"


def _load_settings() -> dict:
    path = _settings_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        print(f"  Could not read {path}: {exc}")
        return {}


def _save_settings(settings: dict) -> None:
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2))


def _is_installed(module_path: str) -> bool:
    try:
        importlib.import_module(module_path)
        return True
    except ImportError:
        return False


def _current_backend() -> str:
    settings = _load_settings()
    return (settings.get("memory") or {}).get("backend", "sqlite_vec")


def _http_request(method: str, path: str) -> dict:
    """Call the running brain over HTTP. Imported lazily so the CLI
    keeps booting when the brain is offline (status / list shouldn't
    need a live brain to work)."""
    from urllib.request import Request, urlopen
    from urllib.error import URLError, HTTPError
    from config.runtime import brain_port

    url = f"http://127.0.0.1:{brain_port()}{path}"
    req = Request(url, method=method)
    try:
        with urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        return json.loads(body)
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", errors="replace"))
        except Exception:
            detail = {"detail": str(exc)}
        return {"ok": False, "status": exc.code, **detail}
    except URLError as exc:
        return {
            "ok": False,
            "error": (
                f"Could not reach brain at {url}: {exc.reason}. "
                "Is `feral start` running?"
            ),
        }


def _brain_health_ok(timeout: float = 1.0) -> bool:
    """Probe ``http://localhost:9090/health`` to detect a running brain.

    Returns True iff the endpoint returns a 200 within ``timeout``
    seconds. Any other outcome (connection refused, timeout, non-200)
    counts as "brain not running" — the worst case is we tell the
    operator to stop something they've already stopped.
    """
    from urllib.request import Request, urlopen
    from urllib.error import URLError, HTTPError

    try:
        from config.runtime import brain_port
        port = brain_port()
    except Exception:
        port = 9090
    url = f"http://localhost:{port}/health"
    try:
        with urlopen(Request(url, method="GET"), timeout=timeout) as resp:
            return resp.status == 200
    except (HTTPError, URLError, OSError, TimeoutError):
        return False


def cmd_memory(action: str, backend_id: str | None, *, flags=None) -> None:
    # Lane 07  — `feral memory query <text>` closes THESIS_SCENARIOS
    # S1 from the CLI side. The argparse positional ``backend_id`` is
    # repurposed as the query string for ``query``; main.py forwards
    # everything after ``feral memory query`` as ``args.backend_id``.
    if action == "query":
        if not backend_id:
            print("  Usage: feral memory query \"<text>\"")
            sys.exit(2)
        from urllib.parse import quote
        path = f"/internal/memory/search?query={quote(backend_id)}&limit=20"
        result = _http_request("GET", path)
        if isinstance(result, dict) and result.get("ok") is False:
            err = result.get("error") or result.get("detail") or result
            print(f"  Memory query failed: {err}")
            sys.exit(1)

        rows = result if isinstance(result, list) else []
        if not rows:
            print(f"  No memory hits for {backend_id!r}.")
            return
        print(f"  {len(rows)} hit(s) for {backend_id!r}:")
        for i, row in enumerate(rows, start=1):
            content = row.get("content") or row.get("text") or ""
            ts = row.get("created_at") or row.get("timestamp") or ""
            tags = row.get("tags") or []
            score = row.get("score")
            score_str = f" score={score:.3f}" if isinstance(score, (int, float)) else ""
            preview = content.strip().replace("\n", " ")
            if len(preview) > 200:
                preview = preview[:200] + "…"
            print()
            print(f"  {i}. [{ts}{score_str}]  tags={','.join(tags) or '-'}")
            print(f"     {preview}")
        return

    if action == "decay":
        if backend_id != "now":
            print("  Usage: feral memory decay now")
            sys.exit(2)
        result = _http_request("POST", "/api/memory/decay/now")
        if not result.get("ok"):
            print(f"  Decay sweep failed: {result.get('error') or result.get('detail') or result}")
            sys.exit(1)
        print(
            "  Decay sweep complete: "
            f"scanned={result.get('scanned', 0)} "
            f"updated={result.get('updated', 0)} "
            f"newly_forgotten={result.get('newly_forgotten', 0)} "
            f"hard_deleted={result.get('hard_deleted', 0)} "
            f"duration={result.get('duration_seconds', 0):.3f}s"
        )
        return

    if action == "forget":
        if not backend_id:
            print("  Usage: feral memory forget <episode_id>")
            sys.exit(2)
        result = _http_request("POST", f"/api/memory/forget/{backend_id}")
        if not result.get("ok"):
            print(f"  Could not forget {backend_id}: {result.get('detail') or result.get('error') or result}")
            sys.exit(1)
        print(f"  Episode {backend_id} forgotten at {result.get('forgotten_at')}.")
        return

    if action == "recall":
        if not backend_id:
            print("  Usage: feral memory recall <episode_id>")
            sys.exit(2)
        result = _http_request("POST", f"/api/memory/recall/{backend_id}")
        if not result.get("ok"):
            print(f"  Could not recall {backend_id}: {result.get('detail') or result.get('error') or result}")
            sys.exit(1)
        print(f"  Episode {backend_id} recalled (forgotten_at cleared).")
        return

    if action == "compact":
        # F2 — promote conversation turns to episodes. Hits the
        # /api/memory/compact endpoint (built in chunk F2 below).
        # Optional positional ``backend_id`` doubles as the
        # ``session_id`` filter.
        path = "/api/memory/compact"
        if backend_id:
            from urllib.parse import quote
            path += f"?session_id={quote(backend_id)}"
        result = _http_request("POST", path)
        if "error" in result and not result.get("results"):
            print(f"  Compaction failed: {result.get('error') or result.get('detail') or result}")
            sys.exit(1)
        results = result.get("results", []) or []
        compacted = [r for r in results if r.get("compacted")]
        print(f"  Compaction complete: sessions_scanned={len(results)} sessions_compacted={len(compacted)}")
        for r in compacted:
            sid = r.get("session_id", "?")
            episode_id = r.get("episode_id") or "(none)"
            ents = r.get("key_entities", []) or []
            print(
                f"    - session={sid} episode={episode_id} "
                f"turns={r.get('original_length', 0)}→{r.get('new_length', 0)} "
                f"entities={','.join(ents[:5]) or '-'}"
            )
        return

    if action == "encrypt":
        if _brain_health_ok():
            print(
                "  Refusing to encrypt while the brain is running. "
                "Stop the brain first (feral stop or pkill -f 'feral serve')."
            )
            sys.exit(1)

        from config.loader import feral_data_home
        from security.vault import get_vault
        from memory.at_rest import encrypt_memory_db

        force = bool(getattr(flags, "force", False))
        no_shred = bool(getattr(flags, "no_shred", False))
        db_path = feral_data_home() / "memory.db"

        try:
            vault = get_vault()
        except Exception as exc:
            print(f"  Vault could not be unlocked: {exc}")
            print(
                "  Run `feral key recover` and supply the recovery code "
                "printed at first boot, then retry."
            )
            sys.exit(1)

        try:
            result = encrypt_memory_db(
                vault=vault,
                db_path=db_path,
                shred_plaintext=not no_shred,
                force=force,
            )
        except FileExistsError as exc:
            print(f"  {exc}")
            sys.exit(1)
        except FileNotFoundError as exc:
            print(f"  {exc}")
            sys.exit(1)
        except Exception as exc:
            print(f"  Encrypt failed: {exc}")
            sys.exit(1)

        print(
            f"  Encrypted {result['ciphertext_path']} "
            f"({result['bytes']} bytes)"
        )
        if result.get("backup_path"):
            print(
                f"  Plaintext backup retained at {result['backup_path']} — "
                f"delete manually when verified."
            )

        settings = _load_settings()
        mem = settings.setdefault("memory", {})
        mem["encrypted_at_rest"] = True
        _save_settings(settings)
        print("  settings.json::memory.encrypted_at_rest = true")
        return

    if action == "reembed":
        """Rewrite stored embeddings with the currently configured provider.

        Needed when the embedder changes underneath existing data. The
        vectors written by the old provider have a different dimension,
        so every vector query raises and hybrid search silently degrades
        to keyword-only. Installing fastembed does exactly this: it wins
        provider selection, and every chunk written before it is
        orphaned.

        Every table is covered, not just ``memory_chunks``. This command
        used to inline a single-table loop, and that is how the knowledge
        graph died on a real install: chunks were migrated to 384 dims and
        ``entities`` was left at 1536, so every entity query raised. The
        table list, the discovery pass that catches a table the list has
        not been taught about, and the batching all live in
        ``memory/reembed.py`` now.

        ``feral memory reembed check`` reports without writing.
        """
        from config.loader import feral_data_home
        from memory.reembed import DegradedProviderError, reembed_store

        db = feral_data_home() / "memory.db"
        if not db.exists():
            print(f"  No memory database at {db}")
            sys.exit(1)

        dry_run = (backend_id or "").strip().lower() in ("check", "--check", "dry-run")

        try:
            report = reembed_store(
                str(db), dry_run=dry_run, on_progress=lambda msg: print(msg),
            )
        except DegradedProviderError as exc:
            print(f"  Refusing to re-embed: {exc}")
            sys.exit(1)

        scan = report["scan"]
        print(f"  Provider: {report['provider']}  ({report['target_dim']}d)")
        # This is the state the run STARTED from. Printed after the
        # progress lines because the scan is part of the report, and
        # labelled so nobody reads "stale" as the outcome.
        print("  Stored vectors before this run:")
        for col in scan.columns:
            widths = ", ".join(
                f"{dim}d x{count}" for dim, count in sorted(col.widths.items())
            ) or "none"
            flag = "" if col.healthy else "   <- stale"
            known = "" if col.registered else "   <- NOT COVERED by this migration"
            print(f"    {col.table}.{col.column}: {widths}{flag}{known}")
        for vec in scan.vec0:
            state = "ok" if vec.matches else "stale, will be rebuilt"
            print(f"    {vec.table} (vec0 index): {vec.declared_dim}d  [{state}]")

        if dry_run:
            print(
                "  Dry run; nothing written. "
                + (
                    "Run `feral memory reembed` to migrate."
                    if scan.stale_total
                    else "Every stored vector already matches the active provider."
                )
            )
            return

        if not report["tables"] and not report["vec0"]:
            print("  Nothing to do; every stored vector matches the active provider.")
            return

        for entry in report["tables"]:
            if entry["stale"]:
                print(
                    f"  {entry['table']}: re-embedded {entry['updated']}, "
                    f"cleared {entry['nulled']} with no source text"
                )

        if not report["ok"]:
            # Exit non-zero rather than print a success line over a store
            # that is still broken. A migration that reports success while
            # a table stays stale is the exact failure mode being fixed.
            left = report.get("scan_after") or scan
            for col in left.columns:
                if col.stale:
                    print(
                        f"  STILL STALE: {col.table}.{col.column} has {col.stale} "
                        f"vector(s) at the wrong width"
                        + ("" if col.registered else " and is not in the migration registry")
                    )
            sys.exit(1)

        print("  Every stored vector now matches the active provider.")
        print("  Restart the brain to pick it up.")
        return

    if action == "status":
        active = _current_backend()
        module_path, install_hint = _KNOWN_BACKENDS.get(
            active, ("external", "registry-installed")
        )
        installed = _is_installed(module_path) if module_path != "external" else True
        print(f"  Active memory backend: {active}")
        print(f"  Module:                {module_path}")
        print(f"  Installed:             {'yes' if installed else 'no'}")
        if not installed:
            print(f"  Install:               {install_hint}")

        from memory.at_rest import encryption_status as _enc_status
        from config.loader import feral_data_home
        enc = _enc_status(feral_data_home() / "memory.db")
        print(f"  Encrypted at rest:     {'yes' if enc['encrypted_at_rest'] else 'no'}")
        if enc["encrypted_at_rest"]:
            print(f"  Ciphertext:            {enc['ciphertext_path']}")
        if enc.get("backup_path"):
            print(f"  Plaintext backup:      {enc['backup_path']}")
        return

    if action == "list":
        active = _current_backend()
        print("  Known memory backends:")
        for name, (module_path, install_hint) in _KNOWN_BACKENDS.items():
            mark = "*" if name == active else " "
            state = "installed" if _is_installed(module_path) else install_hint
            print(f"   {mark} {name:<12} {state}")
        return

    if action == "switch":
        if not backend_id:
            print("  Usage: feral memory switch <backend_id>")
            sys.exit(2)
        if backend_id not in _KNOWN_BACKENDS:
            print(f"  Unknown backend '{backend_id}'.")
            print("  Known: " + ", ".join(_KNOWN_BACKENDS.keys()))
            print("  For a community backend, run `feral install <registry_item_id>` first.")
            sys.exit(1)
        module_path, install_hint = _KNOWN_BACKENDS[backend_id]
        if not _is_installed(module_path):
            print(f"  Backend '{backend_id}' is not installed.")
            print(f"  Run:  {install_hint}")
            sys.exit(1)
        settings = _load_settings()
        mem = settings.setdefault("memory", {})
        mem["backend"] = backend_id
        _save_settings(settings)
        print(f"  Memory backend set to '{backend_id}'.")
        print("  Restart `feral start` for the change to take effect.")
        return

    print(f"  Unknown memory action: {action}")
    sys.exit(2)
