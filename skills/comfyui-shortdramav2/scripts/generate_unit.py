# -*- coding: utf-8 -*-
"""短剧单元批量生成（ComfyUI MiniMax H3 Ref2VA，通用模板）。

从同目录 config.json 读取 ComfyUI 地址、模板文件、参考图/参考音映射，
解析单元剧本 markdown，按 <Picture N>/<Audio N> 标签构建每段工作流并提交。

用法（先复制 config.example.json 为 config.json 并填好你的资产）：
  python generate_unit.py 01              # 生成 + 提交全部段并等待
  python generate_unit.py 01 build        # 只生成工作流 JSON，不提交
  python generate_unit.py 01 submit       # 生成 + 提交全部段
  python generate_unit.py 01 submit 3     # 只提交第 3 段（测试）
"""
import json
import re
import sys
import time
import uuid
import urllib.request
import urllib.error
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# 不再硬编码节点 id：按 class_type / 输入槽位自动探测模板节点，
# 用户换工作流（只要是 MiniMax H3 Ref2VA 结构）也能适配。
REF_CLASS = "MiniMaxH3ReferenceToVideo"
SAVE_CLASS = "SaveVideo"
IMG_LOADER_CLASS = "LoadImage"
AUD_LOADER_CLASS = "LoadAudio"

DEFAULT_CONFIG = {
    "comfyui_url": "http://127.0.0.1:8188",
    "template_file": "MiniMaxH3-1采TE加速.json",
    "output_prefix": "BOSS",
    "unit_glob": "单元{unit:02d}_*.md",
    "pictures": {"1": "", "2": "", "3": "", "4": "", "5": "", "6": "", "7": "", "8": "", "9": ""},
    "audios": {"1": "", "2": "", "3": ""},
    "segment_pictures": {},
    "segment_audios": {},
}

_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    p = BASE_DIR / "config.json"
    if p.exists():
        user = json.loads(p.read_text(encoding="utf-8"))
        for key, default in DEFAULT_CONFIG.items():
            if key not in user:
                continue
            if isinstance(default, dict):
                merged = dict(default)
                merged.update(user[key] or {})
                cfg[key] = merged
            else:
                cfg[key] = user[key]
    else:
        print("[提示] 未找到 config.json，使用默认空资产（请从 config.example.json 复制并填写）")
    return cfg


def cn_to_int(s):
    s = s.strip()
    if not s:
        return None
    if s in _CN:
        return _CN[s]
    if s == "十":
        return 10
    if s.startswith("十") and len(s) == 2:
        return 10 + _CN.get(s[1], 0)
    if s.endswith("十") and len(s) == 2:
        return _CN.get(s[0], 0) * 10
    if "十" in s:
        a, b = s.split("十", 1)
        return _CN.get(a, 0) * 10 + _CN.get(b, 0)
    try:
        return int(s)
    except ValueError:
        return None


def find_markdown(unit, glob_pattern):
    pats = list(BASE_DIR.glob(glob_pattern.format(unit=unit)))
    if not pats:
        raise FileNotFoundError(f"找不到剧本文件（模式 {glob_pattern}，unit={unit:02d}）")
    return pats[0]


_PROMPT_TITLE_KEYS = ("Ref2VA", "H3 Prompt", "H3 提示词")
_PROMPT_MARKERS = ("**H3 提示词**", "**H3 Ref2VA 提示词", "**H3提示词", "**H3 Prompt")

# 提示词有效内容从该字段开始；代码块前的上传前缀不进入 ComfyUI
_REQUIRED_FIRST_FIELD = "subject_definitions"


def _prompt_head_ok(prompt):
    """校验提取结果首行必须是 subject_definitions:（允许字段名后带空格）。"""
    return re.match(r"^%s\s*:" % _REQUIRED_FIRST_FIELD, prompt) is not None


def _is_prompt_section(head):
    """判断某个 ## 段落是否为 H3 提示词候选段，不以语言过滤。"""
    if re.search(r"第\s*[一二三四五六七八九十\d]+\s*段", head):
        return True
    if re.search(r"片段\s*\d+", head):
        return True
    return any(k in head for k in _PROMPT_TITLE_KEYS)


