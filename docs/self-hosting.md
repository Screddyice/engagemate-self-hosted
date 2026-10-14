# Run your own EngageMate

EngageMate is a single-owner application. Fork this repository and run the web app, PostgreSQL, API, worker and agent runtime on hardware you control. There is no hosted EngageMate account, subscription, analytics collector or shared agent service. You pay your own runtime, VM and optional proxy/provider costs.

## 1. Prepare your machine

Use Python 3.12, Node 22, PostgreSQL 16 and Chrome. Docker is optional for the database. Linux is recommended for a VM. Keep the app, control API, agent gateway and execution MCP bound to loopback; reach a VM with `ssh -L 3030:127.0.0.1:3030 your-vm`. Do not publish the ports to the internet.

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r api/requirements.txt
cd app
npm ci
cd ..
python scripts/setup.py --runtime codex  # or --runtime hermes
```

Setup packages the localhost extension, generates fresh keys and a database password with file permissions 0600. It refuses to overwrite existing configuration. Keep these files private. The owner key stays on your machine; read `OWNER_ACCESS_TOKEN` in `app/.env.local` privately when opening `/setup`.

## 2. Connect YOUR runtime

Run the runtime under your own OS user on the same machine as the worker. Never use the original author's cloud, agent profile, credentials or account. Requests go through a private gateway; your chosen model provider still receives model inputs under your own account.

### Codex

Install the stock [Codex CLI](https://learn.chatgpt.com/docs/cli), then authenticate using `codex login` on this machine. Your installed version must support `codex exec --ephemeral --ignore-user-config` (check `codex exec --help`). Set an optional model available to your account in `deploy/.env.agent`; no author's model or account is selected for you.

```sh
python scripts/run_service.py codex
```

The gateway listens on `127.0.0.1:8657`. Each request starts an ephemeral, read-only Codex run in an empty temporary directory. It ignores the user's other MCP connections, disables shell and web search, and attaches only the authenticated EngageMate execution MCP to scheduled jobs. It cleans up temporary images and kills timed-out processes. The stock CLI's account authentication remains local. This adapter implements the existing worker's `HERMES_AGENT_*` wire protocol; choosing Codex does not require installing Hermes.

### Hermes

Install [Hermes Agent](https://github.com/NousResearch/hermes-agent) on your own machine/VM. Create an isolated profile:

```sh
hermes profile create engagemate-agent
```

Copy the supplied profile's `config.yaml`, `SOUL.md`, `plugins/` and generated private `.env` into the new profile directory reported by Hermes. Then configure your own provider login and model using `hermes -p engagemate-agent setup`; replace the two model placeholders in the profile. Confirm the profile still exposes only `mcp-engagemate_execution` and keeps memory disabled. The generated `.env` has a fresh `API_SERVER_KEY`, the matching execution MCP key, loopback host and port 8657.

```sh
hermes -p engagemate-agent gateway
```

The [Hermes API server](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server) provides `/v1/chat/completions` and session deletion. The worker requires session-header matching and authenticated cleanup; test these before enabling real actions. If you choose another private port, update `HERMES_AGENT_URL` in both API and worker configuration. No original-author gateway is involved.

The same private runtime is used for brand/voice analysis. Optional HyperCrawl retrieval must be a service/account you provision; its URL and token are empty by default. The manual source-audience form in onboarding lets you approve inspiration accounts and write your voice without retrieval or model analysis.

## 3. Start your database and apply migrations

```sh
docker compose up -d db
cd app
npm run migrate
cd ..
```

The first 35 migrations support existing databases; migration 0035 removes SaaS metadata, renames the identity key, and keeps Instagram accounts/sessions. Back up an existing database before upgrading. An existing database does not automatically grant the new owner access to legacy identities; start with a fresh database for a fork.

## 4. Start native services

Run each command in a terminal with the virtual environment activated:

```sh
python scripts/run_service.py api
python scripts/run_service.py scheduler
python scripts/run_service.py worker
npm run dev
```

On macOS, use native services for the worker and gateway; Docker host networking cannot reach your Mac's loopback agent. On Linux, optional Compose `web/api/scheduler/worker` services run on your own VM. The worker uses host networking for its private MCP connection; configure the gateway and API runtime endpoint to a private address reachable from their networks. Compose email and browser-login helpers are optional profiles (`email`, `browser-login`). No automatic deployment workflow exists.

Open `http://localhost:3030/setup`, enter your generated owner setup key, and complete Instagram connection, voice, approved targets and launch settings. The cookie expires after 30 days; use the same local key to reopen the workspace. Rotate the key to revoke browser sessions. Extension capture uses separate signed tickets and the API still requires its own internal key.

## 5. Configure proxies and validate before launch

Execution starts disabled. Configure your own per-account proxy inventory and matching fingerprint geography in the worker/API files. `IPROYAL_MODE=disabled` buys nothing; use operator-provisioned `import` or `inventory` mode to avoid automatic purchases. Managed proxy purchasing is an explicit opt-in with its own spending limits. Email transports are also optional and require your own sender/account configuration.

```sh
python -m pytest -q
cd app
npm test
npm run lint
npx tsc --noEmit
npm run build
```

Run the worker preflight (`deploy/healthcheck.py worker`) in the worker environment and the local execution fixtures before selecting canary/autonomous mode. Preserve warm-up, daily action limits, account ownership, challenge stops and no-retry rules for uncertain writes. Test on an account you own. Enabling the scheduler/runtime alone does not prove Instagram actions work; inspect durable receipts for each bounded canary.

## Keep your fork private where it matters

Never commit populated env files, database exports, auth state, cookies, screenshots, browser profiles, proxy credentials or runtime transcripts. `.gitignore` covers local configuration/artifacts; run `gitleaks git --redact` and inspect your diff before pushing. The repository ships only empty templates and synthetic fixtures. Dependency scanners and secret scanners do not prove the absence of every kind of private information.
