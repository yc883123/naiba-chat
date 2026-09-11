// 把 docs/manual/index.html 打印成 A4 PDF。
// 走 Chrome CLI 的 --print-to-pdf（不是 CDP 的 Page.printToPDF）：
// 实测 CDP printToPDF 在本手册页面上会无限挂起（tiny 页面 49ms、本页 45s+ 不返回），
// CLI 方式稳定。关键点：**必须给独立的 --user-data-dir**，否则会被本机已运行的
// 普通 Chrome 接管，表现就是命令挂住不返回、PDF 不生成。
// 用法：node scripts/make_pdf.mjs
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const HTML = path.join(ROOT, 'docs', 'manual', 'index.html');
const OUT = path.join(ROOT, 'docs', 'manual', 'Naiba-Chat-手册-2.2.0.pdf');

const CHROME = process.env.NAIBA_CHROME
  || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const profile = path.join(os.tmpdir(), `naiba-pdf-${Date.now()}`);

const args = [
  '--headless=new',
  '--no-sandbox',
  '--disable-gpu',
  `--user-data-dir=${profile.replace(/\\/g, '/')}`,
  '--no-first-run',
  '--no-default-browser-check',
  `--print-to-pdf=${OUT.replace(/\\/g, '/')}`,
  '--no-pdf-header-footer',
  `file:///${HTML.replace(/\\/g, '/')}`,
];

await new Promise((resolve, reject) => {
  const child = spawn(CHROME, args, { stdio: 'ignore' });
  const timer = setTimeout(() => { child.kill(); reject(new Error('打印超时（300s）')); }, 300000);
  child.on('exit', () => { clearTimeout(timer); resolve(); });
  child.on('error', (err) => { clearTimeout(timer); reject(err); });
});

if (!fs.existsSync(OUT)) throw new Error('PDF 未生成：' + OUT);
console.log('wrote', OUT, (fs.statSync(OUT).size / 1024 / 1024).toFixed(2), 'MB');
try { fs.rmSync(profile, { recursive: true, force: true }); } catch (_) {}
process.exit(0);