def _detect_language(head, prompt):
    """识别提示词语言；标题显式标识优先，未标识时按正文文字比例判断。"""
    title = head.lower()
    if "中文" in head or "chinese" in title or "[zh]" in title or "（zh）" in title:
        return "zh"
    if "英文" in head or "english" in title or "[en]" in title or "（en）" in title:
        return "en"

    # 字段名、标签本身会带英文，故仅在中文字符明显存在时认定为中文。
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", prompt))
    latin_count = len(re.findall(r"[A-Za-z]", prompt))
    return "zh" if cjk_count >= 8 and cjk_count * 2 >= latin_count else "en"


def _seg_index(head):
    """从标题提取段号，兼容 第N段 / 片段 NN / P05-5 / P04。

    不用通用数字兜底：标题里的 Ref2VA、H3 等词含数字，会被误当成段号。
    """
    m = re.search(r"第\s*([一二三四五六七八九十\d]+)\s*段", head)
    if m:
        return cn_to_int(m.group(1))
    m = re.search(r"片段\s*(\d+)", head)
    if m:
        return int(m.group(1))
    m = re.search(r"[Pp](\d+)\s*[-–—]\s*(\d+)", head)  # P05-5 -> 5
    if m:
        return int(m.group(2))
    m = re.search(r"[Pp](\d+)", head)  # P04 -> 4
    if m:
        return int(m.group(1))
    return None


def _extract_prompt_block(part, marker):
    """提取代码块，并从 subject_definitions: 开始，排除块内误放的上传前缀。"""
    code_start = part.find("```text", marker)
    if code_start < 0:
        return None
    content_start = part.find("\n", code_start) + 1
    code_end = part.find("```", content_start)
    if code_end < 0:
        return None
    raw = part[content_start:code_end].strip()
    # 正确边界：只把 subject_definitions: 及后续内容交给 ComfyUI。
    field = re.search(r"(?m)^[ \t]*(subject_definitions\s*:)", raw)
    if not field:
        return None
    prompt = raw[field.start(1):].strip()
    return prompt or None


def parse_markdown(path):
    """解析 markdown，返回 {段号: {"en": prompt, "zh": prompt}}。

    中英文版本均保留；语言由标题显式标记优先，未标记时按正文判断。
    代码块内若误放“本段上传”前缀，会从 subject_definitions: 开始截取。
    """
    text = path.read_text(encoding="utf-8")
    parts = re.split(r"(?m)^## ", text)
    result = {}
    for part in parts[1:]:
        head = part.split("\n", 1)[0]
        if not _is_prompt_section(head):
            continue
        idx = _seg_index(head)
        if idx is None:
            continue
        marker = -1
        for pat in _PROMPT_MARKERS:
            marker = part.find(pat)
            if marker >= 0:
                break
        if marker < 0:
            marker = 0  # 无标记：取本段第一个 ```text
        prompt = _extract_prompt_block(part, marker)
        if not prompt:
            continue
        first_line = prompt.splitlines()[0]
        if not _prompt_head_ok(prompt):
            print("[跳过] 第%s段：代码块中找不到首字段 %s: -> %r"
                  % (idx, _REQUIRED_FIRST_FIELD, first_line[:70]))
            continue
        lang = _detect_language(head, prompt)
        result.setdefault(idx, {})[lang] = prompt
    return result


def choose_prompts(variants):
    """按可用语言自动选择；中英并存时由用户选择。"""
    langs = set()
    for item in variants.values():
        langs.update(item)
    if langs == {"zh"}:
        selected = "zh"
    elif langs == {"en"}:
        selected = "en"
    elif langs == {"zh", "en"}:
        print("检测到中文和英文提示词版本，请选择提交语言：")
        print("1. 中文")
        print("2. 英文")
        while True:
            answer = input("请输入编号（1/2）：").strip()
            if answer in ("1", "2"):
                selected = "zh" if answer == "1" else "en"
                break
            print("请输入 1 或 2。")
    else:
        return {}
    selected_result = {}
    for idx, item in variants.items():
        if selected in item:
            selected_result[idx] = item[selected]
        else:
            print(f"[跳过] 第{idx}段没有{('中文' if selected == 'zh' else '英文')}版本")
    return selected_result


