#!/usr/bin/env node
// zh-candidates.mjs - 中文结构候选规则（alpha.1 P04 + 补正 Q05）。
//
// textlint 规则：只产生结构候选（候选不携带任何修改授权，设计 5 节）。
// 十三类问题的完整中文判据在 references/v2-candidate-rules-zh.md 里与本规则
// ID 建立对应，不在此重复。
//
// 补正 Q05（独立复核 R05）：
// - **AST 正文范围**：只遍历 Paragraph 节点；代码围栏（CodeBlock）、HTML 块
//   （HtmlBlock）、表格（Table）与标题（非 Paragraph 的标题节点）整体跳过，
//   不再把 Document 全文当纯正文扫描——技术材料不是小说病灶。
// - **段内保护区**：段内的 InlineCode / Image / Html 按绝对 range 用等长空白
//   屏蔽（坐标不漂移），链接只检查可见文字（url 不在任何文本子节点里）。
// - **阈值与判据文档一致**：机械时间链在同行命中 ≥3 个连接词才报（2 词不报）。
// - **候选可消费**：重复句候选在消息尾携带 `@ctx:<start>-<end>`（首次出现
//   的参照范围），时间链候选携带所在行范围——由 bridge.mjs 解析为
//   context_range_utf16，Python 侧换算并回读验证。
//
// textlint 15 规则合同：context 不再提供 Syntax/RuleError——节点类型直接用
// AST 类型字符串（@textlint/ast-node-types 的同名常量值；本文件保持零外部
// 导入，使仓库根 rules/ 目录不依赖 node_modules 解析半径）；
// report 直接接受 { message, padding: locator.range([start, end]) } 普通对象。
// 输出 range 是相对 Document 的 UTF-16 偏移（Document.range 从 0 起，即
// 全文绝对偏移），Python 侧负责 UTF16→Python 换算与回读验证。
//
// options: { scene: "novel" | "normal" }

const Syntax = { Document: "Document", Paragraph: "Paragraph" };

// 段内保护区：这些子树里的 Str 不算正文（行内代码/行内 HTML/图片 alt）。
// 注意 @textlint/markdown-to-ast 的行内代码类型是 "Code"（带 value、无 Str 子节点）。
const INLINE_PROTECTED = new Set(["Code", "Html", "Image"]);
// 块级保护区：整个子树跳过。
const BLOCK_PROTECTED = new Set(["CodeBlock", "HtmlBlock", "Table"]);

const CONNECTIVES = ["然后", "随后", "紧接着", "接着"];
const MIN_TEMPORAL_HITS = 3;  // 同行连接词 ≥3 才报（判据文档口径）
const MIN_REPEAT_LEN = 4; // 重复句判定：句子至少 4 个字符（UTF-16 长度，含句号）

function splitSentences(text) {
  const parts = [];
  let start = 0;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    // 换行也是句子边界：标题行（无句号）不得与后续正文粘连成跨段长句。
    if (ch === "。" || ch === "！" || ch === "？" || ch === "；"
        || ch === "\n" || ch === "\r") {
      parts.push({ text: text.slice(start, i + 1), start });
      start = i + 1;
    }
  }
  if (start < text.length) parts.push({ text: text.slice(start), start });
  return parts;
}

