# -*- coding: utf-8 -*-
"""RunningHub AI 应用客户端（自包含，仅 Python 标准库）。

供 shortdramav2-rh 的 generate_unit_rh.py 导入使用。实现：

- 站点 / API Key 多来源解析（config > 环境变量 > ~/.openclaw/openclaw.json）
- AI 应用节点探测（GET /api/webapp/apiCallDemo）
- 参考资产上传（POST /task/openapi/upload，multipart）
- 任务提交（POST /task/openapi/ai-app/run，nodeInfoList）
- 任务轮询（POST /openapi/v2/query）
- 结果下载 + QuickTime MOV 容器修复为 MP4

错误统一抛出 RhError（含 error_code / message / steps），由调用方决定如何呈现。
不依赖 curl、不依赖 runninghub skill，任何脚本均可独立导入。
"""

from __future__ import annotations

import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

SITES = {
    "ai": {
        "label": "AI 站（国际，runninghub.ai）",
        "home": "https://www.runninghub.ai",
        "api": "https://www.runninghub.ai/openapi/v2",
    },
    "cn": {
        "label": "CN 站（国内，runninghub.cn）",
        "home": "https://www.runninghub.cn",
        "api": "https://www.runninghub.cn/openapi/v2",
    },
}

NODE_INFO_PATH = "/api/webapp/apiCallDemo"
UPLOAD_PATH = "/task/openapi/upload"
SUBMIT_PATH = "/task/openapi/ai-app/run"
POLL_PATH = "/query"

DEFAULT_POLL_SECONDS = 1200
POLL_INTERVAL = 5

_PLACEHOLDER_KEYS = {"your_api_key_here", "<your_api_key>", "YOUR_API_KEY", "RUNNINGHUB_API_KEY"}


class RhError(Exception):
    """业务错误。code 对齐 AI 应用约定（NO_API_KEY / APP_INFO_FAILED / NO_NODES /
    UPLOAD_FAILED / NODE_ERRORS / TASK_FAILED / INSUFFICIENT_BALANCE 等）。"""

    def __init__(self, code: str, message: str, steps: list | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.steps = steps or []

    def to_dict(self) -> dict:
        d = {"error": self.code, "message": self.message}
        if self.steps:
            d["steps"] = self.steps
        return d


# ---------------------------------------------------------------------------
# 站点 / API Key 解析
# ---------------------------------------------------------------------------

def _read_openclaw_config() -> dict:
    try:
        p = Path.home() / ".openclaw" / "openclaw.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def resolve_api_key(provided: str | None = None) -> str | None:
    """解析 API Key：provided > 环境变量 RUNNINGHUB_API_KEY > openclaw.json
    （skills.entries.runninghub.apiKey，回退 env.RUNNINGHUB_API_KEY）。"""
    if provided:
        key = provided.strip()
        if key and key.lower() not in _PLACEHOLDER_KEYS:
            return key
    env_key = os.environ.get("RUNNINGHUB_API_KEY", "").strip()
    if env_key:
        return env_key
    entry = _read_openclaw_config().get("skills", {}).get("entries", {}).get("runninghub", {})
    api_key = entry.get("apiKey")
    if isinstance(api_key, str) and api_key.strip():
        return api_key.strip()
    env_val = entry.get("env", {}).get("RUNNINGHUB_API_KEY")
    if isinstance(env_val, str) and env_val.strip():
        return env_val.strip()
    return None


def require_api_key(provided: str | None = None) -> str:
    key = resolve_api_key(provided)
    if key:
        return key
    raise RhError(
        "NO_API_KEY",
        "未配置 RunningHub API Key。",
        steps=[
            "1. 在 config.json 填 rh_api_key",
            "2. 或设置环境变量 RUNNINGHUB_API_KEY",
            "3. 或在 ~/.openclaw/openclaw.json 配置 skills.entries.runninghub.apiKey",
        ],
    )


def resolve_site(provided: str | None = None, config_site: str | None = None) -> str:
    """解析站点：provided(命令行) > config site > 环境变量 RUNNINGHUB_SITE >
    openclaw.json site > 默认 ai。"""
    for cand in (provided, config_site, os.environ.get("RUNNINGHUB_SITE", "")):
        if cand:
            val = str(cand).strip().lower()
            if val in SITES:
                return val
    entry = _read_openclaw_config().get("skills", {}).get("entries", {}).get("runninghub", {})
    s = entry.get("site")
    if isinstance(s, str) and s.strip().lower() in SITES:
        return s.strip().lower()
    return "ai"


# ---------------------------------------------------------------------------
# HTTP helpers（urllib，stdlib only）
# ---------------------------------------------------------------------------

def _urlopen(req, timeout: int):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        raise RhError("NETWORK_ERROR", f"网络请求失败: {e.reason}")
    except OSError as e:
        raise RhError("NETWORK_ERROR", f"网络请求失败: {e}")


def http_get(url: str, timeout: int = 60) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "shortdramav2-rh/1.0"})
    status, body = _urlopen(req, timeout)
    if status != 200:
        raise RhError("API_ERROR", f"GET 请求失败（HTTP {status}）: {body[:300]}")
    return json.loads(body.decode("utf-8", errors="replace"))


