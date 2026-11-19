import io
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="Python Playground API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in os.getenv(
            "ALLOWED_ORIGINS",
            "http://localhost:8000,http://127.0.0.1:8000",
        ).split(",")
        if origin.strip()
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


class RunRequest(BaseModel):
    code: str


class SaveRequest(BaseModel):
    path: str
    content: str
    title: str = "Update Python file"
    base: str = "main"


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
    path = request.path.replace("\\", "/").lstrip("/")
    if not path.endswith(".py") or ".." in path.split("/"):
        raise HTTPException(400, "Only .py files below the repository root are accepted")

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

    existing = github_optional_json(
        "GET",
        f"{base_url}/contents/{urllib.parse.quote(path, safe='/')}?ref={urllib.parse.quote(branch, safe='')}",
    )
    sha = existing.get("sha")
    encoded = base64.b64encode(request.content.encode()).decode()
    github_json(
        "PUT",
        f"{base_url}/contents/{urllib.parse.quote(path, safe='/')}",
        {
            "message": f"Update {path}",
            "content": encoded,
            "branch": branch,
            **({"sha": sha} if sha else {}),
        },
    )

    pull_request = github_json(
        "POST",
        f"{base_url}/pulls",
        {
            "title": request.title,
            "head": branch,
            "base": request.base,
            "body": f"Updated by the Python Playground UI.\n\nFile: `{path}`",
        },
    )
    return {"pull_request_url": pull_request["html_url"], "branch": branch}
