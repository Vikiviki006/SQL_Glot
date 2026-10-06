"""HTTP front door for the migration transpiler.

The contract mirrors the compiler's own instead of inventing a second one. The
compiler prints a receipt and puts every finding inside the two artifacts it
writes -- a BLOCK, a GOVERNANCE check, an ASSUMPTION and an EDGE all live in
seatunnel.conf or duckdb.yaml, where the person responsible for the run will
meet them (AGENT.md §3a). There is no findings console to proxy, so there is
no findings route either: POST /transpile returns the receipt the CLI would
have printed plus the two generated files, and nothing else.

Four routes:

    GET  /health     {status, tool, version} -- the transpiler module's own
                     constants, so a version mismatch is visible from a curl.

    POST /transpile  multipart form: `spec`, the migration specification YAML
                     (required); `catalog`, the Oracle source schema catalog
                     YAML (optional -- a specification that states its own
                     columns needs none, wildcard scope does); and an optional
                     `spec_name` that overrides the specification's display
                     name, which is also the name of its output folder.
                     Answer: {ok, exit_code, receipt, artifacts: [{name,
                     bytes, content}], storage, error?} -- `storage` on a
                     successful run names the folder and the ZIP bundle the
                     next stage picks up.

    GET  /artifacts/{id}  the handoff file: the ZIP bundle of one run's
                          output folder -- seatunnel.conf and duckdb.yaml at
                          the root of the archive. 404 for an unknown id,
                          410 for one whose TTL has passed.

    GET  /rules      the active rulebook as JSON: transpiler.active_rules()
                     when the transpiler provides it, otherwise the module's
                     vocabulary constants exported from code. This route
                     never answers 500 -- a rulebook with a note attached
                     beats a stack trace.

Each request compiles in a private temp workspace: the uploads are written
into it, the compiler is pointed at it with explicit --spec, --catalog and
--output-dir, and the workspace is deleted whatever happens, so concurrent
requests cannot see each other's files.

A successful run (exit 0) is then PERSISTED for the configured TTL, which
is how the output reaches the next stage: `storage/<id>/` holds
seatunnel.conf, duckdb.yaml and `<id>.zip`, the bundle of those two, and
the response returns the id, the folder path and `/artifacts/<id>` as the
download URL. The folder is the store, the ZIP is the handoff. There is no
manifest beside them: the TTL comes from config/config.yaml, read through
`transpiler.app_config()` -- the config file belongs to the transpiler and
this module only asks -- and a folder's age is its own mtime. One clock,
one file, nothing on disk whose only job is to describe the other two.
Exit 1 and exit 2 persist nothing -- a file that failed re-validation must
not look like a deliverable, and an unreadable specification produced
nothing to deliver.

Expiry is enforced lazily: every request that touches storage first sweeps
it and deletes folders older than the TTL. No background thread, no
sweeper to supervise -- the next request after an expiry cleans up after
the last one, and a TTL test needs only a folder with an old mtime. A
config that exists but cannot be used raises ConfigError from every route
that needs it: HTTP 500 with the message, while /health stays a liveness
answer. The CLI entry point checks the same file and exits 2, as it does
for an unreadable specification.

Exit codes map the way a caller would guess: 0 -> 200; 2, the specification
could not be read, -> 422 quoting the SpecReadError paragraph; 1, a generated
file failed re-validation, -> 500, because that one is a statement about this
compiler rather than about the specification.

Run it with `python src/api.py` (serves on port 8000) or
`python -m uvicorn src.api:app` from the project root.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

# Bootstrapped before the import so the same module resolves whether this file
# is `src.api` under uvicorn from the project root or `__main__` under
# `python src/api.py`, which puts only src/ itself on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import transpiler  # noqa: E402  (path bootstrapped directly above, on purpose)

# The two artifact names, taken from the compiler rather than retyped: if the
# tool ever renames a file, the route renames with it.
SEATUNNEL_CONF_NAME = transpiler.SEATUNNEL_CONF_NAME
DUCKDB_JOBS_NAME = transpiler.DUCKDB_JOBS_NAME

#: The compiler's exit codes, in the HTTP terms a caller already thinks in.
#: 1 is a generated file that failed re-validation -- a statement about this
#: compiler, so 500. 2 is a specification nobody could read -- the caller's
#: input, so 422 with the paragraph quoted.
_STATUS_BY_EXIT_CODE: Dict[Optional[int], int] = {0: 200, 1: 500, 2: 422}

#: Serialises the compile. `contextlib.redirect_stdout` swaps `sys.stdout` for
#: the whole process, not just the calling thread, so two concurrent compiles
#: would read each other's receipts. The compiler is CPU-bound anyway, so
#: queueing costs little and keeps every receipt exactly one request's.
_RECEIPT_LOCK = threading.Lock()

#: Where a successful run is persisted, how long it survives, and what the
#: server listens on all come from config/config.yaml, read through
#: `transpiler.app_config()` -- the config file belongs to the transpiler
#: and this module only asks. Nothing here reads an environment variable or
#: repeats a default: one file, one loader (in src/transpiler.py), one place
#: to change what the service does. A file that cannot be used raises
#: ConfigError out of these helpers, which the routes turn into an HTTP 500
#: carrying the message -- never into a silent fallback, because a TTL the
#: operator believes they set and did not is the exact failure the config
#: exists to prevent.


def _config() -> Dict[str, Any]:
    """The service configuration, or ConfigError naming what is wrong."""
    return transpiler.app_config()


def _storage_dir() -> Path:
    """The storage folder, resolved against the project root.

    A relative path from the config is anchored to the project, never the
    working directory -- the same rule every default in this project
    follows -- so two servers started from different directories store to
    and sweep one folder, and the path handed to a caller is the folder the
    next request will read.
    """
    path = Path(str(_config()["storage"]["dir"])).expanduser()
    if not path.is_absolute():
        path = Path(transpiler.PROJECT_ROOT) / path
    return path


def _ttl_seconds() -> int:
    """Seconds a persisted folder survives, from the config."""
    return int(_config()["storage"]["ttl_seconds"])


#: A request id is uuid4's first twelve hex characters, so it is unambiguous
#: in a URL AND in a filename. Matching it strictly is what makes `/artifacts`
#: safe: an id is never a path, and `../` never reaches `Path` as an id.
_ARTIFACT_ID_RE = re.compile(r"^[0-9a-f]{12}$")


#: The rulebook vocabularies exported when `active_rules()` is not available.
#: These are the module-level constants the dynamic rulebook was built from;
#: names that are absent are skipped rather than fabricated.
_VOCABULARY_CONSTANTS = (
    "SUPPORTED_SCHEMA_VERSIONS",
    "VALUE_OPS",
    "DERIVED_OPS",
    "DROP_OPS",
    "STRUCTURAL_OPS",
    "KNOWN_CATEGORIES",
    "PREDICATE_OPERATORS",
    "ARITHMETIC_OPERATORS",
    "PIVOT_AGGREGATES",
    "JOIN_TYPES",
    "ORACLE_TO_PG",
    "PG_RESERVED_WORDS",
)

app = FastAPI(
    title=transpiler.TOOL_NAME,
    version=transpiler.TOOL_VERSION,
    description=(
        "The migration transpiler over HTTP: compile a specification into "
        "seatunnel.conf and duckdb.yaml, read the receipt back, fetch the "
        "compiled bundle from its TTL'd storage folder, and read the active "
        "rulebook. Findings live inside the two artifacts, as with the CLI "
        "(AGENT.md §3a) -- there is no findings route to ask for."
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _spec_filename(spec_name: Optional[str]) -> str:
    """The filename the uploaded specification gets inside its workspace.

    The name is also the output folder's name (the compiler names the folder
    after the specification's own stem), so `spec_name` is what a caller sees
    in the receipt. Sanitised to a single path segment: a display name is not
    a path, and one that arrives as `../../etc/passwd` must not be honoured
    as one.
    """
    if not spec_name or not spec_name.strip():
        return "spec.yaml"
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", spec_name.strip()).strip("-._")
    if not stem:
        return "spec.yaml"
    if not stem.lower().endswith((".yaml", ".yml")):
        stem += ".yaml"
    return stem


def _json_safe(value: Any) -> Any:
    """Coerce rulebook data to what `JSONResponse` can serialise.

    Vocabulary sets become sorted lists -- the rulebook's own convention -- and
    anything unexpectedly unserialisable becomes its string form rather than
    raising, because /rules must answer even when the rulebook is odd.
    """
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_json_safe(item) for item in value), key=str)
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _read_artifacts(output_root: Path) -> List[Dict[str, Any]]:
    """Read the generated pair back from `<output-dir>/<spec-stem>/`.

    Files that are not there are simply not listed -- the receipt in the same
    response already says which ones were written and how big, so an absent
    file stays visible instead of arriving as an empty string that could be
    mistaken for an empty artifact.
    """
    artifacts: List[Dict[str, Any]] = []
    if not output_root.is_dir():
        return artifacts
    for folder in sorted(path for path in output_root.iterdir() if path.is_dir()):
        for name in (SEATUNNEL_CONF_NAME, DUCKDB_JOBS_NAME):
            path = folder / name
            if path.is_file():
                data = path.read_bytes()
                artifacts.append(
                    {
                        "name": name,
                        "bytes": len(data),
                        "content": data.decode("utf-8", errors="replace"),
                    }
                )
    return artifacts


def _compile_in_workspace(
    spec_bytes: bytes,
    catalog_bytes: Optional[bytes],
    spec_filename: str,
) -> Dict[str, Any]:
    """Compile one upload in a private temp workspace and describe the result.

    Everything the run touches -- the uploads, the outputs -- lives under one
    `mkdtemp`, and the `finally` deletes it whatever happens, so concurrent
    requests cannot see each other's files and a failed run leaves nothing
    behind. `main()` is invoked exactly as the CLI invokes it; only the paths
    differ.
    """
    workspace = Path(tempfile.mkdtemp(prefix="transpiler-api-"))
    stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
    exit_code: Optional[int] = None
    error: Optional[str] = None
    artifacts: List[Dict[str, Any]] = []
    try:
        spec_path = workspace / spec_filename
        spec_path.write_bytes(spec_bytes)

        # Explicit --catalog so an uploaded catalog wins over any search; with
        # no upload the flag is omitted, and the compiler's own search order
        # (beside the specification, then the project root) applies unchanged.
        argv = ["--spec", str(spec_path), "--output-dir", str(workspace / "output")]
        if catalog_bytes is not None:
            catalog_path = workspace / "catalog.yaml"
            catalog_path.write_bytes(catalog_bytes)
            argv += ["--catalog", str(catalog_path)]

        try:
            with (
                _RECEIPT_LOCK,
                contextlib.redirect_stdout(stdout_buf),
                contextlib.redirect_stderr(stderr_buf),
            ):
                exit_code = int(transpiler.main(argv))
        except BaseException:
            # A request must never take the server down: argparse's
            # SystemExit and any compiler crash become a 500 with the
            # traceback quoted, the way a terminal would have shown it.
            error = traceback.format_exc()

        artifacts = _read_artifacts(workspace / "output")
    except BaseException:
        if error is None:
            error = traceback.format_exc()
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    receipt = stdout_buf.getvalue()
    ok = exit_code == 0
    if not ok and error is None:
        # Exit 2 puts the SpecReadError paragraph on stderr; exit 1 puts the
        # re-validation failures in the receipt on stdout. Quote whichever
        # one actually carries the diagnosis.
        error = (
            stderr_buf.getvalue().strip()
            or receipt.strip()
            or f"the compiler exited with code {exit_code}"
        )

    payload: Dict[str, Any] = {
        "ok": ok,
        "exit_code": exit_code,
        "receipt": receipt,
        "artifacts": artifacts,
    }
    if not ok:
        payload["error"] = error
    return payload


def _iso(epoch: float) -> str:
    """UTC timestamp in the form a response and a human both read."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _sweep_expired() -> None:
    """Delete persisted folders older than the TTL.

    Lazy by decision, not by omission: no background thread to start, stop
    or supervise, and no sweeper that dies with the process. Every request
    that touches storage cleans it first, so the request *after* an expiry
    collects what the expired run left behind.

    The clock is the folder's own mtime: the zip is the last entry written
    into it, so mtime is the moment the bundle was completed, and nothing
    afterwards touches it. That is the whole reason no manifest is needed --
    one TTL from config/config.yaml, measured against the folder itself, is
    enough to decide anything.
    """
    directory = _storage_dir()
    if not directory.is_dir():
        return
    ttl = _ttl_seconds()
    now = time.time()
    for folder in directory.iterdir():
        if not folder.is_dir():
            continue
        try:
            mtime = folder.stat().st_mtime
        except OSError:
            continue  # vanished under us; the next sweep finishes the job
        if now - mtime >= ttl:
            shutil.rmtree(folder, ignore_errors=True)


