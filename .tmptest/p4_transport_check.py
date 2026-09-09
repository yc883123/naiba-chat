# -*- coding: utf-8 -*-
"""P4 传输侧校验（无需浏览器）：GIF 首帧缩略图 + /api/file 单区间 Range + .m4v MIME。

运行前：python server.py --port 8799
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE = "http://127.0.0.1:8799"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def request(path: str, headers: dict | None = None, method: str = "GET", body: bytes | None = None):
    req = urllib.request.Request(BASE + path, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def upload(filename: str, payload: bytes, content_type: str = "application/octet-stream") -> dict:
    boundary = "----naibap4" + uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8") + payload + f"\r\n--{boundary}--\r\n".encode("utf-8")
    status, _, raw = request(
        "/api/uploads",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
        body=body,
    )
    if status != 200:
        raise RuntimeError(f"上传失败：{status} {raw[:200]!r}")
    return json.loads(raw.decode("utf-8", "replace"))


def file_url(path: str) -> str:
    return "/api/file?" + urllib.parse.urlencode({"path": path})


def main() -> int:
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'}  {label}{('  -> ' + detail) if detail and not ok else ''}")
        if not ok:
            failures.append(label)

    # ---- 1. GIF：主图原样 + 首帧缩略图可服务 ----
    from PIL import Image

    frames = [Image.new("P", (64, 48), color) for color in (1, 2, 3)]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    gif_bytes = buf.getvalue()
    gif = upload("p4动画.gif", gif_bytes, "image/gif")
    check("GIF 上传成功", bool(gif.get("path")), json.dumps(gif, ensure_ascii=False)[:160])
    main_bytes = open(gif["path"], "rb").read()
    check("GIF 主图原样保留（动画不丢）", main_bytes == gif_bytes, f"{len(main_bytes)} vs {len(gif_bytes)}")
    check("GIF 返回非空 thumb_path", bool(gif.get("thumb_path")), json.dumps(gif, ensure_ascii=False))
    status, headers, thumb = request(file_url(gif["thumb_path"]))
    check("GIF 缩略图可服务 200", status == 200, str(status))
    check("GIF 缩略图 Content-Type=image/webp", headers.get("Content-Type", "").startswith("image/webp"), headers.get("Content-Type", ""))
    check("GIF 缩略图非空且为 WebP", thumb[:4] == b"RIFF" and thumb[8:12] == b"WEBP", thumb[:16].hex())

    # ---- 2. /api/file Range（用一个 5MB 二进制文件）----
    payload = bytes(range(256)) * (5 * 1024 * 1024 // 256)
    blob = upload("p4range.bin", payload)
    path = blob["path"]
    size = len(payload)

    status, headers, body = request(file_url(path))
    check("无 Range → 200 全量", status == 200 and len(body) == size, f"{status} {len(body)}")
    check("无 Range → Accept-Ranges: bytes", headers.get("Accept-Ranges") == "bytes", str(headers.get("Accept-Ranges")))

    status, headers, body = request(file_url(path), {"Range": "bytes=100-199"})
    check("Range 100-199 → 206", status == 206, str(status))
    check("Range 100-199 → Content-Range", headers.get("Content-Range") == f"bytes 100-199/{size}", str(headers.get("Content-Range")))
    check("Range 100-199 → 字节正确", body == payload[100:200], f"{len(body)} bytes")
    check("Range 100-199 → Content-Length 100", headers.get("Content-Length") == "100", str(headers.get("Content-Length")))

    status, headers, body = request(file_url(path), {"Range": "bytes=0-"})
    check("Range 0- → 206 全量", status == 206 and body == payload, f"{status} {len(body)}")

    status, headers, body = request(file_url(path), {"Range": "bytes=-1024"})
    check("Range -1024 → 206 尾部 1024 字节", status == 206 and body == payload[-1024:], f"{status} {len(body)}")

    status, headers, body = request(file_url(path), {"Range": f"bytes={size}-"})
    check("越界 Range → 416", status == 416, str(status))
    check("416 → Content-Range: bytes */size", headers.get("Content-Range") == f"bytes */{size}", str(headers.get("Content-Range")))

    status, headers, body = request(file_url(path), {"Range": "bytes=abc"})
    check("非法 Range → 忽略（200 全量）", status == 200 and len(body) == size, f"{status} {len(body)}")

    # ---- 3. .m4v MIME 兜底（P1 修的名单缺口；系统 mimetypes 认得时用系统值，认不得时用兜底表）
    m4v = upload("p4视频.m4v", b"\x00\x00\x00\x18ftypm4v " + b"\x00" * 64)
    status, headers, _ = request(file_url(m4v["path"]))
    content_type = headers.get("Content-Type", "")
    check(".m4v → video/*（不得是 octet-stream）", content_type.startswith("video/"), content_type)

    print()
    print("P4 传输侧校验：", "全部通过" if not failures else f"{len(failures)} 项失败 -> {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
