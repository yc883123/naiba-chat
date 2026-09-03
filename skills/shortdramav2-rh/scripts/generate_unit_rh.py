# -*- coding: utf-8 -*-
"""短剧单元批量生成（RunningHub AI 应用版，MiniMax H3 Ref2VA）。

以 comfyui-shortdramav2 的剧本解析 / <Picture N> / <Audio N> 标签映射逻辑为基础，
把「构建本地工作流 JSON + 提交本地 ComfyUI」替换为 RunningHub 云端流程：
上传参考资产 → 组装 nodeInfoList → 提交 AI 应用（webappId）→ 轮询 → 下载视频。

用法（先复制 config.example.json 为 config.json 并填好 webapp_id / 资产）：
  python generate_unit_rh.py 01              # 解析 + 提交全部段并等待下载
  python generate_unit_rh.py 01 build        # 只组装并落盘 nodeInfoList，不提交
  python generate_unit_rh.py 01 submit       # 解析 + 提交全部段
  python generate_unit_rh.py 01 submit 3     # 只提交第 3 段（测试）
  python generate_unit_rh.py --info [WEBAPP_ID]   # 探测 AI 应用节点并打印映射
可选参数：--site ai|cn  --api-key KEY  --instance-type default|plus  --lang zh|en
"""
import copy
import json
import re
import sys
import time
from datetime import date
from pathlib import Path

import rh_client as rh

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

PACKAGE_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG = {
    "site": "ai",
    "rh_api_key": "",
    "webapp_id": "",
    "instance_type": "default",
    "asset_dir": "assets",
    "output_prefix": "BOSS",
    "unit_glob": "单元{unit:02d}_*.md",
    "pictures": {"1": "", "2": "", "3": "", "4": "", "5": "", "6": "", "7": "", "8": "", "9": ""},
    "audios": {"1": "", "2": "", "3": ""},
    "segment_pictures": {},
    "segment_audios": {},
    "duration": None,
    "runninghub": {"prompt_node": "", "duration_node": "", "ref_images": {}, "ref_audios": {}},
}

_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


# ---------------------------------------------------------------------------
# 配置 / 剧本解析（与 comfyui-shortdramav2 的 generate_unit.py 同源）
# ---------------------------------------------------------------------------

def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    p = PACKAGE_ROOT / "config.json"
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
        print("[提示] 未找到 config.json，使用默认配置（请从 config.example.json 复制并填写）")
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
    pats = list(PACKAGE_ROOT.glob(glob_pattern.format(unit=unit)))
    if not pats:
        raise FileNotFoundError(f"找不到剧本文件（模式 {glob_pattern}，unit={unit:02d}）")
    return pats[0]


_PROMPT_TITLE_KEYS = ("Ref2VA", "H3 Prompt", "H3 提示词")
_PROMPT_MARKERS = ("**H3 提示词**", "**H3 Ref2VA 提示词", "**H3提示词", "**H3 Prompt")
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
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", prompt))
    latin_count = len(re.findall(r"[A-Za-z]", prompt))
    return "zh" if cjk_count >= 8 and cjk_count * 2 >= latin_count else "en"


def _seg_index(head):
    """从标题提取段号，兼容 第N段 / 片段 NN / P05-5 / P04。"""
    m = re.search(r"第\s*([一二三四五六七八九十\d]+)\s*段", head)
    if m:
        return cn_to_int(m.group(1))
    m = re.search(r"片段\s*(\d+)", head)
    if m:
        return int(m.group(1))
    m = re.search(r"[Pp](\d+)\s*[-–—]\s*(\d+)", head)
    if m:
        return int(m.group(2))
    m = re.search(r"[Pp](\d+)", head)
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
    field = re.search(r"(?m)^[ \t]*(subject_definitions\s*:)", raw)
    if not field:
        return None
    prompt = raw[field.start(1):].strip()
    return prompt or None


def parse_markdown(path):
    """解析 markdown，返回 {段号: {"en": prompt, "zh": prompt}}。"""
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
            marker = 0
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


def choose_prompts(variants, lang=None):
    """按可用语言自动选择；中英并存时按 lang 参数选择，未指定则由用户输入。"""
    langs = set()
    for item in variants.values():
        langs.update(item)
    if langs == {"zh"}:
        selected = "zh"
    elif langs == {"en"}:
        selected = "en"
    elif langs == {"zh", "en"}:
        if lang in ("zh", "en"):
            selected = lang
        else:
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


def describe_refs(prompt, cfg):
    pics = [v for k, v in cfg["pictures"].items() if (v or "").strip() and f"<Picture {k}>" in prompt]
    auds = [v for k, v in cfg["audios"].items() if (v or "").strip() and f"<Audio {k}>" in prompt]
    return pics, auds


