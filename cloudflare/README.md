# FlowScanner on-time trigger (Cloudflare Worker)

GitHub's native `schedule` trigger can be delayed by hours, which caused
premarket scans to fire after the open (or, with the wall-clock guard, to skip
entirely). This Worker is the **primary** trigger: it calls GitHub's
`workflow_dispatch` API with the session already resolved, and in practice fires
within seconds of the scheduled minute.

**It is not guaranteed to fire, and it is not a substitute for the backup.**
Cloudflare publishes no execution guarantee for Cron Triggers — Workers run "on
underutilized machines to make the best use of Cloudflare's capacity". On
2026-07-17 the 13:00 UTC firing was skipped outright: zero invocations, no
error, no Cloudflare incident, crons still registered, Worker healthy — the day
simply had no scans. So the workflow's own `schedule` crons are a **required**
backup, not decoration: they cover the case where this Worker silently doesn't
run, and the workflow's guard means they no-op when it does. Do not remove them.

## One-time setup

### 1. GitHub fine-grained token

- GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate new
- Repository access: **Only select repositories** → `swagaholicsflowscanner7540`
- Permissions: **Actions → Read and write** (nothing else needed)
- Expiration: as long as you're comfortable (max 1 year — set a rotation reminder)
- Copy the token. It goes into Cloudflare as a secret, never into this repo.

### 2. Deploy the Worker

Install wrangler if needed, then from this `cloudflare/` directory:

```bash
npm install -g wrangler        # or: npm i -D wrangler
wrangler login                 # opens browser, authorizes your CF account
wrangler secret put GH_TOKEN   # paste the fine-grained token when prompted
wrangler deploy
```

`wrangler deploy` registers the cron triggers from `wrangler.toml` automatically.

### Dashboard alternative (no CLI)

1. Cloudflare dashboard → Workers & Pages → Create → Worker. Paste `worker.js`.
2. Settings → Variables → **Secret** `GH_TOKEN` = your fine-grained token.
3. Settings → Triggers → Cron Triggers → add each line from `wrangler.toml`.

## Verify

- Trigger once manually: `wrangler dev` then hit the scheduled event, or wait for
  the next cron. Tail logs with `wrangler tail`.
- A successful firing logs `Dispatched premarket scan` / `Dispatched pulse scan`;
  the off-DST twin logs `not a session hour — skipping`.
- Confirm a new run appears under the repo's Actions tab with event
  `workflow_dispatch`, and that it runs the full scan (minutes, not seconds).

## How sessions are decided

`worker.js` reads the true US-Eastern hour (DST-aware via `Intl`) at fire time —
reliable because Cloudflare fires on time — and dispatches `premarket` at 09:00 ET
or `pulse` at 14:00 ET. GitHub receives `session_type` explicitly and trusts it;
see the `Determine session type` step in `.github/workflows/scan.yml`.