def resolve_template(w):
    """按 class_type / 输入槽位自动探测模板中的关键节点，不依赖固定 id。

    返回 (ref_id, save_id, prompt_loc, ref_slots)：
      ref_id       MiniMaxH3ReferenceToVideo 节点 id（无则 None）
      save_id      SaveVideo 节点 id（无则 None）
      prompt_loc   (节点id, 输入key)，提示词写入位置
      ref_slots    槽位名 -> 连接，如 {"ref_images.ref_image_0": ["308", 0]}
    """
    ref_id = save_id = None
    ref_slots = {}
    for nid, node in w.items():
        ct = node.get("class_type", "")
        if ct == REF_CLASS and ref_id is None:
            ref_id = nid
            for k, v in node.get("inputs", {}).items():
                if k.startswith("ref_images.ref_image_") or k.startswith("ref_audios.ref_audio_"):
                    ref_slots[k] = v
        elif ct == SAVE_CLASS and save_id is None:
            save_id = nid

    prompt_loc = None
    if ref_id is not None:
        ri = w[ref_id]["inputs"]
        if isinstance(ri.get("prompt"), str):
            prompt_loc = (ref_id, "prompt")          # 提示词内联在 ref 节点
        else:
            for key in ("text", "prompt_text"):
                link = ri.get(key)
                if isinstance(link, list) and link and link[0] in w:
                    tid = link[0]
                    for cand in ("text", "value", "prompt"):
                        if isinstance(w[tid].get("inputs", {}).get(cand), str):
                            prompt_loc = (tid, cand)  # 上游独立文本节点
                            break
                if prompt_loc:
                    break
    return ref_id, save_id, prompt_loc, ref_slots


def _slot_ordinal(slot):
    """ref_image_0 -> 1, ref_audio_1 -> 2（槽位 0-based，标签 1-based）"""
    m = re.search(r"(\d+)\s*$", slot)
    return int(m.group(1)) + 1


def _set_loader_file(w, loader_id, filename, kind):
    """往 LoadImage/LoadAudio 节点写文件名：优先模板里已存在的输入 key。"""
    loader = w.get(loader_id)
    if not loader:
        return
    inputs = loader.setdefault("inputs", {})
    if kind == "image":
        for key in ("image", "upload_image", "upload"):
            if key in inputs:
                inputs[key] = filename
                return
        inputs["image"] = filename
    else:
        for key in ("audio", "filename"):
            if key in inputs:
                inputs[key] = filename
                return
        inputs["audio"] = filename


def build_workflow(base, prompt, cfg, prefix):
    w = json.loads(json.dumps(base))  # 深拷贝模板
    ref_id, save_id, prompt_loc, ref_slots = resolve_template(w)
    if ref_id is None:
        raise ValueError("模板中找不到 %s 节点，请检查 template_file 是否指向正确的 MiniMax H3 工作流"
                         % REF_CLASS)
    if prompt_loc is None:
        raise ValueError("模板中找不到提示词写入位置（需要 ref 节点内联 prompt，或 text 槽位连接上游文本节点）")
    w[prompt_loc[0]]["inputs"][prompt_loc[1]] = prompt
    if save_id is not None:
        w[save_id]["inputs"]["filename_prefix"] = prefix

    ref_inputs = w[ref_id]["inputs"]
    referenced = set()
    for slot in sorted(ref_slots, key=_slot_ordinal):
        ordinal = _slot_ordinal(slot)
        is_img = slot.startswith("ref_images.")
        tag = "<Picture %d>" % ordinal if is_img else "<Audio %d>" % ordinal
        filename = ((cfg["pictures"].get(str(ordinal)) or "").strip()
                    if is_img else (cfg["audios"].get(str(ordinal)) or "").strip())
        link = ref_slots[slot]
        loader_id = link[0] if isinstance(link, list) and link else None
        if filename and tag in prompt:
            if loader_id is None or loader_id not in w:
                # 槽位存在但没接加载器：动态新建（音频常见）
                loader_id = str(uuid.uuid4().int % 10 ** 9)
                w[loader_id] = {"inputs": {},
                                "class_type": IMG_LOADER_CLASS if is_img else AUD_LOADER_CLASS,
                                "_meta": {"title": "載入圖片" if is_img else "載入音訊"}}
            _set_loader_file(w, loader_id, filename, "image" if is_img else "audio")
            referenced.add(loader_id)
            ref_inputs[slot] = [loader_id, 0]
        else:
            ref_inputs.pop(slot, None)

    # 删除不再被任何 ref 槽位引用的加载器节点
    for nid, node in list(w.items()):
        if node.get("class_type") in (IMG_LOADER_CLASS, AUD_LOADER_CLASS) and nid not in referenced:
            w.pop(nid, None)

    return w


def submit(wf, url):
    payload = json.dumps({"prompt": wf, "client_id": str(uuid.uuid4())}).encode("utf-8")
    req = urllib.request.Request(url + "/prompt", data=payload,
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=60))["prompt_id"]


