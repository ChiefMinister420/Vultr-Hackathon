# 3-minute demo script

**0:00 — Problem.** “A generic agent sandbox has the wrong permissions for most tasks. Slipstream turns each task into a reviewed permission slip.”

**0:20 — Plan.** Enter “Analyze the sample sales CSV and show the top 5 products by revenue.” Point out network none, no secrets, exact input/output paths, and bounded CPU, memory, and time. Mention that Vultr Serverless Inference drafted the intent if the key is configured.

**0:55 — Consent.** Approve the manifest. Before approval, there is no runner. Show the state change and execution events.

**1:15 — Real execution.** Show the Docker runner start and finish, 14 rows processed, ranked products, and chart output. The runner has no network and mounts only the input/output paths.

**2:05 — Audit.** Compare allowed limits with the files and elapsed time used. Show cleanup status. Explain that the sample job deliberately has no network access, so it cannot fetch a URL or exfiltrate a file.

**2:35 — Vultr stack and roadmap.** Show the inference call path and deployment on Vultr Cloud Compute with app/runner images in Vultr Container Registry. The next hardening step is gVisor/runsc plus real per-process telemetry, followed by signed reports and optional Object Storage for retained artifacts.

Record a continuous screen capture of the working deployed app. Do not present a local fallback or a configured-but-unavailable integration as a live Vultr execution.
