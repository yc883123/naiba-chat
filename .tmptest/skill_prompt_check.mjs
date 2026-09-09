// 校验「安装 Skill / 编辑 Skill」两个按钮发给 AI 的说明：脚本规范是否都在、两处是否共用同一份。
// 运行：node .tmptest\skill_prompt_check.mjs
import { readFileSync } from 'node:fs';

const JS_DIR = new URL('../public/js/', import.meta.url);

function extractConst(source, name, scope = {}) {
  const start = source.indexOf(`export const ${name} =`);
  if (start < 0) return null;
  // 取到下一个顶层 `export ` 或文件末尾
  const rest = source.slice(start + `export const ${name} =`.length);
  const next = rest.search(/\nexport (const|function|let|async function) /);
  const body = (next >= 0 ? rest.slice(0, next) : rest).replace(/;\s*$/, '');
  return new Function(...Object.keys(scope), `return (${body});`)(...Object.values(scope));
}

const streamSource = readFileSync(new URL('11-run-stream.js', JS_DIR), 'utf8');
const chatSource = readFileSync(new URL('12-chat-input.js', JS_DIR), 'utf8');

const rules = extractConst(streamSource, 'SKILL_SCRIPT_RULES');
const scope = { SKILL_SCRIPT_RULES: rules };
const installPrompt = extractConst(streamSource, 'SKILL_INSTALL_PRESET', scope);
const editPrompt = extractConst(chatSource, 'SKILL_EDIT_PRESET', scope);

const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

check('SKILL_SCRIPT_RULES 存在', typeof rules === 'string' && rules.length > 0);
check('安装说明存在', typeof installPrompt === 'string' && installPrompt.length > 0);
check('编辑说明存在', typeof editPrompt === 'string' && editPrompt.length > 0);

for (const [label, prompt] of [['安装说明', installPrompt], ['编辑说明', editPrompt]]) {
  check(`${label}含"scripts/ 子目录"规范`, /scripts\/\s*子目录/.test(prompt), prompt?.slice(0, 120));
  check(`${label}含 encoding="utf-8" 要求`, prompt.includes('encoding="utf-8"'), '');
  check(`${label}说明 GBK 风险`, prompt.includes('GBK'), '');
  check(`${label}末尾复用同一份规范块`, prompt.trimEnd().endsWith(rules.trimEnd()), prompt?.slice(-80));
}

console.log();
console.log('===== 共用脚本规范 =====');
console.log(rules);
console.log();
console.log(`Skill 说明校验：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`);
process.exitCode = failures.length ? 1 : 0;
