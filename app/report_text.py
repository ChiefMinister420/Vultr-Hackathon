"""Plain-language run report: short sentences built only from the run record and audit.

No model call and no guessing. A sentence whose facts are missing from the record is skipped.
"""

from __future__ import annotations

MAX_RESULT_CHARS = 600
MAX_FILES_LISTED = 5
MAX_BLOCKED_LISTED = 20

PROVIDERS = {
    "vultr": "Vultr Serverless Inference",
    "groq": "Groq (a development-only provider)",
}

# The proxy's reasons, in plain words.
BLOCK_REASONS = {
    "host not in allowlist": "that website was not on the list",
    "port not allowed": "it was not a secure connection",
    "method not allowed": "it was not a secure connection",
    "IP literal not allowed": "it used a raw address instead of a website name",
    "host did not resolve": "that website could not be found",
    "upstream connection failed": "the website could not be reached",
    "too many connections": "it opened too many connections at once",
}

# Red team attempts: (sentence start, plain outcome when BLOCKED, plain outcome when OK).
REDTEAM_SENTENCES = {
    "exfil_unlisted_host": (
        "It tried to send data to example.org",
        "blocked, because that website was not on the list",
        "it got through, which should not happen",
    ),
    "direct_socket_bypass": (
        "It tried to connect straight to an internet address, skipping the approved route",
        "blocked, because the sandbox has no direct internet connection",
        "it got through, which should not happen",
    ),
    "write_outside_output": (
        "It tried to change files outside its output folder",
        "blocked, because everything except the output folder is read-only",
        "it succeeded, which should not happen",
    ),
    "write_output_folder": (
        "It tried to save a file in its output folder",
        "refused, although that folder should accept files",
        "allowed, as expected",
    ),
    "read_secrets": (
        "It looked for passwords or keys",
        "none were available, so nothing could be taken",
        "it found some, which should not happen",
    ),
    "allowed_host": (
        "It tried to reach example.com, which was on the list",
        "refused, although that website was allowed",
        "allowed, as expected",
    ),
}
# Red team steps whose blocked sentence already names a website and reason, as
# (website, plain reason); the internet section does not repeat these.
REDTEAM_COVERS = {"exfil_unlisted_host": ("example.org", "that website was not on the list")}
# Red team steps where BLOCKED is the good outcome but nothing was actively refused.
REDTEAM_OK_WHEN_BLOCKED = {"read_secrets"}


def observations(run: dict) -> list[dict]:
    """Return [{level, text}] sentences describing a finished run, in a fixed order."""
    points: list[dict] = []

    def add(level: str, text: str) -> None:
        points.append({"level": level, "text": text})

    attempts = [a for a in (run.get("attempts") or []) if isinstance(a, dict)]
    report = run.get("report") if isinstance(run.get("report"), dict) else {}
    last = attempts[-1] if attempts else {}
    redteam = run.get("mode") == "redteam"
    limits = last.get("limits") if isinstance(last.get("limits"), dict) else {}

    _verdict(add, run, attempts, last, limits, redteam)
    _how(add, run, attempts, last, redteam)
    if attempts:
        _limits(add, last, limits)
    covered = set()
    if redteam:
        covered = {
            REDTEAM_COVERS[a["name"]]
            for a in run.get("redteam_attempts") or []
            if isinstance(a, dict)
            and a.get("name") in REDTEAM_COVERS
            and a.get("result") == "BLOCKED"
        }
    _internet(add, report.get("network"), covered)
    if attempts:
        _files(add, run, report)
    if run.get("manifest", {}).get("secrets") == [] and attempts:
        add("ok", "No passwords or keys were available to it.")
    if "removed" in str(report.get("cleanup") or ""):
        add("ok", "The sandbox was deleted afterwards.")
    if redteam:
        _redteam(add, run.get("redteam_attempts"))
    _result(add, run, last, redteam)
    return points


def _verdict(add, run, attempts, last, limits, redteam) -> None:
    if not attempts:
        if run.get("state") == "failed":
            add("problem", "It did not run, because the sandbox could not be started.")
        return
    seconds = _seconds(last.get("elapsed_seconds"))
    count = len(attempts)
    scenario = run.get("redteam_scenario") if redteam else None
    if last.get("killed"):
        cap = _number(limits.get("time_seconds"))
        text = "It was stopped because it ran past its time limit"
        text += f" of {cap} seconds." if cap is not None else "."
        add("ok" if redteam else "problem", text)
        if scenario == "runaway":
            add("ok", "This was the expected result of the test.")
    elif last.get("exit_code") == 137:
        memory = _number(limits.get("memory_mb"))
        text = "It was stopped because it ran out of memory"
        text += f" (it was given {memory} MB)." if memory is not None else "."
        add("ok" if redteam else "problem", text)
        if scenario == "memory":
            add("ok", "This was the expected result of the test.")
    elif last.get("status") == "completed":
        text = "It finished"
        if seconds:
            text += f" in {seconds}"
        if count > 1:
            text += f" on attempt {count}"
        add("ok", text + ".")
    elif count > 1:
        add("problem", f"It failed after {count} attempts.")
    else:
        add("problem", "It failed.")


def _how(add, run, attempts, last, redteam) -> None:
    mode = run.get("mode")
    if redteam:
        add("note", "It ran Slipstream's fixed red team test script; no AI was involved.")
    elif mode == "builtin":
        add("note", "It used Slipstream's built-in analysis program; no AI-written code ran.")
    elif mode == "model-written":
        provider = PROVIDERS.get(last.get("provider"))
        model = last.get("model")
        if provider and model:
            add("note", f"The code was written by {provider}, model {str(model)[:60]}.")
        elif provider:
            add("note", f"The code was written by {provider}.")

    count = len(attempts)
    if mode == "model-written" and count > 1:
        if last.get("status") == "completed":
            add(
                "note",
                f"Attempt 1 failed, so the error was sent back to the model for a fix; "
                f"attempt {count} succeeded.",
            )
        else:
            add(
                "note",
                f"Attempt 1 failed, so the error was sent back to the model for a fix; "
                f"all {count} attempts failed.",
            )
    if str(run.get("error") or "").startswith("Code generation failed"):
        add("problem", "A corrected version could not be written, so it stopped early.")


