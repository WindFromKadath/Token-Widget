// runner.js —— DESIGN.md §4.2 custom_script 执行器
// 用法: node runner.js   （输入经 stdin 传入：含密钥的脚本不落临时文件，§9）
//   stdin: { code, timeoutSec }   code 为完整 ({request, extractor}) 对象字面量
//   占位符替换已在 Python 侧完成（对 code 做字符串级替换）
// 输出: extractor 返回值 JSON -> stdout；错误 -> stderr 并非零退出
"use strict";

const fs = require("fs");

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const { code, timeoutSec } = input;

const script = eval(code); // code 来自 cc-switch.db（CC Switch 自身协议）
const req = script.request || {};

const ctrl = new AbortController();
const timer = setTimeout(() => ctrl.abort(), (timeoutSec || 10) * 1000);

const body =
  req.body === undefined
    ? undefined
    : typeof req.body === "string"
      ? req.body
      : JSON.stringify(req.body);

fetch(req.url, {
  method: req.method || "GET",
  headers: req.headers || {},
  body,
  signal: ctrl.signal,
})
  .then(async (res) => {
    const text = await res.text();
    let parsed;
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = text;
    }
    const out = script.extractor(parsed);
    process.stdout.write(JSON.stringify(out));
  })
  .catch((err) => {
    console.error(String(err));
    process.exit(1);
  })
  .finally(() => clearTimeout(timer));