def http_post(url: str, payload: dict, timeout: int = 60) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "User-Agent": "shortdramav2-rh/1.0",
    })
    status, body = _urlopen(req, timeout)
    if status != 200:
        raise RhError("API_ERROR", f"POST 请求失败（HTTP {status}）: {body[:300]}")
    return json.loads(body.decode("utf-8", errors="replace"))


def _classify_error(prefix: str, resp: dict) -> RhError:
    code = str(resp.get("code", ""))
    msg = str(resp.get("msg", ""))
    joined = f"{code} {msg}".lower()
    if any(k in joined for k in ("auth", "401", "403", "token", "apikey", "鉴权")):
        return RhError("AUTH_FAILED", f"{prefix}: 鉴权失败: {msg}")
    if any(k in joined for k in ("balance", "insufficient", "余额", "credit", "积分")):
        return RhError("INSUFFICIENT_BALANCE", f"{prefix}: 余额不足: {msg}")
    return RhError("API_ERROR", f"{prefix}: {msg}")


# ---------------------------------------------------------------------------
# AI 应用 API
# ---------------------------------------------------------------------------

def get_node_info(api_key: str, webapp_id: str, site: str) -> list[dict]:
    """获取 AI 应用可修改节点列表（含默认 fieldValue / fieldType / description）。"""
    url = (f"{SITES[site]['home']}{NODE_INFO_PATH}"
           f"?apiKey={urllib.parse.quote(api_key)}&webappId={urllib.parse.quote(str(webapp_id))}")
    resp = http_get(url)
    if resp.get("code") != 0:
        raise RhError("APP_INFO_FAILED",
                      f"获取 AI 应用节点失败: {resp.get('msg', resp)}"
                      "（请确认 webappId 正确且应用为公开可访问）")
    node_list = resp.get("data", {}).get("nodeInfoList") or []
    if not node_list:
        raise RhError(
            "NO_NODES",
            "该 AI 应用没有可修改的节点。请先在 RunningHub 网页上成功运行一次该应用，"
            "之后才能通过 API 调用。",
        )
    return node_list


