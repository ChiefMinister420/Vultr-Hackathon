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

The app also starts without Docker for UI exploration. In that mode it reports that the hardened runner is unavailable and does not execute the task. Set `SLIPSTREAM_ALLOW_UNSANDBOXED=true` only for disposable development; this explicitly disables isolation.

## What is enforced

- The inference model can suggest a plan, never approve it or set raw runtime flags.
- Backend policy caps memory at 512 MiB, CPU at 1 core, wall time at 60 seconds, denies secrets, and currently denies task network access.
- The runner uses `--network none`, read-only root, dropped capabilities, `no-new-privileges`, PID/memory/CPU caps, and a bounded writable output mount. Docker must be installed and reachable.
- The UI must submit the exact server-issued manifest ID to run. Approval is explicit.
- Each run uses a fresh container, which is removed on completion or timeout. The report includes allowed/used/blocked data.

This MVP is a single-user hackathon demo, not a multi-tenant production service. Docker is a meaningful isolation boundary but is not equivalent to gVisor or a microVM. For deployment, configure Docker's `runsc` runtime and set `SANDBOX_RUNTIME=runsc`.

## Vultr deployment

Deploy the web/control layer on a Vultr Cloud Compute instance with Docker installed, add the Vultr Serverless Inference API key as a server-side secret, build/push the image to Vultr Container Registry, and run Compose. See [deployment guide](docs/DEPLOY_VULTR.md). Inference calls go to Vultr's OpenAI-compatible endpoint. The task runner has no network; the control plane is the only component allowed to call inference.

## Repository map

- `app/server.py` — HTTP API, planner, approval lifecycle, audit reports.
- `app/policy.py` — deterministic manifest policy.
- `app/llm.py` — provider-agnostic chat client (Vultr, Groq for development).
- `app/runner.py` — ephemeral Docker runner and capability checks.
- `static/` — permission approval and live run dashboard.
- `sandbox/` — minimal runner image and CSV analysis program.
- `docs/` — architecture, threat model, demo and Vultr deployment notes.

## Demo walkthrough

1. Enter a CSV analysis task.
2. Inspect the permission slip: files, memory, CPU, duration, network, and secrets.
3. Approve it. Before approval, Run is unavailable.
4. Watch real container lifecycle and task events.
5. Inspect the output chart and the allowed/used/blocked audit.

Submission checklist: public GitHub repository, deployed demo URL, under-5-minute demo video, PDF slides, and event-specific fields from the live submission form. The public event dashboard currently lists tracks as TBA.

## License

MIT
