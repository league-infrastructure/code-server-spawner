from cspawn.models import CodeHost

import os
import re
import threading
import time
from typing import Optional, Mapping, Any, TYPE_CHECKING

from github import Github, GithubException


# Per-upstream-URL fork locks: serializes concurrent create_fork calls for the
# same upstream so GitHub does not return 403 "already being forked" or 429.
_fork_locks: dict[str, threading.Lock] = {}
_fork_locks_mu = threading.Lock()


def _get_fork_lock(upstream_url: str) -> threading.Lock:
    """Return (creating if necessary) the per-upstream threading.Lock."""
    with _fork_locks_mu:
        if upstream_url not in _fork_locks:
            _fork_locks[upstream_url] = threading.Lock()
        return _fork_locks[upstream_url]


if TYPE_CHECKING:
    from cspawn.init import App

class CodeHostRepo:
    def __init__(self, codehost: CodeHost, app: "App"):
        self.codehost = codehost
        self.app = app
        self.username = codehost.user.username if codehost.user else None
        self.service_name = codehost.service_name
        self.container_id = codehost.container_id
        self.container_name = codehost.container_name
        self.class_proto = codehost.class_proto
        self.class_ = codehost.class_
        self.node_id = codehost.node_id
        self.node_name = codehost.node_name
        # Add more fields as needed

    @classmethod
    def new_codehostrepo(cls, app, username):
        with app.app_context():
            ch = CodeHost.query.filter_by(service_name=username).first()
            if not ch:
                ch = CodeHost.query.join("user").filter_by(username=username).first()
            if not ch:
                raise ValueError(f"No CodeHost found for username: {username}")
            return cls(ch, app)

    def _get_service_container(self) -> "App":
        # Use app.csm to get the container object
        service = self.app.csm.get(self.service_name)
        if service is None:
            raise ValueError(f"No service found for {self.service_name}")

        containers = list(service.containers)
        if not containers:
            raise ValueError(f"No containers found for service {self.service_name}")
        return service, containers[0]

    def _git_environment(self):
        # Only GITHUB_TOKEN is needed from config/env
        token = None
        if self.app and hasattr(self.app, "app_config"):
            token = self.app.app_config.get("GITHUB_TOKEN")
        if not token:
            token = os.getenv("GITHUB_TOKEN")
        if not token:
            raise ValueError("GITHUB_TOKEN is not configured for git operations")
        return {"GITHUB_TOKEN": token}

    def push(self, branch: str = "master", timeout: Optional[float] = None) -> int:
        """Push local changes from the codehost's container to GitHub.

        Args:
            branch: Remote branch to push to.
            timeout: Seconds to allow the underlying ``docker exec`` subprocess
                to run before aborting. Defaults to the
                ``CODEHOST_PUSH_TIMEOUT_S`` config key (falling back to 30s)
                so a wedged SSH/docker-exec never hangs the calling thread
                forever.
        """
        import subprocess

        effective_timeout = timeout or self.app.app_config.get("CODEHOST_PUSH_TIMEOUT_S", 30)

        service, container = self._get_service_container()
        env = self._git_environment()

        repo = service.env['JTL_REPO']
        owner, repo_name = _parse_repo(repo)
        # Token comes in via `docker exec -e GITHUB_TOKEN=...`, referenced as a
        # shell var so it never appears in the process argument list.
        remote = f"https://x-access-token:${{GITHUB_TOKEN}}@github.com/{owner}/{repo_name}.git"

        refspec = f" {branch}" if branch else ""

        # GIT_TERMINAL_PROMPT=0 prevents git from blocking on an interactive
        # username prompt if the token is ever rejected.
        cmd = (
            f'cd "$WORKSPACE_FOLDER" && export GIT_TERMINAL_PROMPT=0 && '
            f'git commit -a -m"Automated commit" || true && git push "{remote}"{refspec}'
        )

        # Exec via the docker CLI, not docker-py's exec_run: exec_run over the
        # SSH transport throws BrokenPipeError (the connection dies between the
        # service inspect and the exec hijack). The docker CLI handles SSH
        # robustly — this is the same `docker -H ssh://... exec` that works by
        # hand. The container's node was attached in CodeServerManager.containers().
        node_host = container.node.attrs["Description"]["Hostname"]
        node_uri = f"ssh://root@{self.app.app_config['NODE_HOSTNAME_TEMPLATE'].format(nodename=node_host)}"
        self.app.logger.info(f"Executing git push for {self.username} on {node_host} ({node_uri})")

        argv = [
            "docker", "-H", node_uri, "exec", "-u", "vscode",
            "-e", f"GITHUB_TOKEN={env['GITHUB_TOKEN']}",
            container.id, "sh", "-c", cmd,
        ]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=effective_timeout)
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"git push timed out after {effective_timeout}s for {self.username} on {node_host}"
            ) from e
        if proc.stdout and proc.stdout.strip():
            print(proc.stdout.strip())
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"git push failed (rc={proc.returncode}): {err[-500:]}")

        return proc.returncode

    def pull(self, branch: str = "master", rebase: bool = True, dry_run: bool = False) -> int:
        """Pull changes from GitHub into the codehost's container."""
        remote = f"https://x-access-token:${{GITHUB_TOKEN}}@github.com/{self.service_name}.git"
        rebase_flag = " --rebase" if rebase else ""
        refspec = f" {branch}" if branch else ""
        cmd = f'cd "$WORKSPACE_FOLDER" && git pull{rebase_flag} "{remote}"{refspec}'

        container = self._get_container()
        env = self._git_environment()

        if dry_run:
            print(f"Would execute on container {container.id[:12]}: {cmd}")
            return 0
        result = container.o.exec_run(
            cmd=["sh", "-c", cmd],
            environment=env,
            user="vscode",
            stream=True,
            demux=True,
        )
        if result.output:
            for stdout, stderr in result.output:
                if stdout:
                    print(stdout.decode().rstrip())
                if stderr:
                    msg = stderr.decode().rstrip()
                    if msg:
                        print(f"ERROR: {msg}")
        exit_code = getattr(result, "exit_code", None)
        if exit_code is None:
            exit_code = 0
        if exit_code != 0:
            raise RuntimeError(f"git pull failed with exit code {exit_code}")
        return exit_code



