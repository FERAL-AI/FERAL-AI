# Upgrading the FERAL Home Assistant Add-on

## Standard Upgrade

1. Open **Settings → Add-ons → FERAL AI Brain** in your Home Assistant UI.
2. Click **Update** when a new version is available.
3. The add-on restarts automatically. Your configuration (`/config/feral/`) is preserved.

## Manual Upgrade (advanced)

```bash
# SSH into your HA host
ha addons update feral-brain

# Or rebuild from source:
cd /addons/feral-ha-addon
git pull
ha addons rebuild feral-brain
```

## Pinning a Version

Edit `Dockerfile` and set the `FERAL_VERSION` build arg:

```dockerfile
ARG FERAL_VERSION=2026.6.6
```

Then rebuild. The add-on will install exactly that pip version.

## Rollback

If an upgrade breaks something:

1. **Via UI:** Go to **Settings → Add-ons → FERAL AI Brain → Info** and look for a "Rollback" option (available on HAOS 12+).
2. **Via CLI:**
   ```bash
   ha addons install feral-brain --version 2026.3.1
   ```
3. **Via Dockerfile:** Set `FERAL_VERSION` to the previous known-good version and rebuild.

## Data Safety

- Your memory database, config, and vault are stored in `/config/feral/` and are **not** deleted on upgrade or uninstall.
- Scheduler jobs (SQLite) survive restarts.
- Always back up via **Settings → System → Backups** before major upgrades.