def _limits(add, last, limits) -> None:
    given = []
    memory = _number(limits.get("memory_mb"))
    if memory is not None:
        given.append(f"{memory} MB of memory")
    cores = _number(limits.get("cpu_cores"))
    if cores is not None:
        given.append(_cores(cores))
    cap = _number(limits.get("time_seconds"))
    if cap is not None:
        given.append(f"{cap} seconds")
    seconds = _seconds(last.get("elapsed_seconds"))
    if given:
        text = "It was given " + _join(given)
        add("note", text + (f", and it used {seconds}." if seconds else "."))

    # A stopped run is already told in the first sentence; only a run that ended on its own
    # gets the "stayed inside" sentence.
    stopped = last.get("killed") or last.get("exit_code") == 137
    if not stopped and last.get("status") in ("completed", "failed"):
        add("ok", "It stayed inside the memory, CPU and time it was given.")


def _internet(add, network, covered=frozenset()) -> None:
    if not isinstance(network, dict):
        return
    allowed = [str(h) for h in network.get("allowed") or []]
    used = [str(h) for h in network.get("used") or []]
    if not allowed:
        add("ok", "No internet access was given to this task.")
    else:
        add("note", "Only these websites were allowed: " + _join(allowed) + ".")
        if used:
            add("note", "It used: " + _join(used) + ".")
        else:
            add("note", "It did not use any of them.")

    counts: dict[tuple, int] = {}
    for item in network.get("blocked") or []:
        if isinstance(item, dict):
            key = (item.get("host"), _plain_reason(item.get("reason")))
            if key not in covered:
                counts[key] = counts.get(key, 0) + 1
    for (host, reason), times in list(counts.items())[:MAX_BLOCKED_LISTED]:
        who = f"It tried to reach {str(host)[:80]}" if host else "A connection attempt"
        repeat = f" ({times} times)" if times > 1 else ""
        add("blocked", f"{who}{repeat}: blocked, because {reason}.")
    if len(counts) > MAX_BLOCKED_LISTED:
        add("blocked", f"{len(counts) - MAX_BLOCKED_LISTED} more blocked attempts are not listed.")


def _files(add, run, report) -> None:
    files = run.get("output_files")
    if files is None:
        files = (report.get("used") or {}).get("output_files")
    if not isinstance(files, list):
        return
    files = [f for f in files if isinstance(f, dict) and f.get("name")]
    if not files:
        add("note", "It did not write any files.")
    else:
        listed = [
            f"{str(f['name'])[:60]}"
            + (f" ({_size(f['size'])})" if isinstance(f.get("size"), int) else "")
            for f in files[:MAX_FILES_LISTED]
        ]
        more = f" and {len(files) - MAX_FILES_LISTED} more" if len(files) > MAX_FILES_LISTED else ""
        noun = "file" if len(files) == 1 else "files"
        add(
            "note", f"It wrote {len(files)} {noun} to its output folder: {', '.join(listed)}{more}."
        )
    add("ok", "Everything else was read-only.")


def _redteam(add, attempts) -> None:
    for attempt in attempts or []:
        if not isinstance(attempt, dict) or not attempt.get("name"):
            continue
        name, result = str(attempt["name"]), attempt.get("result")
        plain = name.replace("_", " ")[:40]
        if result == "MISSING":
            add("problem", f"The test step '{plain}' did not report a result.")
            continue
        start, if_blocked, if_ok = REDTEAM_SENTENCES.get(
            name, (f"It tried the step '{plain}'", "blocked", "allowed")
        )
        outcome = if_blocked if result == "BLOCKED" else if_ok
        expected = attempt.get("as_expected")
        if expected is False:
            level = "problem"
        elif result == "BLOCKED" and name not in REDTEAM_OK_WHEN_BLOCKED:
            level = "blocked"
        else:
            level = "ok"
        add(level, f"{start}: {outcome}.")


def _result(add, run, last, redteam) -> None:
    # Red team output is raw test lines, already told above in plain words.
    if redteam or last.get("status") != "completed":
        return
    answer = str(run.get("final_answer") or "").strip()
    if answer:
        if len(answer) > MAX_RESULT_CHARS:
            answer = answer[: MAX_RESULT_CHARS - 1].rstrip() + "…"
        add("result", answer)


def _plain_reason(reason) -> str:
    reason = str(reason or "")
    if reason in BLOCK_REASONS:
        return BLOCK_REASONS[reason]
    if reason.startswith("resolves to"):
        return "that address is not allowed (it points to a private or internal network)"
    if "header" in reason or "malformed" in reason:
        return "the request was malformed"
    return "the safety proxy refused it"


def _number(value):
    """A whole number as int, other numbers as given; None for anything else."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value) if float(value).is_integer() else value


def _seconds(value) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return f"{value:.1f} seconds"


def _cores(cores) -> str:
    if cores == 1:
        return "1 CPU core"
    if cores < 1:
        return f"{cores} of a CPU core"
    return f"{cores} CPU cores"


def _size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size < 0.05 * 1024:
        return "under 0.1 KB"
    return f"{size / 1024:.1f} KB"


def _join(items: list[str]) -> str:
    items = [str(i)[:80] for i in items]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]