def _parse_repo(url: str) -> tuple[str, str]:
    """Return (owner, name) for a GitHub repo URL or "owner/name" string."""
    m = re.match(r"^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if m:
        return m.group(1), m.group(2)
    if "/" in url and not url.startswith("http"):
        owner, name = url.split("/", 1)
        return owner, name
    
    raise ValueError(f"Unrecognized repo format: {url}")


class StudentRepo:


    def __init__(
        self,
        config: Optional[Mapping[str, Any]],
        app: Optional["App"],
        org: str,
        name: str,
        upstream_name: str,
        upstream_url: str,
        username: str,
    ) -> None:
        self.config = config
        self.app = app
        self.org = org
        self.name = name
        self.upstream_name = upstream_name
        self.upstream_url = upstream_url
        self.username = username

    @property
    def full_name(self) -> str:
        return f"{self.org}/{self.name}"

    @property
    def html_url(self) -> str:
        # Use PyGithub to get repo html_url if possible
        try:
            gh = Github(self._resolve_token())
            repo = gh.get_repo(self.full_name)
            return repo.html_url
        except Exception:
            return f"https://github.com/{self.full_name}"

    def push(self, branch: Optional[str] = "master", dry_run: bool = False) -> int:
        """Push local changes from the student's container to GitHub."""
        return self._run_git_command(
            command=self._build_push_command(branch),
            dry_run=dry_run,
        )

    def pull(
        self,
        branch: Optional[str] = "master",
        rebase: bool = True,
        dry_run: bool = False,
    ) -> int:
        """Pull changes from GitHub into the student's container."""
        return self._run_git_command(
            command=self._build_pull_command(branch=branch, rebase=rebase),
            dry_run=dry_run,
        )

    def _build_push_command(self, branch: Optional[str]) -> str:
        remote = self._remote_url_template
        refspec = f" {branch}" if branch else ""
        return f'cd "$WORKSPACE_FOLDER" && git push "{remote}"{refspec}'

    def _build_pull_command(self, branch: Optional[str], rebase: bool) -> str:
        remote = self._remote_url_template
        rebase_flag = " --rebase" if rebase else ""
        refspec = f" {branch}" if branch else ""
        return f'cd "$WORKSPACE_FOLDER" && git pull{rebase_flag} "{remote}"{refspec}'

    def get_info_dict(self, token=None):
        """Return info about this repo as a dict using PyGithub."""
        from github import Github
        gh = Github(token or self._resolve_token())
        info = {
            "repo_url": f"https://github.com/{self.full_name}",
            "exists": False,
            "description": None,
            "private": None,
            "created_at": None,
            "pushed_at": None,
        }
        try:
            repo = gh.get_repo(self.full_name)
            info["repo_url"] = repo.html_url
            info["exists"] = True
            info["description"] = repo.description
            info["private"] = repo.private
            info["created_at"] = repo.created_at
            info["pushed_at"] = repo.pushed_at
        except Exception:
            pass
        return info

    @property
    def _remote_url_template(self) -> str:
        return f"https://x-access-token:${{GITHUB_TOKEN}}@github.com/{self.full_name}.git"

    def _run_git_command(self, command: str, dry_run: bool) -> int:
        _, container = self._get_service_and_container()
        env = self._git_environment()

        if dry_run:
            print(f"Would execute on container {container.id[:12]}: {command}")
            return 0

        result = container.o.exec_run(
            cmd=["sh", "-c", command],
            environment=env,
            user="vscode",
            stream=True,
            demux=True,
        )

        if result.output:
            for stdout, stderr in result.output:
                if stdout:
                    print(stdout.decode().rstrip())
                if stderr:
                    msg = stderr.decode().rstrip()
                    if msg:
                        print(f"ERROR: {msg}")

        exit_code = getattr(result, "exit_code", None)
        if exit_code is None:
            exit_code = 0

        if exit_code != 0:
            raise RuntimeError(f"git command failed with exit code {exit_code}")

        return exit_code

    def _get_service_and_container(self):
        if self.app is None or not hasattr(self.app, "csm"):
            raise ValueError("StudentRepo is missing application context for git operations")

        service = self.app.csm.get_by_username(self.username)
        if not service:
            raise ValueError(f"No service found for username: {self.username}")

        containers = list(service.containers)
        if not containers:
            raise ValueError(f"No containers found for service {service.name}")

        return service, containers[0]

    def _git_environment(self) -> Mapping[str, str]:
        token = self._resolve_token()
        return {"GITHUB_TOKEN": token}

    def _resolve_token(self) -> str:
        candidates = []
        if self.config is not None:
            candidates.append(self.config)
        if self.app is not None and hasattr(self.app, "app_config"):
            candidates.append(self.app.app_config)

        for cfg in candidates:
            if hasattr(cfg, "get"):
                token = cfg.get("GITHUB_TOKEN")
            else:
                token = cfg["GITHUB_TOKEN"] if "GITHUB_TOKEN" in cfg else None
            if token:
                return token

        token = os.getenv("GITHUB_TOKEN")
        if token:
            return token

        raise ValueError("GITHUB_TOKEN is not configured for git operations")


class GithubOrg:

    @staticmethod
    def new_org(app: "App") -> "GithubOrg":
        cfg  = app.app_config

        org = cfg.get("GITHUB_ORG")
        token = cfg.get("GITHUB_ORG_TOKEN") or cfg.get("GITHUB_TOKEN")
        if not org or not token:
            raise ValueError("GITHUB_ORG and GITHUB_ORG_TOKEN must be set in config or env")
        return GithubOrg(app=app, org=org, token=token, config=cfg)


    def __init__(
        self,
        app: Optional["App"],
        org: Optional[str] = None,
        token: Optional[str] = None,
        config: Optional[Mapping[str, Any]] = None,
    ):
        # Prefer config values when provided; fall back to explicit args/env
        effective_config = config or (getattr(app, "app_config", None))
        cfg_org = None
        cfg_token = None
        if effective_config is not None:
            cfg_org = effective_config.get("GITHUB_ORG")
            cfg_token = effective_config.get("GITHUB_ORG_TOKEN") or effective_config.get("GITHUB_TOKEN")

        eff_org = org or cfg_org
        eff_token = token or cfg_token

        if not eff_org or not eff_token:
            raise ValueError("GithubOrg requires both an organization and access token")

        self.org = eff_org.rstrip("/").split("/")[-1]
        self.token = eff_token
        self.config = effective_config
        self.app = app
        self.gh = Github(self.token)
        self._org_obj = self.gh.get_organization(self.org)

    def _org_repo_name(self, upstream_url: str, username: str) -> tuple[str, str]:
        _, base = _parse_repo(upstream_url)
        return f"{base}-{username}", base

    def fork(self, upstream_url: str, username: str, private: bool = False) -> StudentRepo:
        """Fork upstream into this org with -username suffix; idempotent."""
        owner, name = _parse_repo(upstream_url)
        target_name, upstream_name = self._org_repo_name(upstream_url, username)

        # If it already exists, return it
        if self._repo_exists(self.org, target_name):
            self.app.logger.info(f"Repo {self.org}/{target_name} already exists; skipping fork")
            return StudentRepo(self.config, self.app, self.org, target_name, upstream_name, upstream_url, username)

        # Fork the upstream DIRECTLY to the per-student target name. GitHub's
        # create_fork supports a custom `name=`, so each student gets its own
        # uniquely-named fork with no shared intermediate repo and no rename.
        # This eliminates the prior fork->rename race: concurrent students used
        # to collide on a single `<org>/<upstream-name>` repo, so some renames
        # lost the race, leaving orphaned forks and "<target> not ready in
        # time" timeouts. The per-upstream lock + retry remain as defense
        # against 429 (rate limit) / 403 ("already being forked") around the
        # create_fork POST.
        upstream_repo = self.gh.get_repo(f"{owner}/{name}")
        fork_lock = _get_fork_lock(upstream_url)
        _MAX_FORK_ATTEMPTS = 8
        _FORK_BACKOFF_START = 2   # seconds
        _FORK_BACKOFF_CAP = 30    # seconds
        with fork_lock:
            backoff = _FORK_BACKOFF_START
            for attempt in range(1, _MAX_FORK_ATTEMPTS + 1):
                try:
                    upstream_repo.create_fork(
                        organization=self.org, name=target_name, default_branch_only=True
                    )
                    break  # success
                except GithubException as exc:
                    retryable = (
                        exc.status == 429
                        or (exc.status == 403 and "already being forked" in str(exc.data))
                    )
                    if not retryable or attempt == _MAX_FORK_ATTEMPTS:
                        raise
                    self.app.logger.warning(
                        f"create_fork attempt {attempt}/{_MAX_FORK_ATTEMPTS} "
                        f"got {exc.status}; retrying in {backoff}s"
                    )
                    time.sleep(backoff)
                    backoff = min(backoff * 2, _FORK_BACKOFF_CAP)

        # Wait for the (directly-named) fork to be ready.
        self._wait_repo_ready(self.org, target_name)

        if private:
            try:
                self.gh.get_repo(f"{self.org}/{target_name}").edit(private=True)
            except Exception as e:
                self.app.logger.warning(f"Could not set {self.org}/{target_name} private: {e}")

        return StudentRepo(self.config, self.app, self.org, target_name, upstream_name, upstream_url, username)

    def remove(self, upstream_or_fullname: str, username: Optional[str] = None) -> bool:
        """Delete a student repo. Accepts an upstream URL+username or full "org/name"."""
        if username:
            target_name, _ = self._org_repo_name(upstream_or_fullname, username)
            full = f"{self.org}/{target_name}"
        else:
            if "/" in upstream_or_fullname:
                full = upstream_or_fullname.split("github.com/")[-1]
            else:
                full = upstream_or_fullname
        full = full.strip("/")
        try:
            repo = self.gh.get_repo(full)
            repo.delete()
            return True
        except Exception as e:
            if "Not Found" in str(e):
                return False
            raise RuntimeError(f"Delete failed: {e}")

    def get_repo(self, upstream_url: str, username: str) -> Optional[StudentRepo]:
        target_name, upstream_name = self._org_repo_name(upstream_url, username)
        if self._repo_exists(self.org, target_name):
            return StudentRepo(self.config, self.app, self.org, target_name, upstream_name, upstream_url, username)
        return None

    def _repo_exists(self, owner: str, name: str) -> bool:
        try:
            self.gh.get_repo(f"{owner}/{name}")
            return True
        except Exception:
            return False

    def _wait_repo_ready(self, owner: str, name: str, timeout: int = 180) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.gh.get_repo(f"{owner}/{name}")
                return
            except Exception:
                time.sleep(2)
        raise TimeoutError(f"Repo {owner}/{name} not ready in time")

    def _rename_with_retry(self, owner: str, current_name: str, target_name: str, private: bool = False, retries: int = 8) -> None:
        backoff = 1
        last_exc = None
        for _ in range(retries):
            try:
                repo = self.gh.get_repo(f"{owner}/{current_name}")
                repo.edit(name=target_name, private=private)
                return
            except Exception as exc:
                last_exc = exc
                # If target already exists, consider operation complete if it matches desired state
                if "already exists" in str(exc).lower() and self._repo_exists(owner, target_name):
                    return
                if "conflicting repository operation" in str(exc).lower():
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    continue
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
        raise RuntimeError(f"Rename failed after retries: {last_exc}")
