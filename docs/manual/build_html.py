"""README.md → docs/manual/index.html（带左侧目录、阅读样式、图片灯箱）。"""
import re
import html as html_mod
from pathlib import Path
import markdown

ROOT = Path(__file__).resolve().parent
MD_PATH = ROOT / 'README.md'
HTML_PATH = ROOT / 'index.html'
IMG_DIR = ROOT / 'images'

md_text = MD_PATH.read_text(encoding='utf-8')

# 把 19/20/21 引用补进文档末尾占位
placeholder = '''<!--
待补配图（涉及真实会话内容，需授权或自行补充）：
- 示例对话截图 1：流式渲染（思考块 + 工具块 + 正文交错）
- 示例对话截图 2：产物卡片（图片/视频缩略图 + 预览 + 下载）
- 示例对话截图 3：工具审批确认弹窗
建议命名：19-chat-streaming.png / 20-chat-artifacts.png / 21-tool-approval.png
-->'''
if '19-chat-streaming.png' not in md_text:
    md_text = md_text.replace(placeholder, '').rstrip() + '\n'

# === 渲染 ===
md = markdown.Markdown(extensions=['extra', 'toc', 'sane_lists', 'tables'], output_format='html5')
body_html = md.convert(md_text)

# 优先用 webp（已转码时大幅瘦身）；若 webp 不存在则保留 png
from pathlib import Path as _P
body_html = re.sub(
    r'src="(images/[^"]+?)\.png"',
    lambda m: f'src="{m.group(1)}.webp"' if (_P(ROOT) / f'{m.group(1)}.webp').exists() else m.group(0),
    body_html,
)

# 提取 toc（再跑一遍用 markdown 的 toc 标记）
# markdown 的 toc 扩展不直接给列表；自己解析 heading
toc_items = []
for m in re.finditer(r'<h([1-3]) id="([^"]+)">(.*?)</h\1>', body_html, re.S):
    level, hid, text = m.group(1), m.group(2), m.group(3)
    # 去掉内部标签（如 <code>）
    clean = re.sub(r'<[^>]+>', '', text)
    toc_items.append((int(level), hid, clean))

def build_toc(items):
    out = ['<ul class="toc">']
    for level, hid, text in items:
        if level == 1:
            out.append('</ul><ul class="toc toc-l1">')
        out.append(f'<li><a href="#{hid}">{html_mod.escape(text)}</a></li>')
    out.append('</ul>')
    return '\n'.join(out)

toc_html = build_toc(toc_items)

