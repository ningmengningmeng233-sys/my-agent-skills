#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { gateReport, computeStats } from '../novel-script/scripts/novel-script.mjs';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// 读取禁词与 AI 味正则库
const bannedWords = JSON.parse(fs.readFileSync(path.join(__dirname, 'banned_words.json'), 'utf-8'));
const aiPatterns = JSON.parse(fs.readFileSync(path.join(__dirname, 'ai_tone_patterns.json'), 'utf-8')).map(item => ({
  name: item.name,
  regex: new RegExp(item.pattern)
}));

export function enhancedGateReport(doc, ctx = {}) {
  // 先获取官方原生 10 道质量门
  const baseGates = gateReport(doc, ctx);
  
  const eps = Array.isArray(doc?.episodes) ? doc.episodes : [];
  const badActionWords = [];
  const badAIToneLines = [];
  const hits = [];

  for (const ep of eps) {
    const epNum = ep.ep;
    const epLabel = `第 ${epNum} 集`;
    for (const [scIdx, sc] of (ep.scenes ?? []).entries()) {
      const scId = sc.sceneId || (sc.scene ? sc.scene : `S${String(scIdx + 1).padStart(2, '0')}`);
      const scLabel = `场次 ${scId}`;
      for (const [bIdx, beat] of (sc.flow ?? []).entries()) {
        const beatPos = `拍 ${bIdx + 1}`;
        // 检查动作中的文学病形容词
        if (typeof beat.action === 'string') {
          for (const bw of bannedWords) {
            if (beat.action.includes(bw)) {
              badActionWords.push(`${epLabel} ${scLabel} ${beatPos} 包含文学病禁词「${bw}」`);
              hits.push({
                gate: 'literature-sickness',
                term: bw,
                ep: epNum,
                scene: scId,
                snippet: beat.action.slice(0, 15)
              });
            }
          }
        }
        // 检查台词中的翻案腔与辩论腔
        if (typeof beat.line === 'string') {
          for (const pat of aiPatterns) {
            if (pat.regex.test(beat.line)) {
              badAIToneLines.push(`${epLabel} ${scLabel}「${beat.line.slice(0, 15)}…」命中 ${pat.name}`);
              hits.push({
                gate: 'ai-tone-removal',
                pattern: pat.name,
                ep: epNum,
                scene: scId,
                snippet: beat.line.slice(0, 15)
              });
            }
          }
        }
      }
    }
  }

  ctx._hits = hits;

  // 追加 G11 和 G12
  baseGates.push({
    id: 'literature-sickness',
    label: '禁止文学病与抽象形容词（若有所思/神色复杂/睫毛微颤等）',
    ok: badActionWords.length === 0,
    detail: badActionWords.join('；')
  });

  baseGates.push({
    id: 'ai-tone-removal',
    label: '禁止 AI 辩论腔与翻案腔（不是……而是……/看似……实则……）',
    ok: badAIToneLines.length === 0,
    detail: badAIToneLines.join('；')
  });

  return baseGates;
}

/**
 * 跨集回声告警（X1，告警级不计门数）：上一集末场人物与下一集首场人物零交集，
 * 意味着上一集的 cliffhanger 在下一集开场无人接住。冷却转场合法，故只 ⚠ 不否决。
 * 单元剧模式 (mode === 'anthology') 下每集故事与人物完全独立，自动静默 X1 告警。
 */
