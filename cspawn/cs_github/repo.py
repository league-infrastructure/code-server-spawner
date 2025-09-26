from cspawn.models import CodeHost

import os
import re
from typing import Optional, Mapping, Any, TYPE_CHECKING

from github import Github


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

    def push(self, branch: str = "master") -> int:
        """Push local changes from the codehost's container to GitHub."""

        service, container = self._get_service_container()
        env = self._git_environment()
   
        repo = service.env['JTL_REPO']

        owner, repo_name = _parse_repo(repo)
        remote = f"https://x-access-token:{env['GITHUB_TOKEN']}@github.com/{owner}/{repo_name}.git"

     
        refspec = f" {branch}" if branch else ""

        cmd = f'cd "$WORKSPACE_FOLDER" && git commit -a -m"Automated commit" || true && git push "{remote}"{refspec}'
        self.app.logger.info(f"Executing git push for {self.username}: {cmd}")
     
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
            raise RuntimeError(f"git push failed with exit code {exit_code}")
        
        return exit_code

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

        # Create a fork under the org
        upstream_repo = self.gh.get_repo(f"{owner}/{name}")
        # Create a shallow fork with only the last commit (default_branch_only=True)
        forked_repo = upstream_repo.create_fork(organization=self.org, default_branch_only=True)

        # Wait for fork to be ready
        self._wait_repo_ready(self.org, name)

        # If target already exists (a previous run progressed further), treat as done
        if self._repo_exists(self.org, target_name):
            self.app.logger.info(f"Repo {self.org}/{target_name} already exists after fork; skipping rename")
            return StudentRepo(self.config, self.app, self.org, target_name, upstream_name, upstream_url, username)

        # Rename with retries to handle GitHub background operations
        if name != target_name:
            self.app.logger.info(f"Renaming {self.org}/{name} to {self.org}/{target_name}")
            self._rename_with_retry(self.org, name, target_name, private=private)
            self._wait_repo_ready(self.org, target_name)

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
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.gh.get_repo(f"{owner}/{name}")
                return
            except Exception:
                time.sleep(2)
        raise TimeoutError(f"Repo {owner}/{name} not ready in time")

    def _rename_with_retry(self, owner: str, current_name: str, target_name: str, private: bool = False, retries: int = 8) -> None:
        import time
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