# === 模板 ===
TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Naiba Chat 应用说明书 · 1.6.9 Beta</title>
<style>
  :root {
    --bg: #fbfaf7;
    --surface: #ffffff;
    --text: #1a1a1a;
    --muted: #6b6b6b;
    --line: #e6e3dc;
    --line-strong: #cdc8bb;
    --accent: #15815b;
    --accent-soft: #d4ebe0;
    --amber: #b3791a;
    --code-bg: #f4f1ea;
    --kbd-bg: #eee8d9;
    --shadow: 0 1px 3px rgba(0,0,0,.06), 0 8px 24px rgba(0,0,0,.04);
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", "Hiragino Sans GB", sans-serif;
    line-height: 1.65; font-size: 15px; }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  code { font-family: "JetBrains Mono", "Cascadia Code", "Source Code Pro", Consolas, "Courier New", monospace;
    background: var(--code-bg); padding: 1px 5px; border-radius: 3px; font-size: 0.9em; }
  pre { background: var(--code-bg); border: 1px solid var(--line); border-radius: 6px;
    padding: 12px 14px; overflow-x: auto; font-size: 13px; line-height: 1.5; }
  pre code { background: transparent; padding: 0; font-size: 13px; }
  table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 14px; }
  th, td { border: 1px solid var(--line); padding: 7px 10px; text-align: left; vertical-align: top; }
  th { background: #f1ede2; font-weight: 600; }
  tr:nth-child(even) td { background: #faf8f3; }
  blockquote { border-left: 3px solid var(--accent-soft); background: #f3f8f5;
    margin: 12px 0; padding: 8px 14px; color: #355246; }
  hr { border: 0; border-top: 1px solid var(--line); margin: 24px 0; }
  img { max-width: 100%; height: auto; border-radius: 4px; cursor: zoom-in; display: block; margin: 12px auto; }
  kbd { font-family: "JetBrains Mono", Consolas, monospace; background: var(--kbd-bg);
    border: 1px solid var(--line-strong); border-bottom-width: 2px; border-radius: 3px;
    padding: 1px 6px; font-size: 12px; }
  ul, ol { padding-left: 1.5em; }
  li { margin: 4px 0; }

  .layout { display: flex; min-height: 100vh; }

  /* 侧边目录 */
  nav.toc-nav {
    position: sticky; top: 0; align-self: flex-start;
    width: 260px; flex: 0 0 260px; height: 100vh; overflow-y: auto;
    background: var(--surface); border-right: 1px solid var(--line);
    padding: 20px 16px; font-size: 13px;
  }
  nav.toc-nav .brand { font-weight: 700; font-size: 15px; color: var(--accent);
    margin-bottom: 4px; line-height: 1.3; }
  nav.toc-nav .sub { color: var(--muted); font-size: 12px; margin-bottom: 14px; }
  nav.toc-nav .toc { list-style: none; padding-left: 0; margin: 6px 0; }
  nav.toc-nav .toc.toc-l1 { margin-top: 14px; padding-top: 10px; border-top: 1px solid var(--line); }
  nav.toc-nav .toc li { margin: 2px 0; }
  nav.toc-nav .toc a { display: block; padding: 4px 8px; color: var(--text);
    border-left: 2px solid transparent; line-height: 1.4; }
  nav.toc-nav .toc a:hover { background: #f1ede2; border-left-color: var(--accent-soft); text-decoration: none; }
  nav.toc-nav .toc .toc-l1 a { font-weight: 600; color: var(--accent); }
  nav.toc-nav .search { margin-top: 14px; }
  nav.toc-nav .search input { width: 100%; padding: 5px 8px; font-size: 12px;
    border: 1px solid var(--line); border-radius: 4px; background: var(--bg); }

  /* 主区 */
  main { flex: 1; padding: 32px 48px 80px; max-width: 980px; }
  main h1 { font-size: 30px; border-bottom: 2px solid var(--line); padding-bottom: 12px; margin-top: 0; }
  main h2 { font-size: 22px; border-bottom: 1px solid var(--line); padding-bottom: 6px; margin-top: 36px;
    scroll-margin-top: 20px; }
  main h3 { font-size: 17px; color: var(--accent); margin-top: 24px; scroll-margin-top: 20px; }
  main h4 { font-size: 15px; margin-top: 18px; }

  /* 提示 */
  blockquote > p:first-child { margin-top: 0; }
  blockquote > p:last-child { margin-bottom: 0; }

  /* 灯箱 */
  .lightbox { position: fixed; inset: 0; background: rgba(20,20,20,.88); z-index: 1000;
    display: none; align-items: center; justify-content: center; padding: 20px; }
  .lightbox.open { display: flex; }
  .lightbox img { max-width: 95vw; max-height: 95vh; margin: 0; cursor: zoom-out; }
  .lightbox-close { position: fixed; top: 14px; right: 18px; background: rgba(255,255,255,.15);
    color: #fff; border: 0; border-radius: 50%; width: 36px; height: 36px;
    font-size: 22px; cursor: pointer; }

  /* 下载 PDF 浮动按钮 */
  a.dl-pdf {
    position: fixed; right: 22px; bottom: 22px; z-index: 900;
    background: var(--accent); color: #fff; font-weight: 600; font-size: 14px;
    padding: 10px 18px; border-radius: 999px; box-shadow: 0 4px 14px rgba(21,129,91,.35);
  }
  a.dl-pdf:hover { background: #126440; text-decoration: none; }

  /* 响应 */
  @media (max-width: 900px) {
    .layout { flex-direction: column; }
    nav.toc-nav { width: 100%; height: auto; max-height: 50vh; position: relative; }
    main { padding: 20px; }
  }
</style>
</head>
<body>
<div class="layout">
  <nav class="toc-nav">
    <div class="brand">Naiba Chat<br>应用说明书</div>
    <div class="sub">1.6.5 Beta · Windows</div>
    <input class="search" type="search" placeholder="搜索标题（Ctrl+F）" onfocus="this.select()">
    __TOC__
    <a class="dl-pdf" href="__PDFNAME__" download>下载 PDF</a>
  </nav>
  <main class="doc">
    __BODY__
  </main>
</div>
<div class="lightbox" id="lb" onclick="document.getElementById('lb').classList.remove('open')">
  <button class="lightbox-close" aria-label="关闭">×</button>
  <img id="lb-img" alt="">
</div>
<script>
  document.querySelectorAll('main img').forEach((img) => {
    img.addEventListener('click', () => {
      const lb = document.getElementById('lb');
      document.getElementById('lb-img').src = img.src;
      lb.classList.add('open');
    });
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') document.getElementById('lb').classList.remove('open');
  });
</script>
</body>
</html>
"""

# 找出 README 里的 h1（应该是主标题）和它的 id，前置页要排除在 toc l1 之外
# 实际上 toc 第一个 h1（"Naiba Chat 应用说明书"）保留也没坏，作为回到顶部
PDF_NAME = 'Naiba-Chat-手册-1.6.5.pdf'
html = (TEMPLATE
        .replace('__TOC__', toc_html)
        .replace('__BODY__', body_html)
        .replace('__PDFNAME__', PDF_NAME))
HTML_PATH.write_text(html, encoding='utf-8')
print('wrote', HTML_PATH, 'size', HTML_PATH.stat().st_size, 'B,', len(toc_items), 'toc items')
