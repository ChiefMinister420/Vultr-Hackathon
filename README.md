# Slipstream

**A permission slip before every agent run.** Slipstream turns a task into a bounded manifest, waits for approval, executes the work in an ephemeral container, and reports what it used and what was denied.

> Hackathon MVP: the included live demo handles CSV analysis. It uses a real Docker container with hard resource limits when Docker is available. Vultr Serverless Inference can draft the manifest; deterministic policy checks always run after it. A local mode is provided for development, but it is visibly labeled and is not a security boundary.

## Run locally

Requirements: Python 3.11+ and Docker Desktop/Engine.

```sh
# 1. Create and activate a virtualenv
python3 -m venv .venv
source .venv/bin/activate

# 2. Install the project
pip install -e '.[dev]'

# 3. Activate the git hooks
pre-commit install

# 4. Configure environment
cp .env.example .env

# 5. Build the sandbox runner image
docker build -t slipstream-runner:local -f sandbox/Dockerfile .

# 6. Build the network proxy image (used only by runs with allowed network domains)
docker build -t slipstream-proxy:local -f proxy/Dockerfile .

# 7. Start the server
python3 -m app.server
```

On Linux, make sure your user can reach the Docker socket without `sudo` — `docker info`
should succeed unprivileged. If it only works under `sudo`, add yourself to the `docker`
group and start a fresh shell:

```sh
sudo usermod -aG docker "$USER"
newgrp docker
```

Open http://localhost:8080. Try “Analyze the sample sales CSV and show the top 5 products by revenue.” Review and approve the permission card, then watch the execution events and report. The sample dataset lives at `/workspace/input/sales.csv` inside the runner.

### Planner provider

The planner calls an OpenAI-compatible chat API through `app/llm.py`. `LLM_PROVIDER` selects it:

- `vultr` — Vultr Serverless Inference, the submission provider. Uses `VULTR_INFERENCE_API_KEY` and `VULTR_INFERENCE_MODEL`.
- `groq` — local development only. Uses `GROQ_API_KEY`; default model `openai/gpt-oss-120b`. The UI labels its plans "DEV ONLY".

Left empty, it picks `vultr` if a Vultr key is set, else `groq` if a Groq key is set, else the built-in deterministic planner. `LLM_MODEL` overrides the model. Any planner error falls back to the built-in planner.

For a submission or production deployment, set `REQUIRE_VULTR=true`: every provider except `vultr` is refused, and the server will not start without `VULTR_INFERENCE_API_KEY`. `/api/health` reports the provider, the model and whether this guard is on.

### Model-written code

When a provider is configured, `/api/plan` also asks the model (`app/codegen.py`) for one self-contained Python script for the task. The prompt lists exactly what the runner image offers (Python 3.12 standard library only), the read-only input, the writable `/workspace/output/` folder and the manifest's network rule. The script is stored with the manifest ID and shown, collapsed, on the permission card.

Approval covers the permission manifest, not the code: `/api/approve` cannot supply or edit a script. After approval, the script is delivered as `/workspace/input/task.py` on the read-only input mount and run by the image's Python (`analyze.py --script`) with the same flags and limits as any run. If it exits non-zero or times out, the model gets the previous script and the last 2000 characters of its error output and writes a corrected one, which runs in a fresh container under the identical limits. There are at most 3 attempts; the run stops at the first success. The run record and audit list every attempt (exit code, time, script, output) and confirm the limits were identical.

`LLM_CODE_MAX_TOKENS` (default 3000) sets the code reply budget. If a reasoning model spends it all, generation retries once with low reasoning effort.

**Fallback:** with no provider configured, or if generation fails at plan time, the plan is labelled built-in and the fixed `analyze.py` analysis runs instead, exactly as before. Model-written scripts never run outside Docker, even with `SLIPSTREAM_ALLOW_UNSANDBOXED=true`. The container, not any check on the code, is the security boundary.

After pulling this change, rebuild the runner image (step 5 above) so it has the `--script` mode.

The app also starts without Docker for UI exploration. In that mode it reports that the hardened runner is unavailable and does not execute the task. Set `SLIPSTREAM_ALLOW_UNSANDBOXED=true` only for disposable development; this explicitly disables isolation.

## What is enforced

- The inference model can suggest a plan, never approve it or set raw runtime flags.
- Backend policy caps memory at 512 MiB, CPU at 1 core, wall time at 60 seconds, denies secrets, and currently denies task network access.
- The runner uses `--network none`, read-only root, dropped capabilities, `no-new-privileges`, PID/memory/CPU caps, and a bounded writable output mount. Docker must be installed and reachable.
- The UI must submit the exact server-issued manifest ID to run. Approval is explicit.
- Each run uses a fresh container, which is removed on completion or timeout. The report includes allowed/used/blocked data.