def _expired(folder: Path) -> bool:
    """Whether a persisted folder has passed its deadline.

    The same clock as the sweep -- folder mtime against the configured TTL
    -- so a folder cannot be expired for one route and fresh for the other.
    A folder whose mtime cannot be read is not expired here: "cannot see
    the clock" is not "past it", and the sweep still collects it in due
    course if the reading problem persists.
    """
    try:
        return time.time() - folder.stat().st_mtime >= _ttl_seconds()
    except OSError:
        return False


def _artifact_folder(artifact_id: str) -> Optional[Path]:
    """The folder for an id, or None when the id is not a valid one.

    The strict hex match is the whole path-traversal defence: an id that is
    not twelve lowercase hex characters never becomes a `Path` component, so
    `..%2F..%2F` and friends stop here rather than in the filesystem.
    """
    if not _ARTIFACT_ID_RE.match(artifact_id or ""):
        return None
    return _storage_dir() / artifact_id


def _persist_bundle(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Persist a successful run as a folder plus the ZIP bundle of it.

    The folder is the store, the ZIP is the handoff: `storage/<id>/` keeps
    seatunnel.conf and duckdb.yaml as plain files a person can read, and
    `<id>.zip` packs the same two at the archive root for the next stage to
    fetch in one GET. There is deliberately no third file describing them:
    the TTL is config/config.yaml's `storage.ttl_seconds`, the age is the
    folder's mtime, and the receipt -- which specification ran, how many
    jobs compiled -- is already in the response this bundle was born from.
    A manifest would be the only file on disk that nothing reads.

    Any failure removes the folder again and reports `storage_error` on the
    payload instead of failing the request: the compile succeeded and its
    artifacts are already in the response, so a full disk must not turn a
    good run into a 500 -- but it must not leave half a deliverable that
    the sweep would later mistake for a whole one either.
    """
    artifact_id = uuid.uuid4().hex[:12]
    directory = _storage_dir()
    folder = directory / artifact_id
    ttl = _ttl_seconds()
    try:
        folder.mkdir(parents=True, exist_ok=True)

        entries: List[Dict[str, Any]] = []
        for item in payload.get("artifacts") or []:
            data = str(item["content"]).encode("utf-8")
            (folder / item["name"]).write_bytes(data)
            entries.append(
                {"name": item["name"], "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            )

        # The zip goes in last, so the folder's mtime -- the clock every
        # later read judges age by -- is the moment the bundle completed.
        bundle_path = folder / f"{artifact_id}.zip"
        with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for entry in entries:
                archive.write(folder / entry["name"], arcname=entry["name"])
        bundle_data = bundle_path.read_bytes()

        created = time.time()
        return {
            "id": artifact_id,
            "folder": str(folder),
            "download_url": f"/artifacts/{artifact_id}",
            "bundle": {
                "name": bundle_path.name,
                "bytes": len(bundle_data),
                "sha256": hashlib.sha256(bundle_data).hexdigest(),
            },
            "created_at": _iso(created),
            "expires_at": _iso(created + ttl),
            "ttl_seconds": ttl,
        }
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        payload["storage_error"] = traceback.format_exc()
        return None


def _fallback_rulebook() -> Dict[str, Any]:
    """The module's vocabulary constants, exported as plain JSON.

    The defensive answer for GET /rules: until `active_rules()` exists, and
    any time it raises, the route still tells a caller what governs the
    compiler -- the same constants the dynamic rulebook was extracted from.
    """
    return {
        name: _json_safe(getattr(transpiler, name))
        for name in _VOCABULARY_CONSTANTS
        if hasattr(transpiler, name)
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> Dict[str, Any]:
    """Liveness, plus the tool's own name and version from the module."""
    return {
        "status": "ok",
        "tool": transpiler.TOOL_NAME,
        "version": transpiler.TOOL_VERSION,
    }


@app.post("/transpile")
async def transpile(
    spec: UploadFile = File(..., description="the migration specification YAML"),
    catalog: Optional[UploadFile] = File(
        None, description="the Oracle source schema catalog YAML; needed for wildcard scope"
    ),
    spec_name: Optional[str] = Form(
        None, description="overrides the specification's display name"
    ),
) -> JSONResponse:
    """Compile one specification and return the receipt plus both artifacts.

    On success the output is also persisted -- `storage` in the answer names
    the folder, the id and the ZIP the next stage fetches -- and the request
    first sweeps expired folders, which is where the configured TTL is
    enforced (see the module docstring). A config that cannot be used is
    reported before anything compiles: a run whose storage settings are
    unknown must not be half-answered with a bundle nobody can find.
    """
    spec_bytes = await spec.read()
    catalog_bytes = await catalog.read() if catalog is not None else None

    try:
        _config()
        # Lazy sweep, before this request adds one more folder to the pile:
        # the run after an expiry is what reclaims it. Runs in the threadpool
        # because it deletes files, and the loop must not wait on a slow disk.
        await run_in_threadpool(_sweep_expired)
    except transpiler.ConfigError as exc:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "exit_code": None, "error": str(exc)},
        )

    payload = await run_in_threadpool(
        _compile_in_workspace, spec_bytes, catalog_bytes, _spec_filename(spec_name)
    )

    if payload.get("exit_code") == 0:
        # ConfigError inside _persist_bundle is caught there and reported as
        # `storage_error` -- the compile succeeded, so the artifacts stay in
        # the answer either way.
        storage = await run_in_threadpool(_persist_bundle, payload)
        if storage is not None:
            payload["storage"] = storage

    return JSONResponse(
        status_code=_STATUS_BY_EXIT_CODE.get(payload["exit_code"], 500),
        content=payload,
    )


