# EngageMate

A forkable, self-hosted Instagram Story engagement engine. Run it on your own computer or VM with your own Hermes or Codex account. EngageMate has no hosted signup, paid plans, checkout, telemetry or dependency on the original author's infrastructure.

It supports approved prospect sourcing, Story views/likes/replies, optional follow/unfollow, account-scoped encrypted sessions, warm-up, pacing, challenge stops and durable action receipts. Execution is disabled until you configure and validate your own installation.

**Start with [the self-hosting guide](docs/self-hosting.md).** It covers local/VM setup, generated owner keys, PostgreSQL migrations, your own runtime, proxies and bounded canaries. This is a community fork-and-run project under the MIT license; there is no hosted service or managed support offering.

```text
Local owner browser + Chrome extension
          |
Next.js workspace (private owner session)
          |
FastAPI control API -> your PostgreSQL <- scheduler
          |
Account worker + loopback execution MCP
          |
Your Hermes gateway OR the included stock Codex CLI adapter
```

| Path | Contents |
| --- | --- |
| `app/` | Next.js web app, owner setup, Drizzle migrations and web tests |
| `api/engagemate/` | Control API, scheduler, worker, reports and runtime adapters |
| `extension/` | Chrome session capture extension; localhost only by default |
| `deploy/` | Empty environment templates, healthcheck.py and owner Hermes profile |
| `scripts/setup.py` | Generates fresh private configuration for your fork |
| `scripts/run_service.py` | Starts local services using scoped configuration |
| `tests/` | Backend and deployment-contract tests |

## Quick start

Use Python 3.12, Node 22, PostgreSQL 16 and Chrome. Complete your own runtime login first.

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r api/requirements.txt
npm --prefix app ci
python scripts/setup.py --runtime codex  # or hermes
```

Continue with the guide to start PostgreSQL, apply migrations and run services. Open `http://localhost:3030/setup` with the generated owner key. For a VM, use an SSH tunnel. All service ports stay private.

Load `extension/` as an unpacked extension in Chrome. To package for an origin you control:

```sh
ENGAGEMATE_WEB_ORIGIN=https://app.example.com python3 scripts/package_extension.py
```

The packager rejects credentials, wildcard origins and insecure non-loopback HTTP. Nothing trusts a public domain by default. Optional web origin, MCP OAuth URLs, email, proxy and retrieval providers must belong to you.

## Validation

```sh
pip install -r api/requirements-test.txt
PGCLIENTENCODING=UTF8 python -m pytest -q
npm --prefix app test
npm --prefix app run lint
npm --prefix app run build
cd app && npx tsc --noEmit
```

Database tests create throwaway databases using `PGHOST`/`PGPORT` and skip if PostgreSQL is unavailable. Never point tests at a database containing real account data. Run preflight, execution fixtures and bounded account-owned canaries before enabling Instagram actions.

## Privacy

Only blank templates and synthetic fixtures belong in this repository. Owner access keys, agent authentication, Instagram sessions, browser profiles, screenshots, database backups and proxy credentials stay in ignored files on your machine. Read the [connection privacy disclosure](app/src/app/(marketing)/privacy/page.tsx) and [self-hosting guide](docs/self-hosting.md) before connecting an account.

## License

MIT. See [LICENSE](LICENSE).
