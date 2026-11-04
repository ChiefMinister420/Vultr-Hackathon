from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app import codegen, llm
from app.policy import apply_policy, tighten
from app.runner import cleanup_stale, docker_ready, execute_csv

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_CSV = ROOT / "examples" / "sales.csv"
MODEL_WRITTEN = "model-written"
BUILTIN = "builtin"
MAX_ATTEMPTS = 3

RUNS = {}
LOCK = threading.Lock()


CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("slipstream")


def load_env() -> None:
    """Load simple KEY=value entries from the project .env without extra dependencies."""
    env_file = ROOT / ".env"
    if not env_file.is_file():
        logger.info("No .env file found; using process environment variables.")
        return

    loaded = 0
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()

        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()

        if name and name not in os.environ:
            os.environ[name] = value
            loaded += 1

    logger.info("Loaded %d environment setting(s) from .env.", loaded)


load_env()

INFERENCE_STATUS = llm.status() | {
    "connected": False,
    "status": "not_tested",
    "last_error": None,
    "last_checked": None,
}

PLAN_PROMPT = (
    "Return only a compact JSON object. "
    "No markdown or explanations. "
    "Keys: summary, network_domains, files, memory_mb, "
    "cpu_cores, time_seconds, secrets. "
    "Plan a CSV analysis using the sample file. "
    "Never request secrets or network access."
)


def infer_manifest(task: str) -> tuple[dict, str, dict]:
    """Use the configured LLM as a planner; sanitize every field locally."""
    source = "Deterministic planner"
    planned_by = {"provider": "builtin", "model": None, "finish_reason": None}
    draft = {}

    config = llm.status()
    INFERENCE_STATUS.update(
        config,
        connected=False,
        status="connecting" if config["configured"] else "not_configured",
        last_error=config["error"],
        last_checked=time.time(),
    )

    if config["error"]:
        INFERENCE_STATUS.update(status="error")
        logger.error("LLM configuration refused: %s Using deterministic planner.", config["error"])
    elif not config["configured"]:
        logger.warning("No LLM provider is configured. Using deterministic planner.")
    else:
        provider, model = config["provider"], config["model"]
        preset = llm.PRESETS[provider]
        started = time.monotonic()
        logger.info("Sending planning request to %s (model=%s).", preset["short"], model)

        try:
            result = llm.chat(
                [
                    {"role": "system", "content": PLAN_PROMPT},
                    {"role": "user", "content": task[:2000]},
                ],
                purpose="plan",
                temperature=0,
                # Reasoning models spend part of the budget before answering.
                max_tokens=1500 if provider == "groq" else 1000,
            )
            raw = result.text

            logger.debug("%s raw response: %s", preset["short"], raw[:2000])

            match = re.search(r"\{[\s\S]*\}", raw)
            if not match:
                raise ValueError("Inference response did not contain a JSON object.")

            draft = json.loads(match.group())
            if not isinstance(draft, dict):
                raise ValueError("Inference response JSON was not an object.")

            source = preset["label"]
            planned_by = {
                "provider": result.provider,
                "model": result.model,
                "finish_reason": result.finish_reason,
            }
            INFERENCE_STATUS.update(
                connected=True,
                status="connected",
                last_error=None,
            )
            logger.info(
                "%s request succeeded in %.2fs (model=%s).",
                preset["short"],
                time.monotonic() - started,
                model,
            )

        except Exception as exc:
            # LLM errors never carry request headers or the API key.
            detail = (
                str(exc) if isinstance(exc, llm.LLMError) else f"{type(exc).__name__}: {exc}"
            )[:250]
            INFERENCE_STATUS.update(
                connected=False,
                status="error",
                last_error=detail,
            )
            source = f"Deterministic planner ({preset['short']} unavailable)"
            logger.exception("%s request failed: %s", preset["short"], detail)

    # Model suggestions never expand permissions; app.policy decides the manifest.
    manifest, notes = apply_policy(draft)

    logger.info(
        "Manifest drafted | planner=%s | memory=%sMB | cpu=%s | time=%ss | network=%s | notes=%d",
        source,
        manifest["memory_mb"],
        manifest["cpu_cores"],
        manifest["time_seconds"],
        manifest["network_domains"],
        len(notes),
    )
    return manifest, source, planned_by


def generate(task: str, manifest: dict, previous_script=None, error=None) -> tuple[str, dict]:
    """Ask the configured model for a script; return it with who wrote it."""
    config = llm.status()
    if not config["configured"] or config["error"]:
        raise codegen.CodegenError(config["error"] or "No LLM provider is configured.")
    script = codegen.generate_script(
        task,
        codegen.RUNNER_MODULES,
        codegen.preview_csv(SAMPLE_CSV),
        previous_script=previous_script,
        error=error,
        network_domains=manifest["network_domains"],
    )
    return script, {"provider": config["provider"], "model": config["model"]}