@app.get("/artifacts/{artifact_id}")
def artifact_bundle(artifact_id: str) -> Response:
    """The handoff file: the ZIP bundle of one run's output folder.

    Answered from disk, not from memory -- the point of the TTL storage is
    that the *next* stage can fetch this after the compile response is gone.
    An expired id answers 410 Gone (and deletes the folder on the way out)
    rather than 404, so a consumer can tell "too late" from "never existed"
    and react differently to each. An unknown or malformed id is 404 and
    never touches the filesystem: `_artifact_folder` refuses anything that
    is not twelve hex characters.
    """
    try:
        folder = _artifact_folder(artifact_id)
        if folder is None:
            raise HTTPException(status_code=404, detail=f"no artifact id `{artifact_id}`")

        if _expired(folder):
            shutil.rmtree(folder, ignore_errors=True)
            raise HTTPException(
                status_code=410,
                detail=(
                    f"artifact {artifact_id} outlived its "
                    f"{_ttl_seconds()}-second TTL and was deleted"
                ),
            )
        bundle_path = folder / f"{artifact_id}.zip"
        if not bundle_path.is_file():
            _sweep_expired()
            raise HTTPException(status_code=404, detail=f"no bundle for artifact {artifact_id}")
    except transpiler.ConfigError as exc:
        # The bundle's age cannot be judged without the TTL, so serving it
        # "fresh" on a broken config would be a guess. Say which file is at
        # fault instead -- the config's own argument, arriving over HTTP.
        # (HTTPException raised inside this try is not a ConfigError and
        # sails straight through, which is what keeps 404 and 410 intact.)
        raise HTTPException(status_code=500, detail=str(exc)) from None

    return FileResponse(
        bundle_path,
        media_type="application/zip",
        filename=bundle_path.name,
    )


