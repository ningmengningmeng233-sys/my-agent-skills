#!/usr/bin/env node
// bridge.mjs - v2 结构候选桥（alpha.1 P04 + 补正 Q05）。
//
// stdin  : { "text": string, "scene": "novel"|"normal" }
// stdout : { "schema": "lieflat-textlint-bridge-v2",
//            "findings": [{ "rule_id", "range_utf16": [start, end],
//                           "evidence", "context_range_utf16"?: [start, end] }] }
//
// 合同：
// - 只 lint，不 fixFiles、不重新序列化整文——AST 只用于结构识别（设计 4 节）。
// - 不加载工作目录的 .textlintrc/插件：规则与插件全部由本文件显式 import，
//   descriptor 手工构造（用户配置不得影响扫描结果）。
// - 输出 range 是 UTF-16 码元偏移（ECMAScript 规范），Python 侧负责换算并回读验证；
//   桥自身绝不输出 Python 坐标，也不猜。
// - 规则在消息尾携带的 `@ctx:<start>-<end>` 是候选的上下文/参照范围（重复句的
//   首次出现、时间链的所在行），本桥解析成 context_range_utf16 并从 evidence
//   中剥掉该标记；没有该标记的字段缺省。
// - 任何错误输出到 stderr 并以非零退出，不输出半份 JSON。
import { createLinter } from "textlint";
import { TextlintKernelDescriptor } from "@textlint/kernel";
import mdPluginModule from "@textlint/textlint-plugin-markdown";
// CJS 互操作：包以 module.exports.default 形状暴露，Processor 在 default 上。
const markdownPlugin = mdPluginModule.default ?? mdPluginModule;
import {fileURLToPath} from "node:url";
import {readFile} from "node:fs/promises";
import zhCandidates from "../../rules/zh-candidates.mjs";
const NODE_MODULES_RE = /[\\/]node_modules[\\/]/;

const CONTEXT_MARK = /\s*@ctx:(\d+)-(\d+)\s*$/;

// --self-check：只回答「这些模块到底从哪儿加载的」，用来证明结构扫描没借开发树。
// 不做扫描、不写文件；输出交给 Python 侧 preflight 登记。
async function selfCheck() {
  const packages = ["textlint", "@textlint/kernel", "@textlint/textlint-plugin-markdown"];
  const manifest = JSON.parse(
    await readFile(new URL("./package.json", import.meta.url), "utf8"));
  const resolved = {};
  const versions = {};
  for (const name of packages) {
    resolved[name] = fileURLToPath(import.meta.resolve(name));
    const root = resolved[name].split(NODE_MODULES_RE)[0];
    versions[name] = manifest.dependencies[name] ?? null;
    if (!NODE_MODULES_RE.test(resolved[name])) {
      throw new Error(`依赖不是从 node_modules 解析的：${name} -> ${resolved[name]}`);
    }
    void root;
  }
  process.stdout.write(JSON.stringify({
    schema: "lieflat-textlint-selfcheck-v1",
    node: process.version,
    declared: manifest.dependencies,
    versions,
    resolved,
  }));
}

async function main() {
  if (process.argv.includes("--self-check")) { await selfCheck(); return; }
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  let request;
  try {
    request = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch (err) {
    throw new Error(`bridge 输入不是合法 JSON: ${err.message}`);
  }
  if (typeof request?.text !== "string") {
    throw new Error("bridge 请求缺少 text 字段");
  }
  const descriptor = new TextlintKernelDescriptor({
    rules: [{
      ruleId: "zh-candidates",
      rule: zhCandidates,
      options: { scene: request.scene ?? "normal" },
    }],
    plugins: [{
      pluginId: "markdown",
      plugin: markdownPlugin,
    }],
    filterRules: [],
  });
  const linter = createLinter({ descriptor });
  // textlint 15 签名：lintText(text, filePath)——虚拟文件名只决定 ext= .md，
  // 桥绝不触碰真实文件路径；返回单个 TextlintResult。
  const result = await linter.lintText(request.text, "v2-candidate.md");
  const findings = [];
  for (const message of result.messages) {
    // 具名检查 ID 藏在 message 的 [zh-*] 前缀里；evidence 是去前缀的说明。
    const named = /^\[([a-z0-9-]+)\]\s*/.exec(message.message);
    let evidence = named ? message.message.slice(named[0].length)
                         : message.message;
    const finding = {
      rule_id: named ? named[1] : message.ruleId,
      range_utf16: [message.range[0], message.range[1]],
      evidence,
    };
    const contextMark = CONTEXT_MARK.exec(evidence);
    if (contextMark) {
      finding.context_range_utf16 = [Number(contextMark[1]),
                                     Number(contextMark[2])];
      finding.evidence = evidence.replace(CONTEXT_MARK, "").trimEnd();
    }
    findings.push(finding);
  }
  process.stdout.write(JSON.stringify({
    schema: "lieflat-textlint-bridge-v2",
    findings,
  }));
}

main().catch((err) => {
  process.stderr.write(String(err?.stack ?? err) + "\n");
  process.exit(1);
});