def attempt_record(number: int, mode: str, written_by: dict, script, result: dict) -> dict:
    model_written = mode == MODEL_WRITTEN
    return {
        "attempt": number,
        "mode": mode,
        "provider": written_by.get("provider") if model_written else BUILTIN,
        "model": written_by.get("model") if model_written else "analyze.py",
        "status": result.get("status"),
        "exit_code": result.get("exit_code"),
        "killed": bool(result.get("killed")),
        "elapsed_seconds": result.get("elapsed_seconds"),
        "container": result.get("container"),
        "stdout": (result.get("stdout") or "")[-4096:],
        "stderr": (result.get("stderr") or "")[-4096:],
        "script": script if model_written else None,
        "limits": result.get("limits"),
        "net_events": result.get("net_events") or [],
    }


def audit_report(
    manifest: dict, result: dict, planner: dict | None = None, attempts: list | None = None
) -> dict:
    # Limits come from what the runner passed to Docker, not from the manifest.
    limits = result.get("limits") or {}
    domains = limits.get("network_domains") or []
    if attempts:
        events = [event for attempt in attempts for event in attempt["net_events"]]
    else:
        events = result.get("net_events") or []
    used_hosts = sorted({e.get("host") for e in events if e.get("allowed")})
    blocked = [
        {"host": e.get("host"), "port": e.get("port"), "reason": e.get("reason")}
        for e in events
        if not e.get("allowed")
    ]

    if result.get("killed"):
        cleanup = "Ephemeral container created, killed at its time limit, and removed."
    elif result.get("engine", "").startswith("Docker"):
        cleanup = "Ephemeral container removed after run."
    else:
        cleanup = "No container was created."
    if domains:
        cleanup += " Proxy container and internal network removed."
    if attempts and len(attempts) > 1:
        cleanup = f"{len(attempts)} attempts, each in a fresh container. Last attempt: {cleanup}"

    report = {
        "allowed": manifest,
        "network": {
            "mode": "allowlist proxy" if domains else "denied (--network none)",
            "allowed": domains,
            "used": used_hosts,
            "blocked": blocked,
        },
        "used": {
            "network_domains": used_hosts,
            # This is an artifact-based indicator, not syscall-level
            # measurement of file access.
            "files": (
                [
                    "/workspace/input/sales.csv:read",
                    "/workspace/output/report.json:write",
                ]
                if result.get("artifact")
                else []
            ),
            "memory_limit_mb": limits.get("memory_mb"),
            "cpu_limit_cores": limits.get("cpu_cores"),
            "elapsed_seconds": result.get("elapsed_seconds"),
            "output_files": result.get("files") or [],
        },
        "blocked_attempts": blocked,
        "cleanup": cleanup,
        "planner": _planner_info(planner),
    }
    if attempts:
        # Every attempt's limits come from the dict the runner passed to Docker.
        report["attempts"] = [
            {
                key: attempt[key]
                for key in (
                    "attempt",
                    "mode",
                    "provider",
                    "model",
                    "status",
                    "exit_code",
                    "killed",
                    "elapsed_seconds",
                    "container",
                    "limits",
                )
            }
            for attempt in attempts
        ]
        report["identical_limits"] = all(a["limits"] == attempts[0]["limits"] for a in attempts)
    return report


