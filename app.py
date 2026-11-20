import os
import subprocess
import sys
import tempfile
import ast
import base64
import json
import urllib.parse

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Python Playground API")

DEFAULT_ALLOWED_ORIGINS = {
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "https://pravin22kumar.github.io",
}


def allowed_origins():
    configured = os.getenv("ALLOWED_ORIGINS", "")
    return sorted(
        DEFAULT_ALLOWED_ORIGINS
        | {
            origin.strip().rstrip("/")
            for origin in configured.split(",")
            if origin.strip()
        }
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/files")
def list_repository_files(search: str = ""):
    import json
    import urllib.error
    import urllib.request

    owner = os.getenv("GITHUB_OWNER", "pravin22kumar")
    repo = os.getenv("GITHUB_REPO", "Python")
    token = os.getenv("GITHUB_TOKEN")
    url = f"https://api.github.com/repos/{owner}/{repo}/contents"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "python-playground",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            items = json.loads(response.read())
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read().decode("utf-8", "replace"))
            message = detail.get("message", "Could not load repository files")
        except (ValueError, UnicodeDecodeError):
            message = "Could not load repository files"
        raise HTTPException(error.code, f"GitHub repository files: {message}")
    except urllib.error.URLError as error:
        raise HTTPException(503, f"Repository file service is unavailable: {error.reason}")

    query = search.strip().lower()
    return [
        {
            "name": item["name"],
            "type": item["type"],
            "download_url": item.get("download_url"),
        }
        for item in items
        if item.get("type") == "file"
        and item.get("name", "").endswith(".py")
        and item.get("name", "").lower() != "app.py"
        and (not query or query in item.get("name", "").lower())
    ]


@app.get("/api/files/check")
def check_file_name(name: str):
    normalized = name.strip().lower()
    files = list_repository_files()
    return {
        "exists": any(item["name"].lower() == normalized for item in files),
        "allowed": normalized.endswith(".py") and normalized != "app.py",
    }


class RunRequest(BaseModel):
    code: str
    stdin: str = ""
    timeout: int = Field(default=5, ge=1, le=30)
    memory_mb: int = Field(default=128, ge=32, le=512)
    network_disabled: bool = True
    test_framework: str = "none"
    dependencies: list[str] = Field(default_factory=list)


class SaveRequest(BaseModel):
    path: str
    content: str
    title: str = "Update Python file"
    base: str = "main"
    changes: list[dict] = Field(default_factory=list)


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.post("/api/run")
def run_code(request: RunRequest):
    if len(request.code) > 50_000:
        raise HTTPException(400, "Code exceeds the 50 KB limit")

    if len(request.stdin) > 10_000:
        raise HTTPException(400, "Standard input exceeds the 10 KB limit")
    if request.test_framework not in {"none", "unittest", "pytest"}:
        raise HTTPException(400, "Unsupported test framework")

    allowed_dependencies = {
        item.strip().lower()
        for item in os.getenv("ALLOWED_PACKAGES", "").split(",")
        if item.strip()
    }
    unknown_dependencies = sorted({
        dependency.lower()
        for dependency in request.dependencies
        if dependency.lower() not in allowed_dependencies
    })
    if unknown_dependencies:
        raise HTTPException(
            400,
            "Dependencies are not allowlisted: " + ", ".join(unknown_dependencies),
        )

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as source:
        bootstrap = ""
        if request.network_disabled:
            bootstrap = (
                "import sys\n"
                "def _block_network(event, args):\n"
                "    if event.startswith('socket.'):\n"
                "        raise PermissionError('Network access is disabled for this run')\n"
                "sys.addaudithook(_block_network)\n"
            )
        bootstrap += (
            "import resource\n"
            f"_limit = {request.memory_mb} * 1024 * 1024\n"
            "try:\n"
            "    resource.setrlimit(resource.RLIMIT_AS, (_limit, _limit))\n"
            "except (AttributeError, ValueError, OSError):\n"
            "    pass\n"
        )
        source.write(bootstrap)
        source.write(request.code)
        if request.test_framework == "unittest":
            source.write(
                "\n\nimport unittest\n"
                "_suite = unittest.defaultTestLoader.loadTestsFromModule("
                "sys.modules[__name__])\n"
                "_result = unittest.TextTestRunner().run(_suite)\n"
                "raise SystemExit(0 if _result.wasSuccessful() else 1)\n"
            )
        source_path = source.name

    try:
        argv = [sys.executable, "-I", "-B", source_path]
        if request.test_framework == "pytest":
            argv = [sys.executable, "-I", "-B", "-m", "pytest", "-q", source_path]
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            input=request.stdin,
            timeout=request.timeout,
            cwd=tempfile.gettempdir(),
            env={
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": "",
                "PYTHONIOENCODING": "utf-8",
            },
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": f"Execution timed out after {request.timeout} seconds.",
            "exit_code": 124,
        }
    finally:
        os.unlink(source_path)


