# Deploy on Vultr

This is a runbook for the hackathon demo. Resource creation and billing happen in the Vultr account; check current pricing before deployment.

1. Create a Vultr Cloud Compute instance with Docker Engine installed. For a stronger runtime boundary, install/configure gVisor `runsc` on the host.
2. Create a Vultr Serverless Inference subscription and copy its API key into a server-side environment secret named `VULTR_INFERENCE_API_KEY`. The app calls `https://api.vultrinference.com/v1/chat/completions`.
3. Build the control image and runner image, tag and push them to Vultr Container Registry. Keep the same runner image tag available to the host Docker daemon.
4. Configure firewall ingress for the demo's web port. Keep the Docker socket private to the single-user app host; do not expose it on TCP.
5. Set `PORT`, `VULTR_INFERENCE_API_KEY`, `VULTR_INFERENCE_MODEL`, and optionally `SANDBOX_RUNTIME=runsc`, then start with `docker compose up -d`.
6. Open the web URL, submit the sample task, approve it, and verify the event stream reports Docker (and runsc when configured). A deployment without Docker reports that no task process started.

For a public judging demo, use TLS through a reverse proxy, add authentication, rotate keys after the event, and do not store run data beyond the demo session. The included server is intentionally a lightweight single-user prototype; do not expose it directly to untrusted multi-user traffic.

## Container Registry

Use the registry endpoint and credentials shown in the Vultr Container Registry console. A typical workflow is:

```sh
docker build -t <registry>/<namespace>/slipstream-control:demo .
docker build -t <registry>/<namespace>/slipstream-runner:demo -f sandbox/Dockerfile .
docker push <registry>/<namespace>/slipstream-control:demo
docker push <registry>/<namespace>/slipstream-runner:demo
```

Set `SLIPSTREAM_RUNNER_IMAGE` to the runner image's registry tag in the control service. Authenticate the host Docker daemon to the registry so it can pull the runner image. Keep tags immutable for the judging demo.