def upload_file(api_key: str, path: str, site: str) -> str:
    """上传参考资产，返回 data.fileName（用于 nodeInfoList 的 fieldValue）。"""
    p = Path(path)
    if not p.exists():
        raise RhError("FILE_NOT_FOUND", f"上传文件不存在: {path}")
    url = SITES[site]["home"] + UPLOAD_PATH
    boundary = "----rh" + uuid.uuid4().hex
    filename = p.name

    body = bytearray()
    body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"apiKey\"\r\n\r\n{api_key}\r\n".encode("utf-8"))
    body.extend(b"--" + boundary.encode("ascii") + b"\r\nContent-Disposition: form-data; name=\"fileType\"\r\n\r\ninput\r\n")
    if any(ord(c) > 127 for c in filename):
        quoted = urllib.parse.quote(filename)
        body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename*=UTF-8''{quoted}\r\n"
                    "Content-Type: application/octet-stream\r\n\r\n".encode("utf-8"))
    else:
        body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
                    "Content-Type: application/octet-stream\r\n\r\n".encode("utf-8"))
    body.extend(p.read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode("utf-8"))

    req = urllib.request.Request(url, data=bytes(body), headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "User-Agent": "shortdramav2-rh/1.0",
    })
    try:
        status, resp_body = _urlopen(req, 120)
    except RhError as e:
        raise RhError("UPLOAD_FAILED", f"文件上传失败: {e.message}")
    if status != 200:
        raise RhError("UPLOAD_FAILED", f"文件上传失败（HTTP {status}）: {resp_body[:300]}")
    try:
        resp = json.loads(resp_body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        raise RhError("UPLOAD_FAILED", f"文件上传失败，响应无法解析: {resp_body[:300]}")
    if resp.get("code") != 0 or resp.get("msg") != "success":
        raise RhError("UPLOAD_FAILED", f"文件上传失败: {resp.get('msg', resp)}")
    file_name = resp.get("data", {}).get("fileName")
    if not file_name:
        raise RhError("UPLOAD_FAILED", "文件上传成功但未返回 fileName")
    return file_name


def submit_task(api_key: str, webapp_id: str, node_info_list: list[dict],
                site: str, instance_type: str = "default") -> str:
    """提交 AI 应用任务，返回 taskId。nodeInfoList 未指定节点保留默认值。"""
    payload = {
        "apiKey": api_key,
        "webappId": int(webapp_id),
        "nodeInfoList": node_info_list,
    }
    if instance_type and instance_type != "default":
        payload["instanceType"] = instance_type
    url = SITES[site]["home"] + SUBMIT_PATH
    resp = http_post(url, payload)
    if resp.get("code") != 0:
        raise _classify_error("提交 AI 应用任务失败", resp)
    data = resp.get("data") or {}
    task_id = data.get("taskId")
    if not task_id:
        raise RhError("SUBMIT_FAILED", f"提交任务后未返回 taskId: {resp}")
    tips = data.get("promptTips")
    if tips:
        try:
            node_errors = json.loads(tips).get("node_errors") or {}
        except (json.JSONDecodeError, TypeError, AttributeError):
            node_errors = {}
        if node_errors:
            raise RhError("NODE_ERRORS",
                          "工作流节点出错，请检查提交的参数: "
                          + json.dumps(node_errors, ensure_ascii=False))
    return str(task_id)


def poll_task(api_key: str, task_id: str, site: str,
              timeout: int = DEFAULT_POLL_SECONDS, interval: int = POLL_INTERVAL) -> dict:
    """轮询任务直到 SUCCESS / FAILED / 超时。返回最终响应 dict。"""
    url = SITES[site]["api"] + POLL_PATH
    deadline = time.time() + timeout
    consecutive_failures = 0
    while time.time() < deadline:
        time.sleep(interval)
        try:
            resp = http_post(url, {"taskId": task_id})
        except RhError:
            consecutive_failures += 1
            if consecutive_failures >= 5:
                raise RhError("POLL_FAILED",
                              "连续多次轮询失败，任务状态未知，请到 RunningHub 网页确认。")
            continue
        consecutive_failures = 0
        status = str(resp.get("status", "UNKNOWN")).upper()
        if status == "SUCCESS":
            return resp
        if status == "FAILED":
            err = str(resp.get("errorMessage") or resp.get("msg") or "Unknown error")
            ec = str(resp.get("errorCode") or "")
            joined = f"{err} {ec}".lower()
            if any(k in joined for k in ("balance", "insufficient", "余额", "credit")):
                raise RhError("INSUFFICIENT_BALANCE", f"任务失败: {err}")
            raise RhError("TASK_FAILED", f"任务失败: [{ec}] {err}")
    raise RhError("TASK_TIMEOUT", f"任务超过 {int(timeout)} 秒未完成，请到 RunningHub 网页查看进度。")


def download_file(url: str, dest: str, timeout: int = 300) -> str:
    """下载结果文件到 dest，返回绝对路径。"""
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "shortdramav2-rh/1.0"})
    try:
        status, body = _urlopen(req, timeout)
    except RhError as e:
        raise RhError("DOWNLOAD_FAILED", f"下载结果失败: {e.message}")
    if status != 200:
        raise RhError("DOWNLOAD_FAILED", f"下载结果失败（HTTP {status}）: {body[:200]}")
    Path(dest).write_bytes(body)
    return str(Path(dest).resolve())


def fix_mov_to_mp4(file_path: str) -> bool:
    """把 QuickTime MOV 的 ftyp 盒改写成标准 MP4，平台兼容性修复。
    只动文件头，不重新编码，无外部依赖。返回是否修复成功。"""
    try:
        with open(file_path, "rb") as f:
            header = f.read(64)
    except OSError:
        return False
    if len(header) < 16:
        return False
    box_size = struct.unpack(">I", header[0:4])[0]
    if header[4:8] != b"ftyp" or box_size < 16 or box_size > len(header):
        return False
    if header[8:12] != b"qt  ":
        return False
    minor_version = header[12:16]
    brands = [b"isom", b"iso2", b"avc1", b"mp41"]
    max_brands = (box_size - 16) // 4
    used_brands = brands[:max_brands]
    new_ftyp = struct.pack(">I", box_size) + b"ftyp" + b"isom" + minor_version
    for b in used_brands:
        new_ftyp += b
    new_ftyp += b"\x00" * (box_size - len(new_ftyp))
    with open(file_path, "r+b") as f:
        f.write(new_ftyp)
    return True