def wait_history(pid, url, timeout=7200, interval=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            h = json.load(urllib.request.urlopen(f"{url}/history/{pid}", timeout=60))
        except urllib.error.URLError:
            time.sleep(interval)
            continue
        if pid in h:
            return h[pid]
        time.sleep(interval)
    return None


def extract_outputs(entry):
    outs = []
    for _nid, node in (entry.get("outputs") or {}).items():
        for kind in ("images", "gifs", "videos"):
            for it in node.get(kind, []):
                outs.append(f"{kind}/{it.get('filename')}")
    return outs


def describe_refs(prompt, cfg):
    pics = [v for k, v in cfg["pictures"].items() if (v or "").strip() and f"<Picture {k}>" in prompt]
    auds = [v for k, v in cfg["audios"].items() if (v or "").strip() and f"<Audio {k}>" in prompt]
    return pics, auds


def seg_cfg(cfg, idx):
    """返回该段的 config：全局映射 + 按段覆盖（segment_pictures/segment_audios）合并。

    config 示例：
      "segment_pictures": {"2": {"3": "顾昊（第一套）.png", "4": "S3_离婚登记处.png", "5": "王婉.png"}}
    表示第 2 段用段级映射覆盖全局 pictures 的 3/4/5 号槽位；未覆盖的编号沿用全局。
    """
    seg = dict(cfg)
    pics = dict(cfg.get("pictures") or {})
    auds = dict(cfg.get("audios") or {})
    over_pics = (cfg.get("segment_pictures") or {}).get(str(idx)) or {}
    over_auds = (cfg.get("segment_audios") or {}).get(str(idx)) or {}
    pics.update(over_pics)
    auds.update(over_auds)
    seg["pictures"] = pics
    seg["audios"] = auds
    return seg


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--url")]
    if not args:
        print(__doc__)
        return 2
    unit = int(args[0])
    action = args[1] if len(args) > 1 else "submit"
    only = [int(a) for a in args[2:]] or None

    cfg = load_config()
    url = cfg["comfyui_url"]
    for a in sys.argv[1:]:
        if a.startswith("--url="):
            url = a.split("=", 1)[1]

    path = find_markdown(unit, cfg["unit_glob"])
    variants = parse_markdown(path)
    segments = choose_prompts(variants)
    if not segments:
        print("未找到可提交的提示词段，已停止。")
        return 1
    template_path = BASE_DIR / cfg["template_file"]
    base = json.loads(template_path.read_text(encoding="utf-8"))
    prefix = f"{cfg['output_prefix']}/{date.today().isoformat()}/{unit:02d}单元"
    out_dir = BASE_DIR / f"{unit:02d}单元工作流"
    out_dir.mkdir(exist_ok=True)

    print(f"单元 {unit:02d} | 解析到 {len(segments)} 段 | 模板 {template_path.name} | 前缀 {prefix}")

    if action == "build":
        for idx in sorted(segments):
            scfg = seg_cfg(cfg, idx)
            wf = build_workflow(base, segments[idx], scfg, prefix)
            out = out_dir / f"{unit:02d}-{idx:02d}.json"
            out.write_text(json.dumps(wf, ensure_ascii=False, indent=2), encoding="utf-8")
            pics, auds = describe_refs(segments[idx], scfg)
            print(f"[生成] {out.name}  图={'+'.join(pics) or '无'} 音频={'+'.join(auds) or '无'}")
        print("未提交（加 submit 参数提交）。")
        return 0

    targets = sorted(only) if only else sorted(segments)
    for idx in targets:
        scfg = seg_cfg(cfg, idx)
        wf = build_workflow(base, segments[idx], scfg, prefix)
        out = out_dir / f"{unit:02d}-{idx:02d}.json"
        out.write_text(json.dumps(wf, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n=== 提交 第{idx}段 ===")
        try:
            pid = submit(wf, url)
            print(f"prompt_id = {pid}")
            entry = wait_history(pid, url)
            if entry is None:
                print("[超时] 未在时限内完成")
                continue
            status = (entry.get("status") or {}).get("status_str", "?")
            if status == "success":
                print(f"[成功] {status}  outputs={extract_outputs(entry)}")
            else:
                msgs = [m for m in entry.get("messages", [])
                        if m[0] in ("execution_error", "execution_cached")]
                print(f"[失败] status={status} messages={msgs[:3]}")
        except Exception as e:
            print(f"[异常] {e}")
    print("ALL DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
