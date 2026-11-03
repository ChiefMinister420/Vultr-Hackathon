# Slipstream

**A permission slip before every agent run.** Slipstream turns a task into a bounded manifest, waits for approval, executes the work in an ephemeral container, and reports what it used and what was denied.

> Hackathon MVP: the included live demo handles CSV analysis. It uses a real Docker container with hard resource limits when Docker is available. Vultr Serverless Inference can draft the manifest; deterministic policy checks always run after it. A local mode is provided for development, but it is visibly labeled and is not a security boundary.

## Run locally

Requirements: Python 3.11+ and Docker Desktop/Engine. For inference-backed planning, set `VULTR_INFERENCE_API_KEY` and optionally `VULTR_INFERENCE_MODEL` (default `deepseek-v4.1-flash-1017`).

```sh
cp .env.example .env   # optional; add your inference key
docker build -t slipstream-runner:local -f sandbox/Dockerfile .
python3 -m app.server
```

Open http://localhost:8080. Try “Analyze the sample sales CSV and show the top 5 products by revenue.” Review and approve the permission card, then watch the execution events and report. The sample dataset lives at `/workspace/input/sales.csv` inside the runner.

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

- `app/server.py` — HTTP API, planner, deterministic manifest policy, approval lifecycle, audit reports.
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