This MVP is a single-user hackathon demo, not a multi-tenant production service. Docker is a meaningful isolation boundary but is not equivalent to gVisor or a microVM. For deployment, configure Docker's `runsc` runtime and set `SANDBOX_RUNTIME=runsc`.

## Vultr deployment

Deploy on one Vultr Cloud Compute VM (Ubuntu 24.04). The control server runs directly on the host under systemd, never in a container with the Docker socket. Sandboxes run under gVisor, and Caddy provides HTTPS. `deploy/` holds the setup script, systemd unit, Caddyfile and env template. See the [deployment runbook](docs/DEPLOY_VULTR.md). Inference calls go to Vultr's OpenAI-compatible endpoint. The task runner has no network; the control plane is the only component allowed to call inference.

The server binds to `HOST` (default `127.0.0.1`) and `PORT` (default `8080`). It sends no CORS header unless `ALLOWED_ORIGIN` names one origin. For a public URL it enforces `MAX_CONCURRENT_RUNS` (default 2; extra approvals get 429 and stay awaiting approval). It rate-limits each client IP to `RATE_LIMIT_PER_MIN` plans per minute (default 10), and to twice that across plan, red team and approve requests; the IP comes from `X-Forwarded-For` only with `TRUST_PROXY=true`. Tasks are capped at 1000 characters and request bodies at 16 KB. `MAX_LLM_CALLS_PER_DAY` (default 300, UTC day) is a global model-call cap; past it, plans use the built-in planner and are labelled so. Only the newest 200 runs from the last 2 hours are kept in memory.

## Repository map

- `app/server.py` — HTTP API, planner, approval lifecycle, audit reports.
- `app/policy.py` — deterministic manifest policy.
- `app/llm.py` — provider-agnostic chat client (Vultr, Groq for development).
- `app/codegen.py` — model-written scripts: prompt, code extraction, retries on token exhaustion.
- `app/redteam.py` — red team self-test: fixed manifests, canned scripts, ATTEMPT-line scorecard.
- `app/runner.py` — ephemeral Docker runner and capability checks.
- `static/` — permission approval and live run dashboard.
- `sandbox/` — minimal runner image and CSV analysis program.
- `docs/` — architecture, threat model, demo and Vultr deployment notes.

## Demo walkthrough

1. Enter a CSV analysis task.
2. Inspect the permission slip: files, memory, CPU, duration, network, and secrets.
3. Approve it. Before approval, Run is unavailable.
4. Watch real container lifecycle and task events.
5. Inspect the output chart, the network section (allowed hosts, hosts used, blocked attempts) and the allowed/used/blocked audit.

### Red Team self-test

The **Red Team self-test** buttons run canned, harmless scripts from `app/redteam.py` instead of a task (`POST /api/redteam {"scenario": ...}`). There is no model call and no provider is needed. Each one gets a fixed manifest (128 MB, 0.5 CPU, `network_domains: ["example.com"]`, no secrets) that still passes through the policy and still needs approval through `/api/approve`, with the same tighten-only rules. Runs are labelled `RED TEAM SELF-TEST` and recorded with mode `redteam`.

6. Click **Escape attempts**, review the slip, approve. The scorecard should show: `exfil_unlisted_host` BLOCKED (the proxy returns 403, and its "host not in allowlist" line shows as a red row in the network section), `direct_socket_bypass` BLOCKED (network unreachable), `write_outside_output` BLOCKED (read-only file system for `/etc`, `/app` and the input folder), `write_output_folder` OK, `read_secrets` BLOCKED (0 names; only a count is printed, never a value), and `allowed_host` OK (example.com through the proxy).
7. Click **Memory bomb** and approve. It allocates 16 MB chunks until the 128 MB limit kills it: "Killed at memory limit" (exit code 137).
8. Click **Runaway CPU** and approve. It loops forever until the 8-second limit kills it: "Killed at time limit after N seconds".

The run record and audit carry `redteam_attempts` (name, result, expected, detail) next to the `network` section, plus `redteam_outcome` for the memory and runaway runs.

Submission checklist: public GitHub repository, deployed demo URL, under-5-minute demo video, PDF slides, and event-specific fields from the live submission form. The public event dashboard currently lists tracks as TBA.

## License

MIT
