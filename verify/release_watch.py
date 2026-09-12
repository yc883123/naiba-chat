"""等 GitHub Actions 发版跑完，并校验 Release 资产是否自洽。

本机**没有 gh CLI**，所以直接打匿名 GitHub API（仓库公开、免 token，60 次/小时）。
一条命令走完「等 run → 看每步结论 → 核对 manifest → 下载 exe 对哈希」。

用法：
  .venv/Scripts/python.exe verify/release_watch.py
      # 等本地 HEAD 那个 sha 的 run；成功后若知道 tag 会自动核对资产
  .venv/Scripts/python.exe verify/release_watch.py <sha> <tag>
      # 显式指定，如：… release_watch.py ed68fe55… v2.3.5-beta

退出码：0 = 发布成功且资产自洽；1 = 失败/超时/资产不一致。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "yc883123/naiba-chat"
API = f"https://api.github.com/repos/{REPO}"
HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "naiba-release-watch"}


def get_json(url: str, timeout: int = 30):
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def local_head() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT), check=True,
                          capture_output=True, text=True).stdout.strip()


def wait_for_run(sha: str, minutes: int = 20):
    deadline = time.time() + minutes * 60
    run = None
    while time.time() < deadline:
        try:
            data = get_json(f"{API}/actions/runs?per_page=10")
        except Exception as error:  # noqa: BLE001 - 网络抖动不影响主流程
            print("查询失败，稍后重试:", error)
            time.sleep(15)
            continue
        run = next((item for item in data.get("workflow_runs", [])
                    if item["head_sha"] == sha), None)
        if run is None:
            print(f"还没看到 sha={sha[:8]} 的 run…")
        else:
            print(f"#{run['run_number']} status={run['status']} conclusion={run['conclusion']}")
            if run["status"] == "completed":
                return run
        time.sleep(15)
    print("超时：没等到该 run")
    return run


def report_steps(run: dict) -> None:
    jobs = get_json(f"{API}/actions/runs/{run['id']}/jobs")
    for job in jobs.get("jobs", []):
        print(f"\njob: {job['name']} | {job['status']}/{job['conclusion']}")
        for step in job.get("steps", []):
            flag = {"success": "OK  ", "failure": "FAIL", "skipped": "skip"}.get(
                step.get("conclusion") or "", "..  ")
            print(f"  {flag} {step['name']}")


def check_assets(tag: str, commit: str) -> bool:
    release = get_json(f"{API}/releases/tags/{tag}")
    assets = {a["name"]: a for a in release["assets"]}
    print(f"\n--- Release {release['tag_name']} | {release['name']} ---")
    print(f"prerelease={release['prerelease']}  assets="
          + ", ".join(f"{n}({a['size']:,}B)" for n, a in assets.items()))

    ok = True
    manifest = get_json(assets["naiba-chat-update.json"]["browser_download_url"])
    print(f"manifest: version={manifest['version']} commit={manifest['commit'][:12]} "
          f"notes={len(manifest['release_notes'])} 条")
    print("  sha256 =", manifest["sha256"])
    if manifest["commit"] != commit:
        print("  !! manifest.commit 不是本次提交"); ok = False
    if not manifest["release_notes"][0].startswith("Naiba Chat"):
        print("  !! release_notes 头条异常"); ok = False

    target = ROOT / "verify" / "_release_exe_check.exe"
    print("下载 exe 核对哈希（约 56MB，稍等）…")
    request = urllib.request.Request(assets["naiba-chat.exe"]["browser_download_url"],
                                     headers=HEADERS)
    digest = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=300) as response, target.open("wb") as handle:
        while chunk := response.read(1 << 20):
            handle.write(chunk)
            digest.update(chunk)
    actual = digest.hexdigest()
    print("  实际 sha256 =", actual)
    if actual != manifest["sha256"]:
        print("  !! 哈希不一致 —— 应用内自动更新会拒绝安装"); ok = False
    else:
        print("  哈希一致 → 「检查更新」可正常校验并安装")
    target.unlink(missing_ok=True)
    return ok


def main() -> int:
    sha = sys.argv[1] if len(sys.argv) > 1 else local_head()
    tag = sys.argv[2] if len(sys.argv) > 2 else ""
    print(f"跟踪 sha={sha}")

    run = wait_for_run(sha)
    if run is None:
        return 1
    print(f"\nrun #{run['run_number']} → {run['conclusion']}")
    print(run["html_url"])
    report_steps(run)
    if run["conclusion"] != "success":
        print("\n构建失败，看上面的 FAIL 步骤")
        return 1
    if not tag:
        print("\n发布成功（未指定 tag，跳过资产核对）")
        return 0
    return 0 if check_assets(tag, sha) else 1


if __name__ == "__main__":
    raise SystemExit(main())
