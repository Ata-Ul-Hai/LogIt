import base64
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dotenv import load_dotenv

load_dotenv()

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
os.makedirs(CACHE_DIR, exist_ok=True)
CACHE_FILE = os.path.join(CACHE_DIR, "repo_cache.json")
CACHE_TTL = 86400  # 24 hours


def _get_cache_key(source: str, author_filter: str = None) -> str:
    raw = f"{source.strip().lower()}::{(author_filter or '').strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _read_cache(cache_key: str):
    if not os.path.exists(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        entry = data.get(cache_key)
        if entry and (time.time() - entry.get("timestamp", 0) < CACHE_TTL):
            return entry.get("data")
    except Exception:
        pass
    return None


def _write_cache(cache_key: str, payload: dict):
    data = {}
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    data[cache_key] = {
        "timestamp": time.time(),
        "data": payload
    }
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def parse_repo_source(source: str):
    """
    Determine if input is a local repository path or a GitHub URL.
    Returns (type, path_or_owner, repo_name)
    """
    source = source.strip()
    if not source:
        raise ValueError("Repository input is empty.")

    expanded_path = os.path.expanduser(source)
    if os.path.isdir(expanded_path):
        repo_name = os.path.basename(os.path.abspath(expanded_path))
        return "local", os.path.abspath(expanded_path), repo_name

    url = re.sub(r"\.git$", "", source).rstrip("/")
    m = re.search(r"(?:https?://)?(?:www\.)?github\.com/([^/]+)/([^/]+)", url)
    if m:
        return "github", m.group(1), m.group(2)

    parts = [p for p in url.split("/") if p]
    if len(parts) >= 2:
        return "github", parts[-2], parts[-1]

    raise ValueError(f"Could not parse repository source from: {source}")


def _make_github_request(url: str, token: str = None) -> bytes:
    """Make an HTTP GET request to GitHub API with Bearer token authentication."""
    headers = {
        "User-Agent": "OJL-Maker-App",
        "Accept": "application/vnd.github.v3+json",
    }
    github_token = token or os.environ.get("GITHUB_TOKEN")
    if github_token and github_token.strip():
        headers["Authorization"] = f"Bearer {github_token.strip()}"

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read()


def fetch_local_readme(repo_path: str) -> str:
    """Read local README.md if repository is on disk."""
    for name in ["README.md", "README", "readme.md", "Readme.md"]:
        fpath = os.path.join(repo_path, name)
        if os.path.isfile(fpath):
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    return f.read()
            except Exception:
                pass
    return ""


def fetch_local_commits(repo_path: str, max_commits: int = 250, author_filter: str = None) -> list:
    """Extract commits and changed files from a local git clone using git log."""
    cache_key = _get_cache_key(f"local:{repo_path}", author_filter)
    cached = _read_cache(cache_key)
    if cached:
        return cached

    commits = []
    try:
        cmd = [
            "git", "-C", repo_path, "log",
            f"-n{max_commits}",
            "--pretty=format:COMMIT:%h|||%ad|||%an|||%ae|||%s",
            "--name-only",
            "--date=short",
            "--reverse"
        ]
        if author_filter and author_filter.strip():
            cmd.insert(4, f"--author={author_filter.strip()}")

        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        current_commit = None
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("COMMIT:"):
                if current_commit:
                    commits.append(current_commit)
                parts = line[len("COMMIT:"):].split("|||")
                if len(parts) >= 5:
                    current_commit = {
                        "sha": parts[0],
                        "date": parts[1],
                        "author": parts[2],
                        "email": parts[3],
                        "message": parts[4],
                        "files": []
                    }
            elif current_commit and line:
                current_commit["files"].append(os.path.basename(line))
        if current_commit:
            commits.append(current_commit)
    except Exception as e:
        print(f"Local git log extraction failed: {e}")

    _write_cache(cache_key, commits)
    return commits


def fetch_github_readme(owner: str, repo: str, token: str = None) -> str:
    """Fetch and decode the repository README from GitHub API."""
    cache_key = _get_cache_key(f"github_readme:{owner}/{repo}")
    cached = _read_cache(cache_key)
    if cached:
        return cached

    content = ""
    url = f"https://api.github.com/repos/{owner}/{repo}/readme"
    try:
        raw = _make_github_request(url, token)
        data = json.loads(raw.decode("utf-8"))
        if "content" in data:
            content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except Exception:
        pass

    if not content:
        for branch in ["main", "master"]:
            try:
                raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/README.md"
                headers = {"User-Agent": "OJL-Maker-App"}
                github_token = token or os.environ.get("GITHUB_TOKEN")
                if github_token:
                    headers["Authorization"] = f"Bearer {github_token.strip()}"
                req = urllib.request.Request(raw_url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    content = resp.read().decode("utf-8", errors="replace")
                    break
            except Exception:
                continue

    if content:
        _write_cache(cache_key, content)
    return content


def fetch_commits_atom(owner: str, repo: str, author_filter: str = None) -> list:
    """Fallback: fetch commits from GitHub public Atom feed."""
    commits = []
    try:
        url = f"https://github.com/{owner}/{repo}/commits.atom"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8", errors="replace")
            tree = ET.fromstring(content)
            ns = "{http://www.w3.org/2005/Atom}"
            for entry in tree.findall(f"{ns}entry"):
                title_elem = entry.find(f"{ns}title")
                updated_elem = entry.find(f"{ns}updated")
                author_elem = entry.find(f"{ns}author/{ns}name")
                
                title = title_elem.text.strip() if title_elem is not None and title_elem.text else ""
                date = updated_elem.text.strip() if updated_elem is not None and updated_elem.text else ""
                author = author_elem.text.strip() if author_elem is not None and author_elem.text else "Developer"
                
                if author_filter and author_filter.strip().lower() not in author.lower():
                    continue

                if title:
                    commits.append({
                        "sha": "",
                        "date": date,
                        "author": author,
                        "message": title,
                        "files": []
                    })
    except Exception as e:
        pass

    commits.reverse()
    return commits


def fetch_github_commits(owner: str, repo: str, token: str = None, author_filter: str = None, max_pages: int = 3) -> list:
    """Fetch commits via GitHub REST API (supports author filtering and private repos with token)."""
    cache_key = _get_cache_key(f"github_commits:{owner}/{repo}", author_filter)
    cached = _read_cache(cache_key)
    if cached:
        return cached

    commits = []
    try:
        for page in range(1, max_pages + 1):
            url = f"https://api.github.com/repos/{owner}/{repo}/commits?per_page=100&page={page}"
            if author_filter and author_filter.strip():
                url += f"&author={urllib.parse.quote(author_filter.strip())}"

            raw = _make_github_request(url, token)
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, list) or len(data) == 0:
                break
            for item in data:
                commit_info = item.get("commit", {})
                message = commit_info.get("message", "").strip()
                author_date = commit_info.get("author", {}).get("date", "")
                author_name = commit_info.get("author", {}).get("name", "")
                sha = item.get("sha", "")[:7]
                commits.append({
                    "sha": sha,
                    "date": author_date,
                    "author": author_name,
                    "message": message,
                    "files": []
                })
        commits.reverse()
    except Exception as e:
        github_token = token or os.environ.get("GITHUB_TOKEN")
        if not github_token:
            commits = fetch_commits_atom(owner, repo, author_filter=author_filter)
        else:
            raise ValueError(f"Could not access private repository. Verify your token has 'repo' access: {e}")

    _write_cache(cache_key, commits)
    return commits


def extract_tech_stack_from_readme(readme: str) -> list:
    """Extract detected technologies and libraries from README."""
    common_tools = [
        "Python", "JavaScript", "TypeScript", "React", "Vue", "Next.js", "Node.js", "Express",
        "FastAPI", "Flask", "Django", "HTML5", "CSS3", "Tailwind CSS", "Bootstrap",
        "PostgreSQL", "MySQL", "SQLite", "MongoDB", "Redis", "Docker", "Git", "GitHub Actions",
        "REST API", "GraphQL", "PyTorch", "TensorFlow", "Pandas", "NumPy", "ReportLab", "PyPDF2",
        "Linux", "VS Code", "Postman", "Jest", "Pytest"
    ]
    detected = []
    readme_lower = readme.lower()
    for tool in common_tools:
        if re.search(r"\b" + re.escape(tool.lower()) + r"\b", readme_lower):
            detected.append(tool)

    return detected if detected else ["Git", "VS Code", "Python", "REST API", "Linux"]