export function crossEpisodeWarnings(doc, ctx = {}) {
  if (ctx?.outline?.mode === 'anthology' || doc?.mode === 'anthology') {
    return [];
  }
  const eps = Array.isArray(doc?.episodes) ? doc.episodes : [];
  const warns = [];
  for (let i = 0; i + 1 < eps.length; i++) {
    const prevScenes = eps[i]?.scenes ?? [];
    const nextScenes = eps[i + 1]?.scenes ?? [];
    const prevLast = prevScenes[prevScenes.length - 1];
    const nextFirst = nextScenes[0];
    if (!prevLast || !nextFirst) continue;
    const prevCast = new Set((prevLast.characters ?? []).filter((c) => c && c !== 'VO'));
    const nextCast = new Set((nextFirst.characters ?? []).filter((c) => c && c !== 'VO'));
    if (!prevCast.size || !nextCast.size) continue;
    const echoed = [...nextCast].some((c) => prevCast.has(c));
    if (!echoed) {
      warns.push(`第${eps[i]?.ep}集末场人物 {${[...prevCast].join(',')}} 与第${eps[i + 1]?.ep}集首场人物 {${[...nextCast].join(',')}} 零交集——上一集悬念开场无人接住；若是刻意换线转场请忽略`);
    }
  }
  return warns;
}

// CLI 执行模式
if (process.argv[1] === __filename) {
  const args = process.argv.slice(2);
  const scriptPath = args[0];
  if (!scriptPath || scriptPath === '--help' || scriptPath === '-h') {
    console.log(`用法: node validate.mjs <script.json> [--outline <outline.json>] [--art <art.json>] [--json <report.json>]`);
    process.exit(0);
  }

  const outlineIdx = args.indexOf('--outline');
  let outlinePath = null;
  if (outlineIdx !== -1 && args[outlineIdx + 1]) {
    outlinePath = args[outlineIdx + 1];
  }
  const artIdx = args.indexOf('--art');
  let artPath = null;
  if (artIdx !== -1 && args[artIdx + 1]) {
    artPath = args[artIdx + 1];
  }
  const jsonIdx = args.indexOf('--json');
  let jsonPath = null;
  if (jsonIdx !== -1 && args[jsonIdx + 1]) {
    jsonPath = args[jsonIdx + 1];
  }

  const doc = JSON.parse(fs.readFileSync(scriptPath, 'utf-8'));
  const ctx = {};
  if (outlinePath && fs.existsSync(outlinePath)) {
    ctx.outline = JSON.parse(fs.readFileSync(outlinePath, 'utf-8'));
  }
  if (artPath) {
    if (!fs.existsSync(artPath)) {
      console.error(`❌ 美术设定文件不存在: ${artPath}`);
      process.exit(1);
    }
    ctx.art = JSON.parse(fs.readFileSync(artPath, 'utf-8'));
  }

  const gates = enhancedGateReport(doc, ctx);
  let failedCount = 0;
  console.log('\n================== 12 道确定性质量门检测报告 ==================');
  for (const g of gates) {
    if (g.ok) {
      console.log(`✓ [通过] ${g.label}`);
      // 通过的门也裸露详情（如逐集时长对照），验收报告必须可被肉眼复核
      if (g.detail) {
        console.log(`    ↳ 详情: ${g.detail}`);
      }
    } else {
      failedCount++;
      console.log(`✗ [违规] ${g.label}`);
      if (g.detail) {
        console.log(`    ↳ 详情: ${g.detail}`);
      }
    }
  }
  const warnings = crossEpisodeWarnings(doc, ctx);
  if (warnings.length) {
    console.log('\n【跨集连续性告警（不计入 12 门，不否决交付）】');
    for (const w of warnings) {
      console.log(`⚠ ${w}`);
    }
  }
  console.log('================================================================');

  if (jsonPath) {
    const payload = {
      gates: gates.map((g) => ({
        id: g.id,
        label: g.label,
        ok: g.ok,
        detail: g.detail || ''
      })),
      hits: ctx._hits || [],
      warnings: warnings
    };
    try {
      fs.writeFileSync(jsonPath, JSON.stringify(payload, null, 2), 'utf-8');
    } catch (err) {
      console.error(`⚠ 写入结构化报告失败: ${err.message}`);
    }
  }

  if (failedCount === 0) {
    console.log('\n🎉 全部 12 道质量门 100% 验收通过！剧本具备成片级品质。\n');
    process.exit(0);
  } else {
    console.error(`\n❌ 共有 ${failedCount} 项质量门未通过，请按上述定位精确修正。\n`);
    process.exit(1);
  }
}
