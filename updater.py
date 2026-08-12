from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable


REPOSITORY = "yc883123/naiba-chat"
MANIFEST_ASSET = "naiba-chat-update.json"
EXECUTABLE_ASSET = "naiba-chat.exe"
LATEST_RELEASE_URL = f"https://github.com/{REPOSITORY}/releases/latest"
LATEST_DOWNLOAD_URL = f"{LATEST_RELEASE_URL}/download"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class UpdateManager:
    def __init__(self, app_dir: Path, data_dir: Path, auto_update: bool = True):
        self.app_dir = app_dir.resolve()
        self.data_dir = data_dir.resolve()
        self.auto_update = bool(auto_update)
        self.lock = threading.RLock()
        self.latest: dict[str, Any] | None = None
        self.phase = "idle"
        self.error = ""
        self.checked_at = 0
        self.build = self._read_build_info()

    def _run_git(self, *args: str, timeout: int = 30) -> str:
        attempts = 2 if args and args[0] in {"fetch", "pull", "ls-remote"} else 1
        for attempt in range(attempts):
            result = subprocess.run(
                ["git", *args],
                cwd=self.app_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode == 0:
                return result.stdout.strip()
            if attempt + 1 < attempts:
                time.sleep(1.5)
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or f"git {' '.join(args)} 执行失败")

    def _source_repository(self) -> bool:
        if getattr(sys, "frozen", False) or not (self.app_dir / ".git").exists():
            return False
        try:
            remote = self._run_git("remote", "get-url", "origin", timeout=5).lower()
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            return False
        normalized = remote.removesuffix(".git").replace("\\", "/")
        return bool(re.search(r"github\.com[/:]yc883123/naiba-chat$", normalized))

    def _read_build_info(self) -> dict[str, str]:
        if not getattr(sys, "frozen", False) and (self.app_dir / ".git").exists():
            try:
                commit = self._run_git("rev-parse", "HEAD", timeout=5).lower()
                return {"version": "source", "commit": commit}
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                pass
        resource_dir = Path(getattr(sys, "_MEIPASS", self.app_dir)).resolve()
        path = resource_dir / "build_info.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return {
                    "version": str(value.get("version") or "dev"),
                    "commit": str(value.get("commit") or ""),
                }
        except (OSError, json.JSONDecodeError):
            pass
        return {"version": "dev", "commit": ""}

    @property
    def supported(self) -> bool:
        return bool(os.name == "nt" and (getattr(sys, "frozen", False) or self._source_repository()))

    @property
    def mode(self) -> str:
        return "executable" if getattr(sys, "frozen", False) else "source"

    def _source_dirty(self) -> bool:
        if self.mode != "source" or not self._source_repository():
            return False
        try:
            return bool(self._run_git("status", "--porcelain", "--untracked-files=normal", timeout=10))
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            return True

    def status(self) -> dict[str, Any]:
        with self.lock:
            latest = dict(self.latest or {})
            current_commit = self.build.get("commit", "")
            update_available = bool(
                latest
                and latest.get("update_available", latest.get("commit") != current_commit)
            )
            return {
                "repository": REPOSITORY,
                "supported": self.supported,
                "mode": self.mode,
                "auto_update": self.auto_update,
                "current_version": self.build.get("version", "dev"),
                "current_commit": current_commit,
                "phase": self.phase,
                "error": self.error,
                "checked_at": self.checked_at,
                "update_available": update_available,
                "latest_version": latest.get("version", ""),
                "latest_commit": latest.get("commit", ""),
                "published_at": latest.get("published_at", ""),
                "release_url": latest.get("release_url", ""),
                "source_dirty": self._source_dirty(),
            }

    @staticmethod
    def _request_json(url: str, timeout: int = 20) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "naiba-chat-updater",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8-sig"))
        if not isinstance(value, dict):
            raise RuntimeError("更新服务器返回了无效数据")
        return value

    @staticmethod
    def _validate_manifest(manifest: dict[str, Any]) -> dict[str, str]:
        repository = str(manifest.get("repository") or "")
        commit = str(manifest.get("commit") or "").lower()
        checksum = str(manifest.get("sha256") or "").lower()
        asset = str(manifest.get("asset") or "")
        if repository != REPOSITORY:
            raise RuntimeError("更新清单的仓库不匹配")
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise RuntimeError("更新清单的提交版本无效")
        if not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise RuntimeError("更新清单的校验值无效")
        if asset != EXECUTABLE_ASSET:
            raise RuntimeError("更新清单的程序文件名无效")
        return {
            "repository": repository,
            "commit": commit,
            "sha256": checksum,
            "asset": asset,
            "version": str(manifest.get("version") or commit[:7]),
        }

    def check(self, force: bool = False) -> dict[str, Any]:
        with self.lock:
            if not force and self.checked_at and time.time() - self.checked_at < 6 * 3600:
                return self.status()
            self.phase = "checking"
            self.error = ""
        try:
            if self.mode == "source":
                if not self._source_repository():
                    raise RuntimeError("当前目录不是受支持的 naiba-chat Git 仓库")
                current = self._run_git("rev-parse", "HEAD", timeout=10).lower()
                self._run_git("fetch", "--quiet", "origin", "master", timeout=60)
                remote_commit = self._run_git("rev-parse", "origin/master", timeout=10).lower()
                if not re.fullmatch(r"[0-9a-f]{40}", remote_commit):
                    raise RuntimeError("无法读取 origin/master 的提交版本")
                counts = self._run_git(
                    "rev-list", "--left-right", "--count", "HEAD...origin/master", timeout=10
                ).split()
                if len(counts) != 2:
                    raise RuntimeError("无法比较本地与远程版本")
                local_ahead, remote_ahead = (int(value) for value in counts)
                divergent = local_ahead > 0 and remote_ahead > 0
                update_available = remote_ahead > 0 and not divergent
                self.build = {"version": "source", "commit": current}
                with self.lock:
                    self.latest = {
                        "version": "origin/master",
                        "commit": remote_commit,
                        "release_url": f"https://github.com/{REPOSITORY}/commits/master",
                        "update_available": update_available,
                    }
                    if divergent:
                        self.phase = "error"
                        self.error = "本地与 origin/master 已分叉，请在终端中手动处理"
                    else:
                        self.phase = "available" if update_available else "current"
                    self.checked_at = int(time.time())
                return self.status()
            manifest_url = f"{LATEST_DOWNLOAD_URL}/{MANIFEST_ASSET}"
            executable_url = f"{LATEST_DOWNLOAD_URL}/{EXECUTABLE_ASSET}"
            manifest = self._validate_manifest(self._request_json(manifest_url))
            manifest.update(
                {
                    "download_url": executable_url,
                    "published_at": str(manifest.get("published_at") or ""),
                    "release_url": LATEST_RELEASE_URL,
                }
            )
            with self.lock:
                self.latest = manifest
                self.phase = "available" if manifest["commit"] != self.build.get("commit") else "current"
                self.checked_at = int(time.time())
        except urllib.error.HTTPError as exc:
            message = "尚无可用的自动更新版本" if exc.code == 404 else f"检查更新失败：HTTP {exc.code}"
            with self.lock:
                self.phase = "error"
                self.error = message
                self.checked_at = int(time.time())
        except Exception as exc:
            with self.lock:
                self.phase = "error"
                self.error = f"检查更新失败：{exc}"
                self.checked_at = int(time.time())
        return self.status()

    def _download(self, latest: dict[str, Any]) -> Path:
        update_dir = self.data_dir / "update"
        update_dir.mkdir(parents=True, exist_ok=True)
        target = update_dir / f"naiba-chat-{latest['commit'][:12]}.download"
        request = urllib.request.Request(
            str(latest["download_url"]),
            headers={"Accept": "application/octet-stream", "User-Agent": "naiba-chat-updater"},
        )
        with urllib.request.urlopen(request, timeout=120) as response, target.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        if target.stat().st_size < 1024 * 1024 or target.read_bytes()[:2] != b"MZ":
            target.unlink(missing_ok=True)
            raise RuntimeError("下载的更新文件不是有效的 Windows 程序")
        if _sha256(target) != latest["sha256"]:
            target.unlink(missing_ok=True)
            raise RuntimeError("更新文件校验失败，已拒绝安装")
        return target

    def _launch_replacer(self, downloaded: Path) -> None:
        target = Path(sys.executable).resolve()
        update_dir = downloaded.parent
        script = update_dir / "apply-update.ps1"
        backup = update_dir / "naiba-chat.previous.exe"
        script.write_text(
            """param([int]$ProcessId, [string]$Downloaded, [string]$Target, [string]$Backup)
$ErrorActionPreference = 'Stop'
Wait-Process -Id $ProcessId -Timeout 30 -ErrorAction SilentlyContinue
$deadline = (Get-Date).AddSeconds(60)
$installed = $false
while ((Get-Date) -lt $deadline) {
  try {
    if (Test-Path -LiteralPath $Target) { Copy-Item -LiteralPath $Target -Destination $Backup -Force }
    Copy-Item -LiteralPath $Downloaded -Destination $Target -Force
    $installed = $true
    break
  } catch { Start-Sleep -Milliseconds 500 }
}
if (-not $installed) { exit 1 }
Start-Process -FilePath $Target -WorkingDirectory (Split-Path -Parent $Target) -WindowStyle Hidden
Remove-Item -LiteralPath $Downloaded -Force -ErrorAction SilentlyContinue
""",
            encoding="utf-8",
        )
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-ProcessId",
                str(os.getpid()),
                "-Downloaded",
                str(downloaded),
                "-Target",
                str(target),
                "-Backup",
                str(backup),
            ],
            cwd=str(self.app_dir),
            creationflags=creation_flags,
        )

    def _launch_source_updater(self) -> None:
        update_dir = self.data_dir / "update"
        update_dir.mkdir(parents=True, exist_ok=True)
        script = update_dir / "apply-source-update.ps1"
        launcher = self.app_dir / "launcher.py"
        script.write_text(
            """param([int]$ProcessId, [string]$Repository, [string]$Python, [string]$Launcher)
$ErrorActionPreference = 'Stop'
Wait-Process -Id $ProcessId -Timeout 30 -ErrorAction SilentlyContinue
Set-Location -LiteralPath $Repository
& git pull --ff-only origin master
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Start-Process -FilePath $Python -ArgumentList @($Launcher) -WorkingDirectory $Repository -WindowStyle Hidden
""",
            encoding="utf-8",
        )
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-ProcessId",
                str(os.getpid()),
                "-Repository",
                str(self.app_dir),
                "-Python",
                str(Path(sys.executable).resolve()),
                "-Launcher",
                str(launcher),
            ],
            cwd=str(self.app_dir),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def start_install(self, on_ready: Callable[[], None] | None) -> dict[str, Any]:
        if not self.supported:
            raise RuntimeError("当前运行方式不支持自动更新")
        if self.mode == "source" and on_ready is None:
            raise RuntimeError("源码更新需要从桌面启动器运行")
        with self.lock:
            if self.phase in {"downloading", "restarting"}:
                return self.status()
            latest = dict(self.latest or {})
            if not latest or latest.get("commit") == self.build.get("commit"):
                raise RuntimeError("没有可安装的新版本")
            if self.mode == "source" and self._source_dirty():
                raise RuntimeError("工作区有未提交修改，请先提交或清理后再更新")
            self.phase = "downloading"
            self.error = ""

        if self.mode == "source":
            self._launch_source_updater()
            with self.lock:
                self.phase = "restarting"
            if on_ready:
                threading.Timer(0.5, on_ready).start()
            return self.status()

        def install() -> None:
            try:
                downloaded = self._download(latest)
                self._launch_replacer(downloaded)
                with self.lock:
                    self.phase = "restarting"
                time.sleep(0.5)
                if on_ready:
                    on_ready()
            except Exception as exc:
                with self.lock:
                    self.phase = "error"
                    self.error = f"安装更新失败：{exc}"

        threading.Thread(target=install, name="naiba-update-install", daemon=True).start()
        return self.status()

    def start_auto_update(self, on_ready: Callable[[], None] | None, delay: float = 4.0) -> None:
        if not self.auto_update or not self.supported:
            return

        def run() -> None:
            time.sleep(delay)
            status = self.check()
            if status["update_available"]:
                try:
                    self.start_install(on_ready)
                except RuntimeError:
                    pass

        threading.Thread(target=run, name="naiba-update-check", daemon=True).start()
