# Deploying bullseyemedical.ai to Hostinger

The Claude Code cloud environment only allows outbound **HTTPS**, so it cannot
reach Hostinger over FTP/SFTP (ports 21/22 time out). Deploys therefore run on
**GitHub-hosted runners**, which have open egress. The terminal edits files in
`site/`; GitHub Actions pushes them live.

## One-time setup

1. **Add three repository secrets** — GitHub repo → Settings → Secrets and
   variables → Actions → *New repository secret*:
   - `HOSTINGER_SFTP_HOST` — `156.67.75.206`
   - `HOSTINGER_SFTP_USER` — the Hostinger FTP/SFTP username
   - `HOSTINGER_SFTP_PASSWORD` — the account password
2. **Get the workflows onto `main`.** GitHub only runs `workflow_dispatch`
   workflows that exist on the default branch.

## Bootstrap `site/` (run once)

Actions tab → **Hostinger Pull (bootstrap site/)** → *Run workflow*.

- Read-only on the server. It logs the remote directory tree (confirming which
  folder the account lands in) and mirrors the web root into `site/`, then
  commits the real files to the branch.
- If `sftp`/`22` fails to authenticate (some Hostinger sub-accounts are
  FTP-only), re-run with protocol `ftp`, port `21`.
- Check the log's directory listing. If the web root is a subfolder (e.g.
  `public_html/`), re-run with that `server_dir` (must end with `/`).

## Deploy changes

1. Edit files under `site/`.
2. **Dry-run first** (after any config change): Actions → **Hostinger Deploy**
   → *Run workflow* → mode `dry-run`. It connects and logs the diff but writes
   nothing.
3. When the dry-run looks right, either merge the change to `main` (auto-deploys
   on any `site/**` change) or run **Hostinger Deploy** with mode `deploy`.

## Safety

- `dangerous-clean-slate` is never enabled — the deploy never wipes the server.
  The first deploy uploads `site/` and leaves unknown remote files untouched.
- The destructive risk is overwriting a live page with a wrong local copy, which
  is why `site/` is bootstrapped from the real server files (Hostinger Pull),
  not from a guessed HTTPS mirror.

## Credentials

Local scripts (`upload_site.py`, `download_site.py`, etc.) read credentials from
the gitignored `pipeline-api/.env`. They use plain FTP (port 21) and only work
from a machine that can reach Hostinger directly (e.g. an operator laptop) — not
from the Claude Code cloud environment.
