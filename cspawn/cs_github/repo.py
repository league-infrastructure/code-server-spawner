from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional, Mapping, Any

import requests



def _parse_repo(url: str) -> tuple[str, str]:
    """Return (owner, name) for a GitHub repo URL or "owner/name" string."""
    m = re.match(r"^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if m:
        return m.group(1), m.group(2)
    if "/" in url and not url.startswith("http"):
        owner, name = url.split("/", 1)
        return owner, name
    raise ValueError(f"Unrecognized repo format: {url}")


@dataclass
class StudentRepo:
    org: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.org}/{self.name}"

    @property
    def html_url(self) -> str:
        return f"https://github.com/{self.full_name}"


class GithubOrg:
    def __init__(self, org: Optional[str] = None, token: Optional[str] = None, config: Optional[Mapping[str, Any]] = None):
        # Prefer config values when provided; fall back to explicit args/env
        cfg_org = None
        cfg_token = None
        if config is not None:
            cfg_org = config.get("GITHUB_ORG")
            # Prefer org token, fallback to legacy token key
            cfg_token = config.get("GITHUB_ORG_TOKEN") or config.get("GITHUB_TOKEN")

        eff_org = org or cfg_org 
        eff_token = token or cfg_token

        self.org = eff_org.rstrip("/").split("/")[-1]
        self.token = eff_token
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "code-server-spawner",
        })

    def _org_repo_name(self, upstream_url: str, username: str) -> str:
        _, base = _parse_repo(upstream_url)
        return f"{base}-{username}"

    def fork(self, upstream_url: str, username: str, private: bool = False) -> StudentRepo:
        """Fork upstream into this org with -username suffix; idempotent."""
        owner, name = _parse_repo(upstream_url)
        target_name = self._org_repo_name(upstream_url, username)

        # If it already exists, return it
        if self._repo_exists(self.org, target_name):
            return StudentRepo(self.org, target_name)

        # Create a fork via API then rename and transfer if needed
        # 1) Create a fork under the org (GitHub supports org forks via org param)
        r = self.session.post(
            f"https://api.github.com/repos/{owner}/{name}/forks",
            json={"organization": self.org},
            timeout=30,
        )
        if r.status_code not in (202, 201):
            raise RuntimeError(f"Fork failed: {r.status_code} {r.text}")

        # The fork appears initially as org/name; wait for it to be ready
        self._wait_repo_ready(self.org, name)

        # If target already exists (a previous run progressed further), treat as done
        if self._repo_exists(self.org, target_name):
            return StudentRepo(self.org, target_name)

        # Rename with retries to handle GitHub background operations
        if name != target_name:
            self._rename_with_retry(self.org, name, target_name, private=private)
            # After rename, wait for target to be resolvable
            self._wait_repo_ready(self.org, target_name)

        return StudentRepo(self.org, target_name)

    def remove(self, upstream_or_fullname: str, username: Optional[str] = None) -> bool:
        """Delete a student repo. Accepts an upstream URL+username or full "org/name"."""
        if username:
            name = self._org_repo_name(upstream_or_fullname, username)
            full = f"{self.org}/{name}"
        else:
            if "/" in upstream_or_fullname:
                full = upstream_or_fullname.split("github.com/")[-1]
            else:
                full = upstream_or_fullname
        full = full.strip("/")
        r = self.session.delete(f"https://api.github.com/repos/{full}", timeout=30)
        if r.status_code in (204, 202):
            return True
        if r.status_code == 404:
            return False
        raise RuntimeError(f"Delete failed: {r.status_code} {r.text}")

    def get_repo(self, upstream_url: str, username: str) -> Optional[StudentRepo]:
        name = self._org_repo_name(upstream_url, username)
        if self._repo_exists(self.org, name):
            return StudentRepo(self.org, name)
        return None

    def _repo_exists(self, owner: str, name: str) -> bool:
        r = self.session.get(f"https://api.github.com/repos/{owner}/{name}", timeout=15)
        return r.status_code == 200

    def _wait_repo_ready(self, owner: str, name: str, timeout: int = 180) -> None:
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.session.get(f"https://api.github.com/repos/{owner}/{name}", timeout=15)
            if r.status_code == 200:
                return
            if r.status_code not in (404, 202, 403):
                # Unexpected error; surface it
                raise RuntimeError(f"Repo readiness check failed: {r.status_code} {r.text}")
            time.sleep(2)
        raise TimeoutError(f"Repo {owner}/{name} not ready in time")

    def _rename_with_retry(self, owner: str, current_name: str, target_name: str, private: bool = False, retries: int = 8) -> None:
        import time

        backoff = 1
        last_status = None
        last_text = None
        for _ in range(retries):
            rr = self.session.patch(
                f"https://api.github.com/repos/{owner}/{current_name}",
                json={"name": target_name, "private": private},
                timeout=30,
            )
            if rr.status_code < 400:
                return
            last_status, last_text = rr.status_code, rr.text

            # If target already exists, consider operation complete if it matches desired state
            if rr.status_code == 422:
                try:
                    data = rr.json()
                except Exception:
                    data = {}
                message = (data.get("message") or "")
                errors = " ".join((e.get("message", "") for e in data.get("errors", []) if isinstance(e, dict)))
                combined = f"{message} {errors}".lower()
                if "already exists" in combined and self._repo_exists(owner, target_name):
                    return
                if "conflicting repository operation" in combined:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    continue

            # Other errors: short backoff and retry
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)

        raise RuntimeError(f"Rename failed after retries: {last_status} {last_text}")
