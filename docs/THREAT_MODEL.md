# Threat model

## Assets

Inference API key, host files and credentials, Vultr instance, sample input, run output, and service availability.

## Threats addressed in this MVP

- Prompt injection asks for broader permissions: planner is told task text is untrusted; backend ignores model-provided network/files/secrets and uses fixed policy.
- Unapproved execution: the run endpoint requires a server-issued manifest ID still in `awaiting_approval` state.
- Network exfiltration from task code: runner gets `--network none`.
- Host file access: only explicit input and output directories are mounted; input is read-only.
- Resource exhaustion: Docker enforces memory, CPU, PID and time limits.
- Credential leakage into the task: secret values exist only in the control process environment, never in the runner container environment.

## Limitations

The host Docker daemon and socket remain trusted; a compromise of the control API could ask Docker to launch containers. Docker shares the host kernel unless configured with a stronger runtime. The prototype has no user authentication, rate limiting, durable audit store, or multi-tenant isolation. Do not deploy it as a public general-purpose code execution service. On a real judging deployment, use a dedicated Vultr instance, TLS/auth, gVisor, narrow firewall rules, and short-lived rotated credentials.