@app.post("/api/lint")
def lint_code(request: RunRequest):
    if len(request.code) > 50_000:
        raise HTTPException(400, "Code exceeds the 50 KB limit")
    try:
        ast.parse(request.code)
    except SyntaxError as error:
        return {
            "valid": False,
            "errors": [{
                "line": error.lineno or 1,
                "column": (error.offset or 1),
                "message": error.msg,
            }],
        }
    return {"valid": True, "errors": []}


@app.post("/api/format")
def format_code(request: RunRequest):
    if len(request.code) > 50_000:
        raise HTTPException(400, "Code exceeds the 50 KB limit")
    try:
        tree = ast.parse(request.code)
    except SyntaxError as error:
        raise HTTPException(
            400,
            f"Syntax error on line {error.lineno or 1}: {error.msg}",
        )
    return {"code": ast.unparse(tree)}


@app.post("/api/save")
def save_as_pull_request(request: SaveRequest):
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise HTTPException(503, "GITHUB_TOKEN is not configured")

    import base64
    import json
    import secrets
    import urllib.error
    import urllib.parse
    import urllib.request

    owner = os.getenv("GITHUB_OWNER", "pravin22kumar")
    repo = os.getenv("GITHUB_REPO", "Python")
    def safe_path(value):
        path = value.replace("\\", "/").lstrip("/")
        if not path.endswith(".py") or ".." in path.split("/"):
            raise HTTPException(400, "Only .py files below the repository root are accepted")
        return path

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "python-playground",
    }

    def github_json(method, url, payload=None):
        data = json_bytes(payload)
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                body = response.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            raise HTTPException(error.code, f"GitHub API error: {detail}")
        except urllib.error.URLError as error:
            raise HTTPException(502, f"GitHub API is unavailable: {error.reason}")

    def github_optional_json(method, url, payload=None):
        data = json_bytes(payload)
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                body = response.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return {}
            detail = error.read().decode("utf-8", "replace")
            raise HTTPException(error.code, f"GitHub API error: {detail}")
        except urllib.error.URLError as error:
            raise HTTPException(502, f"GitHub API is unavailable: {error.reason}")

    def json_bytes(payload):
        import json
        return json.dumps(payload).encode() if payload is not None else None

    base_url = f"https://api.github.com/repos/{owner}/{repo}"
    base_branch = github_json("GET", f"{base_url}/branches/{urllib.parse.quote(request.base, safe='')}")
    branch = f"python-playground/{secrets.token_hex(6)}"
    github_json(
        "POST",
        f"{base_url}/git/refs",
        {"ref": f"refs/heads/{branch}", "sha": base_branch["commit"]["sha"]},
    )

    changes = request.changes or [{"type": "modified", "path": request.path, "content": request.content}]
    summary = []
    for change in changes:
        change_type = change.get("type", "modified")
        path = safe_path(change.get("path", ""))
        previous_path = safe_path(change["previous_path"]) if change.get("previous_path") else None
        target_path = previous_path if change_type == "renamed" else path
        existing = github_optional_json(
            "GET",
            f"{base_url}/contents/{urllib.parse.quote(target_path, safe='/')}?ref={urllib.parse.quote(branch, safe='')}",
        )
        sha = existing.get("sha")
        if change_type == "deleted" or change_type == "renamed":
            if sha:
                github_json(
                    "DELETE",
                    f"{base_url}/contents/{urllib.parse.quote(target_path, safe='/')}",
                    {"message": f"Delete {target_path}", "sha": sha, "branch": branch},
                )
            if change_type == "deleted":
                summary.append(f"D {target_path}")
                continue
        content = change.get("content", "")
        encoded = base64.b64encode(content.encode()).decode()
        github_json(
            "PUT",
            f"{base_url}/contents/{urllib.parse.quote(path, safe='/')}",
            {
                "message": f"Update {path}",
                "content": encoded,
                "branch": branch,
                **({"sha": sha} if change_type != "renamed" and sha else {}),
            },
        )
        summary.append(f"{change_type[0].upper()} {path}")

    pull_request = github_json(
        "POST",
        f"{base_url}/pulls",
        {
            "title": request.title,
            "head": branch,
            "base": request.base,
            "body": "Updated by the Python Playground UI.\n\nChanges:\n" + "\n".join(f"- {item}" for item in summary),
        },
    )
    return {"pull_request_url": pull_request["html_url"], "branch": branch}