@app.get("/artifacts/{artifact_id}/manifest")
def artifact_manifest(artifact_id: str) -> Response:
    """The old manifest endpoint, kept only as a pointer to where it went.

    There is no manifest any more: the TTL is config/config.yaml's
    `storage.ttl_seconds`, a folder's age is its own mtime, and everything
    else a manifest used to say was already in the POST /transpile answer
    this bundle was born from. A next stage written against the old contract
    deserves an answer that says where the information went rather than a
    bare FastAPI "Not Found".
    """
    raise HTTPException(
        status_code=404,
        detail=(
            "there is no manifest any more: the TTL is config/config.yaml "
            "(storage.ttl_seconds), and the bundle's metadata -- id, path, "
            f"expiry -- came back with POST /transpile. Try /artifacts/{artifact_id}"
        ),
    )


@app.get("/rules")
def rules() -> JSONResponse:
    """The active rulebook, or the built-in vocabularies -- never a 500.

    `active_rules()` is provided by the transpiler's dynamic rulebook; while
    it is absent, and whenever it raises (a malformed rules file, say), the
    module's own constants are exported instead with a note saying which
    answer the caller got. A rulebook question always has an answer, and a
    stack trace is not one.
    """
    try:
        active_rules = getattr(transpiler, "active_rules", None)
        if callable(active_rules):
            return JSONResponse(
                content={"source": "active_rules", "rules": _json_safe(active_rules())}
            )
        note = (
            "transpiler.active_rules() does not exist yet; "
            "exported the module's vocabulary constants instead."
        )
    except Exception as exc:
        note = (
            f"active_rules() raised {type(exc).__name__}: {exc}; "
            "exported the module's vocabulary constants instead."
        )
    try:
        fallback = _fallback_rulebook()
    except Exception as exc:  # pragma: no cover - belt and braces
        fallback = {}
        note = f"{note} Exporting them raised {type(exc).__name__}: {exc}."
    return JSONResponse(content={"source": "module-constants", "note": note, "rules": fallback})


if __name__ == "__main__":
    # `python src/api.py` puts src/ on sys.path, not the project root, and the
    # import string below resolves `src.api` from the root -- so add it. One
    # command starts the server; reload workers are deliberately off, because
    # a compile mid-request must not be interrupted by an edit. The address
    # comes from config/config.yaml -- the config file belongs to the
    # transpiler, so even "where do I listen" is read through it.
    import uvicorn

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        server = transpiler.app_config()["server"]
    except transpiler.ConfigError as exc:
        # The CLI's contract, kept: a config that cannot be read is a
        # message and exit 2 -- never a traceback, and never a silent start
        # on defaults the operator never chose.
        print(f"\n{exc}\n", file=sys.stderr)
        print("  The server was not started.\n", file=sys.stderr)
        raise SystemExit(2)
    uvicorn.run("src.api:app", host=server["host"], port=server["port"], log_level="info")