def _planner_info(planner: dict | None) -> dict:
    planner = planner or {}
    return {"provider": planner.get("provider"), "model": planner.get("model")}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        # We use structured application logs instead of the default HTTP access log.
        pass

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/health":
            return self.send_json(
                {
                    "ok": True,
                    "docker_ready": docker_ready(),
                    "inference": dict(INFERENCE_STATUS),
                }
            )

        if self.path.startswith("/api/runs/"):
            rid = self.path.rsplit("/", 1)[-1]
            with LOCK:
                record = RUNS.get(rid)
            return self.send_json(record or {"error": "not found"}, 404 if not record else 200)

        static_root = (ROOT / "static").resolve()
        target = self.path.split("?", 1)[0]  # strip query string, if any
        if target in ("/", "/index.html"):
            path = static_root / "index.html"
        else:
            path = (static_root / target.lstrip("/")).resolve()

        if path.is_relative_to(static_root) and path.is_file():
            content = path.read_bytes()
            content_type = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        except (json.JSONDecodeError, ValueError):
            return self.send_json({"error": "invalid JSON"}, 400)

        if self.path == "/api/plan":
            task = str(body.get("task", "")).strip()
            if not task:
                return self.send_json({"error": "Enter a task."}, 400)

            logger.info("Received plan request.")
            manifest, planner, planned_by = infer_manifest(task)
            mid = secrets.token_urlsafe(12)

            # The script is stored with this manifest ID; approval can never replace it.
            mode, script, written_by, codegen_error = BUILTIN, None, None, None
            if llm.status()["configured"]:
                try:
                    script, written_by = generate(task, manifest)
                    mode = MODEL_WRITTEN
                except Exception as exc:
                    codegen_error = str(exc)[:500]
                    logger.warning("Code generation failed; using built-in analysis: %s", exc)
            mode_text = (
                f"Script written by {written_by['provider']} ({written_by['model']})."
                if mode == MODEL_WRITTEN
                else "Built-in analysis (analyze.py) will run; no model-written code."
            )

            record = {
                "id": mid,
                "task": task[:2000],
                "manifest": manifest,
                "planner": planner,
                "provider": planned_by["provider"],
                "model": planned_by["model"],
                "finish_reason": planned_by["finish_reason"],
                "mode": mode,
                "script": script,
                "codegen": written_by,
                "codegen_error": codegen_error,
                "state": "awaiting_approval",
                "events": [
                    {
                        "at": time.time(),
                        "kind": "plan",
                        "text": f"Manifest drafted by {planner}.",
                    },
                    {"at": time.time(), "kind": "plan", "text": mode_text},
                ],
            }

            with LOCK:
                RUNS[mid] = record

            return self.send_json(record)

        if self.path == "/api/approve":
            rid = body.get("id")

            with LOCK:
                rec = RUNS.get(rid)
                if not rec:
                    return self.send_json({"error": "Unknown manifest."}, 404)

                if rec["state"] != "awaiting_approval":
                    return self.send_json(
                        {"error": "Manifest is no longer awaiting approval."}, 409
                    )

                if any(key in body for key in ("script", "code", "mode")):
                    return self.send_json(
                        {
                            "error": "Approval covers the permission manifest; "
                            "it cannot supply or change code."
                        },
                        400,
                    )

                try:
                    rec["manifest"] = tighten(rec["manifest"], body)
                except ValueError as exc:
                    return self.send_json({"error": str(exc)}, 400)

                rec["state"] = "running"
                rec["approved_at"] = time.time()
                rec["events"].append(
                    {
                        "at": time.time(),
                        "kind": "approval",
                        "text": "User approved manifest. Ephemeral runner starting.",
                    }
                )

            logger.info("Manifest approved | run_id=%s", rid)
            threading.Thread(target=self._run, args=(rid,), daemon=True).start()
            return self.send_json({"state": "running", "id": rid}, 202)

        return self.send_json({"error": "not found"}, 404)

    def _run(self, rid):
        with LOCK:
            rec = RUNS[rid]
            task = rec["task"]
            manifest = rec["manifest"]
            planned_by = {"provider": rec.get("provider"), "model": rec.get("model")}
            mode = rec.get("mode", BUILTIN)
            script = rec.get("script")
            written_by = rec.get("codegen") or {}
            attempts = rec["attempts"] = []

        logger.info("Starting execution | run_id=%s | mode=%s", rid, mode)
        regen_error = None

        try:
            for number in range(1, MAX_ATTEMPTS + 1):
                result = execute_csv(task, manifest, script if mode == MODEL_WRITTEN else None)
                # Debug only: stdout comes from model-written code.
                logger.debug("Full runner result: %s", json.dumps(result, default=str)[:4000])
                logger.info(
                    "Attempt finished | run_id=%s | attempt=%d | status=%s | exit_code=%s | "
                    "engine=%s | elapsed=%ss",
                    rid,
                    number,
                    result.get("status"),
                    result.get("exit_code"),
                    result.get("engine", "unknown"),
                    result.get("elapsed_seconds", 0),
                )

                with LOCK:
                    attempts.append(attempt_record(number, mode, written_by, script, result))
                    rec["events"].append(
                        {
                            "at": time.time(),
                            "kind": "runner",
                            "text": (
                                f"Attempt {number}: {result.get('engine', 'Docker')} execution "
                                f"{result['status']} in "
                                f"{result.get('elapsed_seconds', 0)}s."
                            ),
                        }
                    )

                if result["status"] == "completed" or mode != MODEL_WRITTEN:
                    break
                if number == MAX_ATTEMPTS:
                    break
                try:
                    script, written_by = generate(
                        task, manifest, previous_script=script, error=result.get("stderr")
                    )
                except Exception as exc:
                    regen_error = f"Code generation failed before attempt {number + 1}: {exc}"
                    logger.warning("%s | run_id=%s", regen_error, rid)
                    with LOCK:
                        rec["events"].append(
                            {"at": time.time(), "kind": "error", "text": regen_error[:300]}
                        )
                    break

            with LOCK:
                rec["result"] = result
                rec["state"] = result["status"]
                rec["final_answer"] = attempts[-1]["stdout"]
                rec["output_files"] = result.get("files") or []
                if regen_error:
                    rec["error"] = regen_error
                rec["report"] = audit_report(manifest, result, planned_by, attempts)

        except Exception as exc:
            logger.exception("Execution failed | run_id=%s", rid)
            with LOCK:
                rec["state"] = "failed"
                rec["error"] = str(exc)
                rec["events"].append({"at": time.time(), "kind": "error", "text": str(exc)})
                rec["report"] = {
                    "allowed": manifest,
                    "used": {},
                    "blocked_attempts": [],
                    "cleanup": "No task process was started.",
                    "planner": _planner_info(planned_by),
                }


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    try:
        llm.settings()
    except llm.LLMError as exc:
        raise SystemExit(f"LLM configuration error: {exc}") from None
    try:
        logger.info("Removed %d stale sandbox resource(s).", cleanup_stale())
    except Exception:
        logger.exception("Stale sandbox cleanup failed; continuing.")
    logger.info("Slipstream listening on http://0.0.0.0:%s", port)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
