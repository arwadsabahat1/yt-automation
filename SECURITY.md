# Account and source privacy

Only reusable code, tests, empty templates and redistributable fonts belong in the repository. Keep `.env`, `config.yaml`, OAuth clients, tokens, upload-session URLs, personal email addresses, channel IDs, actual video links, source recordings, model weights, reports and databases local. The entire `data/` tree and internal plans/notes are excluded.

The review/analytics servers bind to `127.0.0.1`. The review UI changes local approval state and serves working files; do not expose it through a tunnel/shared host without appropriate authentication and access controls.

Upload and read-only Analytics credentials use separate private token files. The uploader checks expected channel and approved fingerprints before writes. Do not bypass these checks. Restrict API keys appropriately, review OAuth scopes and revoke unused connections in your Google account.

Research/LLM/stock integrations make external requests. Explicit upload sends media to YouTube; explicit Telegram delivery sends a report to its destination. Only send information and recordings you are authorized to share.

Before sharing changes:

```powershell
git diff --cached --stat
python scripts/check_source_privacy.py --tracked
git config core.hooksPath .githooks
```

The pre-commit guard checks staged paths and common private-data signatures. It cannot guarantee detection of arbitrary sensitive prose; review every added file. If a credential leaks, revoke/rotate it first: deleting the latest file does not erase history. Never paste a leaked value into a public issue; describe the type and affected path privately.

Use GitHub's `noreply` commit email to keep personal email out of commits. Bundled font licenses cover those fonts only; they do not grant rights to external footage, music or models.