def seg_cfg(cfg, idx):
    """返回该段的 config：全局映射 + 按段覆盖（segment_pictures/segment_audios）合并。"""
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


# ---------------------------------------------------------------------------
# RunningHub 流程
# ---------------------------------------------------------------------------

def resolve_asset_path(cfg, name):
    p = Path(name)
    if p.is_absolute():
        return p
    base = cfg.get("asset_dir") or "assets"
    base_p = Path(base)
    if base_p.is_absolute():
        return base_p / name
    return PACKAGE_ROOT / base / name


def _fresh_upload(time_str):
    if not time_str:
        return False
    try:
        t0 = time.mktime(time.strptime(time_str, "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return False
    return time.time() - t0 < 24 * 3600  # RunningHub 上传链接 1 天有效


def load_upload_cache(out_dir):
    p = out_dir / "rh_uploads.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def persist_upload_cache(out_dir, cache):
    (out_dir / "rh_uploads.json").write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def get_uploaded(cfg, path, api_key, site, upload_cache, out_dir):
    """按资产绝对路径去重上传；缓存未过期（24h）则复用，否则重新上传。"""
    key = str(path.resolve())
    cached = upload_cache.get(key)
    if cached and _fresh_upload(cached.get("time")):
        return cached["fileName"]
    fname = rh.upload_file(api_key, str(path), site)
    upload_cache[key] = {"fileName": fname, "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
    persist_upload_cache(out_dir, upload_cache)
    return fname


def resolve_node_mapping(cfg, node_list):
    """自动探测 + config 覆盖，返回 (prompt_loc, img_slots, aud_slots)。"""
    rh_cfg = cfg.get("runninghub") or {}
    prompt_loc = rh.detect_prompt_node(node_list, rh_cfg.get("prompt_node"))
    img_slots = rh.detect_ref_slots(node_list, "image", rh_cfg.get("ref_images"))
    aud_slots = rh.detect_ref_slots(node_list, "audio", rh_cfg.get("ref_audios"))
    return prompt_loc, img_slots, aud_slots


def detect_duration_node(cfg, node_list):
    """定位时长（秒）输入节点，返回 (nodeId, fieldName) 或 None。

    优先 config.runninghub.duration_node（nodeId:fieldName），否则探测
    fieldType=FLOAT 且描述含「时长/duration」的节点。
    """
    override = ((cfg.get("runninghub") or {}).get("duration_node") or "").strip()
    if override:
        nid, fname = rh.parse_loc(override)
        if rh.find_node(node_list, nid, fname) is None:
            raise rh.RhError("NODE_CONFIG",
                             f"配置的时长节点 {override} 不在该应用节点列表中，请先用 --info 核对。")
        return nid, fname
    for n in node_list:
        desc = str(n.get("description") or n.get("descriptionCn") or "").lower()
        if n.get("fieldType") == "FLOAT" and any(k in desc for k in ("时长", "duration")):
            return str(n["nodeId"]), n["fieldName"]
    return None


def assemble_node_info_list(prompt, node_list, prompt_loc, img_slots, aud_slots, scfg,
                            api_key, site, upload_cache, out_dir, duration_loc=None):
    """组装本段 nodeInfoList：改提示词 + 时长 + 上传并填入提示词中出现的参考槽位。

    返回 (node_info_list, used_images, used_audios, warnings)。
    """
    nodes = copy.deepcopy(node_list)
    by_key = {(str(n.get("nodeId")), n.get("fieldName")): n for n in nodes}
    used_images, used_audios, warnings = [], [], []

    pnid, pfname = prompt_loc
    by_key[(pnid, pfname)]["fieldValue"] = prompt

    duration = scfg.get("duration")
    if duration_loc and duration is not None:
        dnid, dfname = duration_loc
        if (dnid, dfname) in by_key:
            by_key[(dnid, dfname)]["fieldValue"] = str(duration)

    def fill_slot(tag, nid, fname, tag_text, kind_name, mapping):
        """提示词中出现对应标签且配置了资产时，上传并填入槽位；否则返回 None。"""
        if tag_text not in prompt:
            return None
        name = (mapping.get(str(tag)) or "").strip()
        if not name:
            warnings.append(f"<{kind_name} {tag}> 出现在提示词中但未配置文件名，保留应用默认值")
            return None
        path = resolve_asset_path(scfg, name)
        if not path.exists():
            warnings.append(f"<{kind_name} {tag}> 资产不存在: {path}（保留应用默认值）")
            return None
        by_key[(nid, fname)]["fieldValue"] = get_uploaded(scfg, path, api_key, site, upload_cache, out_dir)
        return name

    for tag, (nid, fname) in img_slots.items():
        name = fill_slot(tag, nid, fname, f"<Picture {tag}>", "Picture", scfg.get("pictures") or {})
        if name:
            used_images.append(name)
    for tag, (nid, fname) in aud_slots.items():
        name = fill_slot(tag, nid, fname, f"<Audio {tag}>", "Audio", scfg.get("audios") or {})
        if name:
            used_audios.append(name)

    return nodes, used_images, used_audios, warnings


def guess_ext_from_url(url):
    path = url.split("?")[0]
    if "." in path.split("/")[-1]:
        return path.split("/")[-1].rsplit(".", 1)[-1].lower()
    return "mp4"


def save_node_info(out_dir, unit, idx, nodes):
    out = out_dir / f"{unit:02d}-{idx:02d}.nodeinfo.json"
    out.write_text(json.dumps(nodes, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def write_rh_nodes(out_dir, node_list, prompt_loc, img_slots, aud_slots, duration_loc=None):
    mapping = {
        "prompt": {"nodeId": prompt_loc[0], "fieldName": prompt_loc[1]},
        "ref_images": {str(t): {"nodeId": nid, "fieldName": fn} for t, (nid, fn) in img_slots.items()},
        "ref_audios": {str(t): {"nodeId": nid, "fieldName": fn} for t, (nid, fn) in aud_slots.items()},
    }
    if duration_loc:
        mapping["duration"] = {"nodeId": duration_loc[0], "fieldName": duration_loc[1]}
    payload = {"mapping": mapping, "nodeCount": len(node_list), "nodes": node_list}
    (out_dir / "rh_nodes.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def download_results(out_dir, unit, idx, final):
    results = final.get("results") or []
    usage = final.get("usage") or {}
    consume_money = usage.get("consumeMoney") or usage.get("thirdPartyConsumeMoney")
    task_cost_time = usage.get("taskCostTime")

    file_urls = [(it.get("url") or it.get("outputUrl"), it.get("outputType", ""))
                 for it in results if (it.get("url") or it.get("outputUrl"))]
    if not file_urls:
        print("[提示] 任务没有文件输出，原始 results：")
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return
    for i, (url, out_type) in enumerate(file_urls):
        ext = out_type or guess_ext_from_url(url)
        if len(file_urls) == 1:
            base = out_dir / f"{unit:02d}-{idx:02d}.{ext}"
        else:
            base = out_dir / f"{unit:02d}-{idx:02d}_{i + 1}.{ext}"
        full = rh.download_file(url, str(base))
        if rh.fix_mov_to_mp4(full):
            # 容器被改写过，补一个 .mp4 后缀名
            fixed = str(Path(full).with_suffix(".mp4"))
            try:
                Path(full).rename(fixed)
                full = fixed
            except OSError:
                pass
        print(f"[成功] 输出 {full}")
    if consume_money is not None:
        print(f"[消耗] ¥{consume_money}")
    if task_cost_time and str(task_cost_time) != "0":
        print(f"[耗时] {task_cost_time}s")


def run_unit(unit, action, only, cfg, flags):
    site = rh.resolve_site(flags.get("site"), cfg.get("site"))
    api_key = rh.resolve_api_key(flags.get("api_key") or cfg.get("rh_api_key"))
    webapp_id = str(cfg.get("webapp_id") or "").strip()
    instance_type = flags.get("instance_type") or cfg.get("instance_type") or "default"

    path = find_markdown(unit, cfg["unit_glob"])
    variants = parse_markdown(path)
    segments = choose_prompts(variants, flags.get("lang"))
    if not segments:
        print("未找到可提交的提示词段，已停止。")
        return 1

    out_dir = PACKAGE_ROOT / f"{unit:02d}单元输出"
    out_dir.mkdir(exist_ok=True)
    upload_cache = load_upload_cache(out_dir)
    prefix = f"{cfg['output_prefix']}/{date.today().isoformat()}/{unit:02d}单元"
    print(f"单元 {unit:02d} | 解析到 {len(segments)} 段 | 站点 {site} | webappId {webapp_id or '未配置'} | 前缀 {prefix}")

    node_list = None
    prompt_loc = img_slots = aud_slots = None
    if webapp_id:
        if not api_key:
            raise rh.RhError("NO_API_KEY", "已配置 webapp_id，但未找到 RunningHub API Key。")
        node_list = rh.get_node_info(api_key, webapp_id, site)
        prompt_loc, img_slots, aud_slots = resolve_node_mapping(cfg, node_list)
        duration_loc = detect_duration_node(cfg, node_list)
        write_rh_nodes(out_dir, node_list, prompt_loc, img_slots, aud_slots, duration_loc)
        print(f"节点映射：prompt={prompt_loc[0]}:{prompt_loc[1]} "
              f"时长={duration_loc} 图槽={len(img_slots)} 音槽={len(aud_slots)}"
              f"（详见 {out_dir.name}/rh_nodes.json）")
    else:
        print("[提示] 未配置 webapp_id：仅做本地解析/资产核对，跳过节点探测与上传。"
              "配好 config.json 的 webapp_id 后才会探测节点并提交。")

    if action == "build":
        for idx in sorted(segments):
            scfg = seg_cfg(cfg, idx)
            prompt = segments[idx]
            pics, auds = describe_refs(prompt, scfg)
            line = f"[生成] 第{idx}段  图={'+'.join(pics) or '无'} 音频={'+'.join(auds) or '无'}"
            if node_list is None:
                print(line + "  （未配置 webapp_id，跳过节点组装）")
                continue
            nodes, used_i, used_a, warnings = assemble_node_info_list(
                prompt, node_list, prompt_loc, img_slots, aud_slots, scfg,
                api_key, site, upload_cache, out_dir, duration_loc)
            out = save_node_info(out_dir, unit, idx, nodes)
            print(line + f"  -> {out.name}")
            for w in warnings:
                print(f"[提示] 第{idx}段 {w}")
        persist_upload_cache(out_dir, upload_cache)
        print("未提交（加 submit 参数提交）。")
        return 0

    targets = sorted(only) if only else sorted(segments)
    for idx in targets:
        scfg = seg_cfg(cfg, idx)
        prompt = segments[idx]
        print(f"\n=== 提交 第{idx}段 ===")
        try:
            nodes, used_i, used_a, warnings = assemble_node_info_list(
                prompt, node_list, prompt_loc, img_slots, aud_slots, scfg,
                api_key, site, upload_cache, out_dir, duration_loc)
            for w in warnings:
                print(f"[提示] 第{idx}段 {w}")
            if node_list is None:
                print("[失败] 未配置 webapp_id，无法提交。请在 config.json 填写 webapp_id。")
                continue
            save_node_info(out_dir, unit, idx, nodes)
            task_id = rh.submit_task(api_key, webapp_id, nodes, site, instance_type)
            print(f"task_id = {task_id}")
            final = rh.poll_task(api_key, task_id, site)
            download_results(out_dir, unit, idx, final)
        except rh.RhError as e:
            print(f"[失败] {e.code}: {e.message}")
            for s in e.steps:
                print(f"  - {s}")
    persist_upload_cache(out_dir, upload_cache)
    print("ALL DONE")
    return 0


def run_info(cfg, flags, positional):
    site = rh.resolve_site(flags.get("site"), cfg.get("site"))
    api_key = rh.require_api_key(flags.get("api_key") or cfg.get("rh_api_key"))
    webapp_id = (positional[0] if positional else "").strip() or str(cfg.get("webapp_id") or "").strip()
    if not webapp_id:
        print("用法：python generate_unit_rh.py --info [WEBAPP_ID]")
        return 2
    node_list = rh.get_node_info(api_key, webapp_id, site)
    print(json.dumps({"webappId": webapp_id, "site": site, "nodeCount": len(node_list),
                      "nodes": node_list}, ensure_ascii=False, indent=2))
    try:
        prompt_loc, img_slots, aud_slots = resolve_node_mapping(cfg, node_list)
        print("\n# 节点映射")
        print(f"prompt: {prompt_loc[0]}:{prompt_loc[1]}")
        for t, (nid, fn) in img_slots.items():
            print(f"Picture {t}: {nid}:{fn}")
        for t, (nid, fn) in aud_slots.items():
            print(f"Audio {t}: {nid}:{fn}")
    except rh.RhError as e:
        print(f"[提示] 节点映射探测失败：{e.message}")
    return 0


def parse_args(argv):
    flags = {"site": None, "api_key": None, "instance_type": None, "lang": None}
    positional = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            body = a[2:]
            if "=" in body:
                k, v = body.split("=", 1)
                if k in flags:
                    flags[k] = v
            elif body in flags and i + 1 < len(argv):
                flags[body] = argv[i + 1]
                i += 1
        else:
            positional.append(a)
        i += 1
    return positional, flags


def main():
    argv = sys.argv[1:]
    if "--info" in argv:
        positional, flags = parse_args([a for a in argv if a != "--info"])
        return run_info(load_config(), flags, positional)

    positional, flags = parse_args(argv)
    if not positional:
        print(__doc__)
        return 2
    try:
        unit = int(positional[0])
    except ValueError:
        print(f"单元号必须是数字，收到: {positional[0]}")
        return 2
    action = positional[1] if len(positional) > 1 else "submit"
    only = [int(a) for a in positional[2:] if a.isdigit()] or None
    if action not in ("build", "submit"):
        print(f"未知动作: {action}（支持 build / submit）")
        return 2
    return run_unit(unit, action, only, load_config(), flags)


if __name__ == "__main__":
    raise SystemExit(main())
