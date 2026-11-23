# Deploy to Rukkhadevata

**Code:** `scripts/deploy-rukkhadevata.ps1`
**Last verified:** 2026-11-22

This fork runs on the NAS Rukkhadevata (192.168.50.84). It moved there from
Nahida on 2026-11-22. The dashboard is http://192.168.50.84:8765 (LAN only,
no login, no proxy host).

## Stack on Rukkhadevata

- Folder: `/Volume1/apps/shelly-energy-analyzer` (listed in `/Volume1/apps/_ops/stacks.list`, so the boot script, the health guard and the nightly backup cover it).
- `compose.yaml`: one service, `network_mode: host` (mDNS discovery of the plugs), port 8765.
- `src/`: a `git archive` of the deployed commit. The image `shelly-energy-analyzer:<version>-<sha7>` is built from it on the NAS. It is not in a registry.
- `data/config.json`: settings, including secrets (`tou.urdb_api_key`, `plug_history.cloud_auth_key`). Do not commit it.
- `data/data/energy.db`: the SQLite database (WAL mode).
- `data/backup-pre-<tag>-<utc>/`: the copies that each deploy makes before it changes the stack.

Only one instance may poll the plugs. Do not start a second copy somewhere else.

## Deploy

Push the commit to `throndir2-plug-history`, then run:

```powershell
& "C:\Repositories\shelly-energy-analyzer\scripts\deploy-rukkhadevata.ps1"
```

The script:

1. Copies `git archive` of `origin/throndir2-plug-history` (or `-Ref`) to `src/`.
2. Builds `shelly-energy-analyzer:<version>-<sha7>` on the NAS.
3. Copies `config.json` and an online SQLite copy of `energy.db` to `data/backup-pre-*`.
4. Sets the image in `compose.yaml` and runs `docker compose up -d`.
5. Waits for `/api/version`. If it does not answer, it goes back to the previous image.

To go back to the previous image by hand:

```powershell
& "C:\Repositories\shelly-energy-analyzer\scripts\deploy-rukkhadevata.ps1" -Rollback
```

The script uses no GitHub Actions. A push to `throndir2-plug-history` starts no workflow.
