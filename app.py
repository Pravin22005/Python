import os
import subprocess
import sys
import tempfile

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
    url = f"https://api.github.com/repos/{owner}/{repo}/contents"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "python-playground",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            items = json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise HTTPException(error.code, "Could not load repository files")
    except urllib.error.URLError:
        raise HTTPException(503, "Repository file service is unavailable")

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

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as source:
        source.write(request.code)
        source_path = source.name

    try:
        result = subprocess.run(
            [sys.executable, "-I", "-B", source_path],
            capture_output=True,
            text=True,
            timeout=5,
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
        return {"stdout": "", "stderr": "Execution timed out after 5 seconds.", "exit_code": 124}
    finally:
        os.unlink(source_path)


@app.post("/api/save")
def save_as_pull_request(request: SaveRequest):
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise HTTPException(503, "GITHUB_TOKEN is not configured")

    import base64
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
