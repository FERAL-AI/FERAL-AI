# feral-registry

The **community marketplace** behind [`registry.feral.sh`](https://registry.feral.sh).
A small FastAPI service that lets anyone publish, browse, download, and flag Feral
artifacts — *skills*, *daemons*, and *MCP servers* — with cryptographic provenance.

---

## Trust model

feral-registry is intentionally minimal. It does **not** try to sandbox code. It
only guarantees **who** signed a given bundle and **what bytes** were uploaded.

- Every publisher authenticates with **GitHub OAuth** and is identified by their
  GitHub handle (`github_login`, globally unique).
- On first publish, a publisher registers an **Ed25519 public key** via
  `POST /api/v1/auth/github/register_pubkey`. The private key lives on the
  publisher's machine; the registry never sees it.
- `POST /api/v1/publish` requires a **detached Ed25519 signature** over the
  hex-encoded `sha256` of the bundle. If the signature does not verify against
  the publisher's registered pubkey, the upload is rejected.
- The signature is stored alongside the item so clients can **re-verify on
  download**. The pubkey is returned from `GET /api/v1/item/{id}`.
- A small allow-list of GitHub handles (`FEATURED_PUBLISHERS` env var) is marked
  `verified: true` in the catalog. Everyone else starts unverified. "Verified"
  is **not** a security claim — it is an editorial "we recognize this
  publisher" badge. Signature verification is the real trust boundary.
- Anyone can `POST /api/v1/flag/{id}` with a reason; flags are stored for human
  moderation.

### Bundle format

A bundle is a single tarball (`.tar.gz`) that contains:

- `manifest.json` at the root: the manifest FERAL loads at install time (for
  `kind=skill`, a `SkillManifest` as documented in `feral-core`),
- the implementation files referenced by the manifest, beside it
  (`impl.py` for a skill).

**Two manifests, and they are different documents.** The `manifest_json` form
field is *registry metadata*: `kind`, `name`, `version`, description, author.
It is what the catalog and `GET /item/{ref}` serve, and it is not covered by
the bundle signature. The tarball's own `manifest.json` is what the brain
loads. Writing the metadata envelope into the tarball in place of the real
manifest is a real bug that shipped: every published skill then failed at
install with `brand field required`, because the envelope has no `brand`.

The registry validates both. The form field is checked for the per-kind shape
(see `schemas.py::validate_manifest_for_kind`). For `kind=skill` the tarball is
opened and its `manifest.json` must parse and carry a non-empty `skill_id`,
`brand.name`, and `description` — the keys `SkillManifest` declares with no
default, plus the stable id that makes the skill nameable. Publish is allowed
to be stricter than install; it is being *looser* that produced a green upload
and a red install.

`feral_registry/bundle.py` deliberately does not re-implement feral-core's
`SkillPermission` vocabulary: that enum grows in feral-core, the registry does
not import feral-core, and a stale copy here would reject a valid new skill at
upload. Structure is checked, vocabulary is not.

---

## Routes

All endpoints live under `/api/v1`.

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET    | `/healthz` | liveness probe |
| GET    | `/auth/github/login` | redirect to GitHub OAuth consent |
| GET    | `/auth/github/callback?code=...` | exchange code, issue `publisher_token` (HS256 JWT, 30-day TTL) |
| POST   | `/auth/github/register_pubkey` | register Ed25519 pubkey (hex) for the authenticated publisher |
| POST   | `/publish` | multipart upload: `bundle`, `signature`, `manifest_json`. Requires `Authorization: Bearer <publisher_token>`. |
| GET    | `/catalog?kind=&q=&sort=newest\|popular` | list items |
| GET    | `/item/{ref}?kind=&version=` | item detail including download URL, pubkey, and signature. `ref` is the item's UUID **or** its `name` (see below). |
| POST   | `/flag/{id}` | community moderation flag |
| GET    | `/blobs/{sha256}` | serve a bundle blob; increments `downloads` |

Every route returns typed Pydantic models.

### Resolving `/item/{ref}`

`ref` is what a caller actually holds. Every one of them is a name: `feral
install robot_ext`, an `AppManifest` declaring `skill_dependencies:
["robot_ext"]`, the `name` column in the catalog. Nobody types the UUID, and a
manifest pinning dependencies by UUID would be unreadable, so the endpoint
resolves both. `UniqueConstraint("kind", "name", "version")` on `items` is what
makes a name a legal key.

Order and rules (implemented once, in `feral_registry/resolve.py`):

1. **By `id` first.** An exact primary-key hit wins, so a published item's id
   stays a permalink that a later item's `name` cannot shadow.
2. **By `name`**, over exactly the rows the caller may see. Anonymous callers
   resolve against `approved` + `public` only, so a name cannot confirm that a
   pending or rejected submission exists. Reviewers resolve against everything.
3. Several rows under one name:
   - **different kinds** -> `409`, naming the kinds. No rule chooses between a
     skill and a daemon called `robot_ext`. Pass `?kind=`.
   - **different versions, one kind** -> the **highest** version, compared as
     dot-separated integers (`0.10.0` > `0.9.0`). This is not a guess; it is
     what `feral install robot_ext` means.
   - **versions that cannot be ordered** (`1.0.0` beside `1.0.0-beta`) ->
     `409`, naming them. Picking one would be arbitrary. Pass `?version=`.
4. Nothing visible matches -> `404 item not found`, the same answer a hidden
   row gives.

If `GITHUB_CLIENT_ID` is unset, the `/auth/github/*` endpoints return
**501 Not Implemented** with `{"detail": "not configured"}` — the rest of the
service still works for local dev.

---

## Local dev

```bash
cd ASOS/feral-registry

python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env          # then edit as needed

alembic upgrade head          # creates tables (SQLite by default)

uvicorn feral_registry.main:app --reload
```

Open <http://localhost:8080/docs> for the OpenAPI UI.

### Running the test suite

```bash
pytest
```

The included end-to-end test (`tests/test_publish_flow.py`) generates an
Ed25519 keypair in-memory, registers a fake publisher, publishes a tiny signed
tarball, and asserts that the catalog surfaces it.

---

## Environment variables

| Var | Default | Purpose |
| --- | ------- | ------- |
| `FERAL_REGISTRY_DB_URL` | `sqlite+aiosqlite:///./registry.db` | SQLAlchemy async URL. Use `postgresql+asyncpg://...` in prod. |
| `FERAL_REGISTRY_BLOB_DIR` | `./_blobs` | Directory where `.tar.gz` bundles are written. |
| `FERAL_REGISTRY_PUBLIC_URL` | `http://localhost:8080` | Base URL used to build `download_url`s. |
| `GITHUB_CLIENT_ID` | *(unset)* | OAuth app client ID. When unset, auth routes return 501. |
| `GITHUB_CLIENT_SECRET` | *(unset)* | OAuth app client secret. |
| `GITHUB_REDIRECT_URI` | `http://localhost:8080/api/v1/auth/github/callback` | Must match the OAuth app config. |
| `JWT_SECRET` | `dev-insecure-change-me` | HS256 signing secret for publisher tokens. **Set a real one in prod.** |
| `FEATURED_PUBLISHERS` | *(empty)* | Comma-separated GitHub handles whose items get `verified: true`. |

### Creating a GitHub OAuth app

1. <https://github.com/settings/developers> → **New OAuth App**.
2. Homepage URL: `https://registry.feral.sh` (or your dev URL).
3. Authorization callback URL: must match `GITHUB_REDIRECT_URI`.
4. Generate a client secret and set both values via `fly secrets set ...` (see
   below) or in your local `.env`.

---

## Deploying to Fly.io

```bash
fly launch --no-deploy            # accept the generated app name "feral-registry"
fly volumes create feral_registry_data --region iad --size 1

fly secrets set \
  GITHUB_CLIENT_ID=xxx \
  GITHUB_CLIENT_SECRET=xxx \
  GITHUB_REDIRECT_URI=https://registry.feral.sh/api/v1/auth/github/callback \
  JWT_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')" \
  FEATURED_PUBLISHERS=feral,theora

fly deploy

fly ssh console -C "alembic upgrade head"
fly ssh console -C "python -m scripts.seed_first_party"   # optional; see below
```

`fly.toml` mounts a 1 GB volume at `/data` for the SQLite database and the
blob directory. To move to Postgres, set `FERAL_REGISTRY_DB_URL` to an
`asyncpg` URL and rerun `alembic upgrade head`.

The `Procfile` is provided for platforms that prefer buildpacks (Render,
Railway, Heroku-style).

---

## Publishing a bundle (client side)

Use the `feral` CLI from `ASOS/feral-client`:

```bash
feral login                     # opens GitHub OAuth, stores publisher_token locally
feral keygen                    # generates Ed25519 keypair, uploads pubkey
feral publish --skill ./my_skill/
```

Under the hood the client:

1. Builds `my_skill.tar.gz` from the skill directory.
2. Computes `sha256(bundle)` and signs the hex digest with the local Ed25519
   private key.
3. `POST`s `bundle`, `signature`, and `manifest_json` to
   `POST /api/v1/publish` with the stored `publisher_token`.

Daemons (`feral publish --daemon ./my_daemon/`) and MCP servers
(`feral publish --mcp ./my_mcp/`) use the exact same endpoint.

---

## Seeding first-party items

After the first deploy you will want the official Feral skills and daemons to
show up as `verified` items in the catalog. Run:

```bash
fly ssh console -C "python -m scripts.seed_first_party"
```

The script reads `ASOS/feral-core/skills/manifests/*.json`, builds matching
tarballs from the referenced implementations, signs them with a `first_party`
seed key, and inserts them as verified items under the `feral` publisher. It
also seeds the `w300_daemon` and `wristband_daemon` entries from
`ASOS/feral-nodes/`.

The seed script is **idempotent** — running it twice skips already-present
`(kind, name, version)` tuples.
