"""HTTP 单区间 Range 解析（纯函数，/api/file 大文件可 seek 的依据）。

浏览器对 `<video>/<audio>` 的 `preload="metadata"` 与进度条拖动都会发
`Range: bytes=...`；旧实现无条件整包 `read_bytes()` + 200，导致"拉全量 +
服务端内存吃满 + 拖不动进度条"。本模块只做解析，不发响应：

- 无 Range / 非 `bytes=` 单位 / 语法不合法 / 多区间 → 返回 None（调用方按整包 200 响应；
  与主流静态服务器一致：单区间才走 206）；
- 语法合法但不可满足（起点越界、空文件后缀请求）→ 抛 ValueError（调用方回 416 +
  `Content-Range: bytes */<size>`）。
"""

from __future__ import annotations

__all__ = ["parse_byte_range", "content_range_header"]


def parse_byte_range(raw: str | None, size: int) -> tuple[int, int] | None:
    """解析单区间 Range，返回闭区间 ``(start, end)``（含端点）；None=按整包响应。"""
    if size < 0:
        raise ValueError(f"文件大小非法：{size}")
    value = str(raw or "").strip()
    if not value or not value.lower().startswith("bytes="):
        return None
    spec = value[len("bytes="):].strip()
    if "," in spec:  # 多区间：本项目不实现 multipart/byteranges，按整包响应
        return None
    start_raw, separator, end_raw = spec.partition("-")
    if not separator:
        return None
    start_raw, end_raw = start_raw.strip(), end_raw.strip()
    if not start_raw:  # bytes=-N：最后 N 字节
        if not end_raw.isdigit():
            return None
        length = int(end_raw)
        if length <= 0 or size == 0:
            raise ValueError("请求区间不可满足")
        return (max(0, size - length), size - 1)
    if not start_raw.isdigit():
        return None
    if end_raw and not end_raw.isdigit():
        return None
    start = int(start_raw)
    if size == 0 or start >= size:
        raise ValueError("请求区间不可满足")
    end = int(end_raw) if end_raw else size - 1
    if end < start:  # 语法合法但无意义：忽略该头（按整包响应）
        return None
    return (start, min(end, size - 1))


def content_range_header(start: int, end: int, size: int) -> str:
    """206 响应的 Content-Range 值。"""
    return f"bytes {int(start)}-{int(end)}/{int(size)}"
