# Upstream Sync Guide (Have-the-best-of-both)

This fork extends the original misaka_danmu_server with Plus features (external control API, invite-based registration, rate limiting and usage aggregation, token governance, trusted proxies/real client IP parsing, etc.).

To keep both upstream improvements (scraper fixes, metadata adapters, stability/perf, deployment updates) and the Plus features, use the provided helper script to periodically merge upstream changes while preserving Plus-specific files.

## TL;DR

1. Ensure your working tree is clean (commit or stash first).
2. Run:

   bash scripts/upstream_sync.sh

   Optionally specify branch (defaults to main):

   bash scripts/upstream_sync.sh main

3. The script will:
   - Add the upstream remote if missing: https://github.com/l429609201/misaka_danmu_server.git
   - Create a branch merge/upstream-YYYYmmdd-HHMMSS
   - Temporarily configure .gitattributes to keep "our" versions of Plus-specific files
   - Merge upstream/main preferring upstream for conflicting files (except the protected Plus files)
   - Clean up temporary .gitattributes rules

4. Review and test the merged branch, then merge it back into your working branch.

## What is protected (kept from Plus by default)

- Backend Plus features and integration glue:
  - src/api/control_api.py
  - src/api/invite_api.py
  - src/rate_limiter.py
  - src/rate_limit/*
  - src/config_manager.py
  - src/main.py
  - src/orm_models.py
  - src/crud.py
  - src/models.py
- Frontend Plus pages and API bridge:
  - web/src/pages/register/**
  - web/src/pages/setting/components/Invites.jsx
  - web/src/pages/setting/components/Accounts.jsx
  - web/src/apis/index.js

These files encode rate-limiting, invite system, external control API, UI integration, and related data models.

## Why this approach

- Upstream evolves rapidly (scrapers, metadata, fixes). Pulling their changes ensures compatibility and stability.
- Plus features are opinionated and often span multiple backend/frontend files. Protecting them ensures they remain intact.
- You still get upstream improvements everywhere else (e.g., scrapers, jobs, metadata, deployment files).

## After merging

- Run your test/deploy routine.
- Check diffs in scrapers and metadata sources; if upstream introduced new providers or settings, expose them in the UI if needed.
- For any conflicts beyond the protected files, decide case-by-case whether to take upstream or keep local changes.

## Notes

- The script uses a temporary .gitattributes block and the built-in `merge.ours` driver for protected files, and otherwise prefers upstream (`-X theirs`).
- If you need to add/remove protected files, edit scripts/upstream_sync.sh accordingly.
- If you maintain long-lived feature branches, repeat this process periodically to keep up with upstream.