# ---------------------------------------------------------------------------
# 节点定位与探测（供 generate_unit_rh.py 使用）
# ---------------------------------------------------------------------------

def parse_loc(loc: str) -> tuple[str, str]:
    """'nodeId:fieldName' -> (nodeId, fieldName)。"""
    if ":" not in loc:
        raise RhError("NODE_CONFIG", f"节点定位格式应为 nodeId:fieldName，收到: {loc}")
    nid, fname = loc.split(":", 1)
    return nid.strip(), fname.strip()


def find_node(node_list: list[dict], node_id: str, field_name: str) -> dict | None:
    for n in node_list:
        if str(n.get("nodeId")) == str(node_id) and n.get("fieldName") == field_name:
            return n
    return None


def detect_prompt_node(node_list: list[dict], override: str | None = None) -> tuple[str, str]:
    """定位提示词写入节点，返回 (nodeId, fieldName)。

    自动探测：MiniMaxH3 系列节点的 prompt 字段；回退描述含「提示词/prompt/text」的
    STRING 字段；最后回退任意 STRING 字段。override 形如 '263:text'，优先于自动探测。
    """
    if override:
        nid, fname = parse_loc(override)
        if find_node(node_list, nid, fname) is None:
            raise RhError("NODE_CONFIG",
                          f"配置的提示词节点 {override} 不在该应用节点列表中，请先用 --info 核对。")
        return nid, fname
    for n in node_list:
        if "MiniMaxH3" in n.get("nodeName", "") and n.get("fieldName") == "prompt":
            return str(n["nodeId"]), "prompt"
    for n in node_list:
        if n.get("fieldType") == "STRING":
            desc = str(n.get("description") or "").lower()
            if any(k in desc for k in ("提示词", "prompt", "text")):
                return str(n["nodeId"]), n["fieldName"]
    for n in node_list:
        if n.get("fieldType") == "STRING":
            return str(n["nodeId"]), n["fieldName"]
    raise RhError("NO_PROMPT_NODE",
                  "找不到提示词字段。请用 config.runninghub.prompt_node 指定 nodeId:fieldName。")


def detect_ref_slots(node_list: list[dict], kind: str, overrides: dict | None = None) -> dict[int, tuple[str, str]]:
    """定位参考图/音槽位，返回 {标签号(1-based): (nodeId, fieldName)}。

    自动探测规则（按优先级）：
    1. fieldName 匹配 ref_images.ref_image_(\\d+)（IMAGE）/ ref_audios.ref_audio_(\\d+)（AUDIO），
       槽位号 = 匹配数字 + 1（0-based 槽位 → 1-based 标签）；
    2. LoadImage / LoadAudio 节点，按 description（image1… / audio1…）编号；
    3. 其它 IMAGE / AUDIO 节点按 description 中出现的数字编号（兜底）。
    overrides 形如 {"1": "51:image"}，显式指定 nodeId:fieldName，优先于自动探测。
    """
    if kind == "image":
        primary = r"ref_images\.ref_image_(\d+)"
        loader = ("LoadImage",)
        desc_pattern = r"image\s*(\d+)"
        tag_name = "Picture"
    else:
        primary = r"ref_audios\.ref_audio_(\d+)"
        loader = ("LoadAudio",)
        desc_pattern = r"audio\s*(\d+)"
        tag_name = "Audio"

    slots: dict[int, tuple[str, str]] = {}
    for n in node_list:
        fn = n.get("fieldName", "")
        m = re.search(primary, fn)
        if m:
            slots[int(m.group(1)) + 1] = (str(n["nodeId"]), fn)
            continue
        node_name = n.get("nodeName", "")
        if node_name not in loader:
            continue
        desc = str(n.get("description") or n.get("descriptionCn") or "")
        m2 = re.search(desc_pattern, desc)
        if m2:
            slots[int(m2.group(1))] = (str(n["nodeId"]), fn)
    for tag, loc in (overrides or {}).items():
        nid, fname = parse_loc(loc)
        if find_node(node_list, nid, fname) is None:
            raise RhError("NODE_CONFIG",
                          f"配置的{('图' if kind == 'image' else '音')}槽位 {loc}"
                          f"（<{tag_name} {tag}>）不在节点列表中，请先用 --info 核对。")
        slots[int(tag)] = (nid, fname)
    return dict(sorted(slots.items()))