function stripNoise(sentence) {
  // 句内比较去掉空白与引号差异，避免同句因引号不同而漏报。
  return sentence.replace(/[\s“”‘’"'「」『』]/g, "");
}

function* walk(node) {
  if (!node || typeof node !== "object") return;
  yield node;
  for (const child of node.children ?? []) yield* walk(child);
}

/** 收集正文段落（块级保护区整体跳过；标题不是 Paragraph，自然排除）。 */
function* proseParagraphs(document) {
  for (const node of walk(document)) {
    if (BLOCK_PROTECTED.has(node.type)) continue;
    if (node.type === Syntax.Paragraph) yield node;
  }
}

/** 以 Str 子节点重建段落正文：等长字符数组按 Str 的绝对位置回填，其余位置
 *  （行内代码内容、链接 URL、强调标记、图片语法）一律留白——非 Str 字节
 *  永远进不了检测，坐标又与原文严格对齐（UTF-16 码元索引）。
 *  进入 INLINE_PROTECTED 子树的 Str 不算正文。 */
function paragraphText(paragraph) {
  const base = paragraph.range[0];
  const chars = " ".repeat(paragraph.range[1] - base).split("");
  const visit = (node, inProtected) => {
    const protect = inProtected || INLINE_PROTECTED.has(node.type);
    if (!protect && node.type === "Str" && typeof node.value === "string") {
      const rel = node.range[0] - base;
      for (let i = 0; i < node.value.length; i++) chars[rel + i] = node.value[i];
      return;
    }
    for (const child of node.children ?? []) visit(child, protect);
  };
  visit(paragraph, false);
  return chars.join("");
}

export default function rule(context, options = {}) {
  const { report, locator } = context;
  const scene = options?.scene ?? "normal";
  const minRepeat = scene === "novel" ? MIN_REPEAT_LEN : MIN_REPEAT_LEN + 6;

  return {
    [Syntax.Document](document) {
      // 重复句跨段落累计（同一句在第 N 段再出现也要报）；键为去噪后的句子。
      const seen = new Map();

      for (const para of proseParagraphs(document)) {
        const base = para.range[0];
        const text = paragraphText(para);

        // 1) 重复句（zh-repeated-sentence）：同一句全文出现 ≥2 次，报第 2+ 次。
        //    重复是候选不是结论——刻意重复（修辞、口头禅）由裁判与规则判据判断。
        //    range 只覆盖句子核心（trim 掉前导空白），保证回读以句子本身开头；
        //    参照锚点（首次出现）随 @ctx: 输出，供裁判回看重复来源。
        for (const sentence of splitSentences(text)) {
          const key = stripNoise(sentence.text);
          if (key.length < minRepeat) continue;
          if (seen.has(key)) {
            const first = seen.get(key);
            const padLeft = sentence.text.length - sentence.text.trimStart().length;
            const coreStart = base + sentence.start + padLeft;
            const coreLen = sentence.text.trim().length;
            report(document, {
              message: `[zh-repeated-sentence] 与前文重复的句子（此前已出现）；`
                + `重复是候选，删除是否安全需判断叙事功能`
                + ` @ctx:${first.start}-${first.start + first.len}`,
              padding: locator.range([coreStart, coreStart + coreLen]),
            });
            first.count += 1;
          } else {
            const padLeft = sentence.text.length - sentence.text.trimStart().length;
            seen.set(key, {
              start: base + sentence.start + padLeft,
              len: sentence.text.trim().length,
              count: 0,
            });
          }
        }

        // 2) 机械时间链（zh-mechanical-temporal-chain）：同一行连接词 ≥3 个才报
        //    第 2+ 个；逐帧播报感是候选，不是禁词判断（用户要求第 19 节）。
        //    阈值与判据文档一致：2 词不报（补正 Q05）。
        let lineStart = 0;
        for (const line of text.split("\n")) {
          const hits = [];
          for (const word of CONNECTIVES) {
            let idx = line.indexOf(word);
            while (idx !== -1) {
              hits.push({ idx, len: word.length });
              idx = line.indexOf(word, idx + word.length);
            }
          }
          hits.sort((a, b) => a.idx - b.idx);
          if (hits.length >= MIN_TEMPORAL_HITS) {
            for (let i = 1; i < hits.length; i++) {
              const hit = hits[i];
              report(document, {
                message: `[zh-mechanical-temporal-chain] 同段第 ${i + 1} 个时间`
                  + `连接词「${line.slice(hit.idx, hit.idx + hit.len)}」；`
                  + `整段是否呈逐帧播报需结合上下文判断`
                  + ` @ctx:${base + lineStart}-${base + lineStart + line.length}`,
                padding: locator.range([base + lineStart + hit.idx,
                                        base + lineStart + hit.idx + hit.len]),
              });
            }
          }
          lineStart += line.length + 1;  // +1 是被 split 吃掉的换行符
        }
      }
    },
  };
}
