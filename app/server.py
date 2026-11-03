from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.runner import docker_ready, execute_csv

ROOT = Path(__file__).resolve().parents[1]
MAX_MEM = 512
MAX_CPU = 1
MAX_TIME = 60

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

INFERENCE_STATUS = {
    "configured": bool(os.getenv("VULTR_INFERENCE_API_KEY")),
    "connected": False,
    "model": os.getenv("VULTR_INFERENCE_MODEL", "deepseek-v4.1-flash-1017"),
    "status": "not_tested",
    "last_error": None,
    "last_checked": None,
}


def infer_manifest(task: str) -> tuple[dict, str]:
    """Use Vultr inference as a planner; sanitize every field locally."""
    source = "Deterministic planner"
    draft = {}

    key = os.getenv("VULTR_INFERENCE_API_KEY")
    model = os.getenv("VULTR_INFERENCE_MODEL", "deepseek-v4.1-flash-1017")

    INFERENCE_STATUS.update(
        configured=bool(key),
        model=model,
        connected=False,
        status="connecting" if key else "not_configured",
        last_error=None,
        last_checked=time.time(),
    )

    if not key:
        logger.warning("Vultr inference is not configured. Using deterministic planner.")
    else:
        started = time.monotonic()
        logger.info("Sending planning request to Vultr inference (model=%s).", model)

        try:
            payload = {
                "model": model,
                "temperature": 0,
                "max_tokens": 1000,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Return only a compact JSON object. "
                            "No markdown or explanations. "
                            "Keys: summary, network_domains, files, memory_mb, "
                            "cpu_cores, time_seconds, secrets. "
                            "Plan a CSV analysis using the sample file. "
                            "Never request secrets or network access."
                        ),
                    },
                    {"role": "user", "content": task[:2000]},
                ],
            }

            request = urllib.request.Request(
                "https://api.vultrinference.com/v1/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )

            with urllib.request.urlopen(request, timeout=12) as resp:
                response_body = json.load(resp)

            logger.info(
                "Vultr response structure: %s",
                json.dumps(response_body, default=str)[:4000],
            )

            choices = response_body.get("choices", [])
            if not choices:
                raise ValueError("Vultr response contained no choices.")

            message = choices[0].get("message", {})
            raw = message.get("content")

            if not raw:
                raise ValueError(
                    f"Vultr returned empty content. "
                    f"Finish reason: {choices[0].get('finish_reason')}"
                )

            raw = response_body["choices"][0]["message"]["content"]

            logger.info("Vultr raw response: %s", str(raw)[:2000])

            if not isinstance(raw, str):
                raise ValueError(f"Unexpected inference content type: {type(raw).__name__}")

            match = re.search(r"\{[\s\S]*\}", raw)
            if not match:
                raise ValueError("Inference response did not contain a JSON object.")

            draft = json.loads(match.group())
            if not isinstance(draft, dict):
                raise ValueError("Inference response JSON was not an object.")

            source = "Vultr Serverless Inference"
            INFERENCE_STATUS.update(
                connected=True,
                status="connected",
                last_error=None,
            )
            logger.info(
                "Vultr inference request succeeded in %.2fs (model=%s).",
                time.monotonic() - started,
                model,
            )

        except urllib.error.HTTPError as exc:
            # Do not log request headers or the API key.
            detail = f"HTTP {exc.code} {exc.reason}"
            INFERENCE_STATUS.update(
                connected=False,
                status="error",
                last_error=detail,
            )
            source = "Deterministic planner (Vultr inference unavailable)"
            logger.exception("Vultr inference returned %s.", detail)

        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"[:250]
            INFERENCE_STATUS.update(
                connected=False,
                status="error",
                last_error=detail,
            )
            source = "Deterministic planner (Vultr inference unavailable)"
            logger.exception("Vultr inference request failed.")

    # For this demonstrator tasks consume a fixed, reviewed capability template.
    # Model suggestions never expand permissions.
    manifest = {
        "summary": str(
            draft.get("summary")
            or "Analyze the bundled sales CSV and return top products by revenue."
        )[:180],
        "network_domains": [],
        "files": [
            "/workspace/input/sales.csv:read",
            "/workspace/output/report.json:write",
        ],
        "memory_mb": min(MAX_MEM, max(64, _integer(draft.get("memory_mb"), 256))),
        "cpu_cores": min(MAX_CPU, max(0.25, _number(draft.get("cpu_cores"), 1))),
        "time_seconds": min(MAX_TIME, max(5, _integer(draft.get("time_seconds"), 30))),
        "secrets": [],
    }

    logger.info(
        "Manifest drafted | planner=%s | memory=%sMB | cpu=%s | time=%ss",
        source,
        manifest["memory_mb"],
        manifest["cpu_cores"],
        manifest["time_seconds"],
    )
    return manifest, source


def _integer(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _number(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
            manifest, planner = infer_manifest(task)
            mid = secrets.token_urlsafe(12)

            record = {
                "id": mid,
                "task": task[:2000],
                "manifest": manifest,
                "planner": planner,
                "state": "awaiting_approval",
                "events": [
                    {
                        "at": time.time(),
                        "kind": "plan",
                        "text": f"Manifest drafted by {planner}.",
                    }
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

                try:
                    requested = max(
                        5,
                        int(body.get("time_seconds", rec["manifest"]["time_seconds"])),
                    )
                except (TypeError, ValueError):
                    requested = rec["manifest"]["time_seconds"]

                rec["manifest"]["time_seconds"] = min(
                    rec["manifest"]["time_seconds"], MAX_TIME, requested
                )
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

        logger.info("Starting execution | run_id=%s", rid)

        try:
            result = execute_csv(task, manifest)
            logger.info("Full runner result: %s", json.dumps(result, default=str)[:4000])
            logger.info(
                "Execution finished | run_id=%s | status=%s | engine=%s | elapsed=%ss",
                rid,
                result.get("status"),
                result.get("engine", "unknown"),
                result.get("elapsed_seconds", 0),
            )

            with LOCK:
                rec["result"] = result
                rec["state"] = result["status"]
                rec["events"].append(
                    {
                        "at": time.time(),
                        "kind": "runner",
                        "text": (
                            f"{result.get('engine', 'Docker')} execution "
                            f"{result['status']} in "
                            f"{result.get('elapsed_seconds', 0)}s."
                        ),
                    }
                )
                rec["report"] = {
                    "allowed": manifest,
                    "used": {
                        "network_domains": [],
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
                        "memory_limit_mb": manifest["memory_mb"],
                        "cpu_limit_cores": manifest["cpu_cores"],
                        "elapsed_seconds": result.get("elapsed_seconds"),
                    },
                    "blocked_attempts": [],
                    "cleanup": (
                        "Ephemeral container removed after run."
                        if result.get("engine", "").startswith("Docker")
                        else "No container was created."
                    ),
                }

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
                }


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    logger.info("Slipstream listening on http://0.0.0.0:%s", port)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
