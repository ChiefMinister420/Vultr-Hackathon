# Hackathon submission kit

## Project

- **Name:** Slipstream
- **Tagline:** Permission slips for agents, enforced at runtime.
- **Short description:** Slipstream turns an agent task into a user-approved, least-privilege manifest, runs a concrete CSV analysis in an ephemeral bounded container, and produces an audit report of permissions and execution.
- **Category:** AI agents / developer tools / cloud security (choose the closest categories offered on the event form).
- **Technology:** Vultr Serverless Inference, Vultr Cloud Compute, Docker with optional gVisor, Vultr Container Registry for deployment.

## Demo and presentation assets to produce

- Public GitHub repository URL
- Public interactive demo URL
- MP4 or MOV demo, ideally 2–3 minutes and under 5 minutes
- PDF slides
- Cover image if the form accepts one

These are submission assets to record/export from a real Vultr deployment. The app is not marked as deployed from this workspace. For the on-camera execution proof, show the inference-backed planner, manifest approval, live Docker/runsc lifecycle, CSV result and audit. The LabLab event dashboard currently shows tracks as TBA, so re-check the event page/form before final submission.

## Slide outline

1. Problem: generic sandboxes are too restrictive or too permissive.
2. Product: task → reviewed manifest → bounded run → evidence report.
3. Live demo: sample CSV, permission card, approval, chart, cleanup report.
4. Architecture: Vultr Serverless Inference + Vultr Cloud Compute + Container Registry + isolated runner.
5. Safety design and measured limits: fixed policy, network disabled, no secrets, resource caps.
6. Market and business model: developer platform/API priced per run or team seat; validate with agent-platform and regulated-workflow teams.
7. Roadmap and limitations: gVisor, stronger telemetry, multi-tenant auth, persistent signed audit.
