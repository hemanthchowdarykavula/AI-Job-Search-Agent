# Cloudflare production scheduler

This Worker is an external clock for the existing GitHub Actions production workflow in `hemanthchowdarykavula/AI-Job-Search-Agent`.

## Schedule
Cloudflare cron heartbeats wake at the configured UTC times. The Worker converts each scheduled timestamp to America/New_York and dispatches GitHub only during the 55-minute recovery windows for 07:30, 10:00, 12:30, 15:30, 18:30, and 21:00 ET.

The GitHub/Python pipeline keeps its existing `last_completed_slot` protection, so recovery dispatches do not intentionally process a completed slot twice.

## GitHub Actions endpoint
`cloudflare-scheduler.js` must dispatch to:

`https://api.github.com/repos/hemanthchowdarykavula/AI-Job-Search-Agent/actions/workflows/daily-discovery.yml/dispatches`

A GitHub source edit alone does **not** necessarily change the deployed Cloudflare Worker. Redeploy the Worker after updating this endpoint.

## Required Cloudflare secret
Set `GITHUB_DISPATCH_TOKEN` as a **Cloudflare Worker secret**. Use a fine-grained GitHub token with access to `hemanthchowdarykavula/AI-Job-Search-Agent` and **Actions: Read and write** repository permissions. Verify its access after renaming the GitHub owner; replace the token only if the existing token has lost access.

This token is separate from `OPENAI_API_KEY`, which is a GitHub Actions repository secret for resume generation. Never commit either token to this repository.

## Deploy
From the repository root in a terminal authenticated to the correct Cloudflare account:

```bash
npx wrangler deploy
```

If the Worker secret is missing or needs replacement:

```bash
npx wrangler secret put GITHUB_DISPATCH_TOKEN
```

Alternatively, in the Cloudflare dashboard, open **Workers & Pages** → **ai-job-search-scheduler**, update/deploy the code there, and check that the Worker still has the `GITHUB_DISPATCH_TOKEN` secret and the configured cron trigger.

The Worker is defined by `wrangler.toml`. Deploy to the same Cloudflare account/Worker name so existing settings are not unintentionally moved to a different Worker.

## Verify
1. Check Cloudflare Worker deployment status and recent invocation logs. A successful scheduled invocation should return without a GitHub dispatch error.
2. Check the [GitHub Actions production workflow](https://github.com/hemanthchowdarykavula/AI-Job-Search-Agent/actions/workflows/daily-discovery.yml) for new `workflow_dispatch` runs around the planned Eastern Time slots.
3. Inspect the `Run scheduled production cycle` step, not just the overall green workflow check. A successful heartbeat can skip discovery when no slot is due.
4. Verify resume generation separately: when an eligible posting exists, `OPENAI_API_KEY` must authenticate and produce validated DOCX/PDF artifacts before jobs reach `READY_TO_APPLY`.
