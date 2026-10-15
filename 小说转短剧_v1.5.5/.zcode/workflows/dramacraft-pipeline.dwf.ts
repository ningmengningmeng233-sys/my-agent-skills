/* zcode-workflow
description: 小说转短剧整条产线，写手出剧本，导演规划，镜头组，逐集监督，读者会与评审会，终验导出。确定性执法全在脚本门族与 validate 上，工人只做创作与检修。
whenToUse: 用户要把一本小说或已有大纲一次跑成可拍的成片件时用；只改单集、人工盯稿或断点续跑请用命令行 run_batch。
args:
  novelPath:
    type: string
    required: true
  outlinePath:
    type: string
    required: true
  workdir:
    type: string
    required: true
  targetSeconds:
    type: number
    default: 90
  episodesRange:
    type: json
  enableReaderMeeting:
    type: boolean
    default: true
  enableReviewMeeting:
    type: boolean
    default: true
  enableSupervision:
    type: boolean
    default: true
  artPath:
    type: string
  engineRoot:
    type: string
  healRoundCap:
    type: number
    default: 3
*/

// —— Step1 预演取证（2026-09-22 实跑）——
// 63 集切批 → 40+23；真跑 validate 得 12 门 / 0 未过；exit0 但报告缺失 → 硬失败；
// snapshot/verify 往返 0/0；盘库 glob 命中。
// 实测教训：**报告目录不存在时 validate.mjs 照样 exit 0 且不落报告**，
// 所以开工第一步必须先建 state/reports 等目录，否则每道门都会被「缺报告」硬失败拦下。
// 2026-09-22 装包实验逼出的三处收口（都在本文件内）：① 引擎定位探测 + 原著 GBK/GB18030 转码；
// ② 读者会前移到大纲预检之后、写作之前，接受的关切直接进写手提示词；③ 会议纪要必须落盘（五字段）、
// 监督报告必须落 state/supervision/，且所有门/链/会议写入一律核退出码——静默失败当成功是这一版最贵的坑。

const WORKDIR = String(args.workdir ?? "");
const OUTLINE = String(args.outlinePath ?? "");
const NOVEL = String(args.novelPath ?? "");
const TARGET = Number(args.targetSeconds ?? 90);
const ART = args.artPath ? String(args.artPath) : "";        // 给了就实扫门 10（场景/道具对账）
const ENGINE_HINT = args.engineRoot ? String(args.engineRoot) : "";   // 引擎根显式指定（缺省自动探测）
const CAP = Math.max(1, Number(args.healRoundCap ?? 3));
const READER_ON = args.enableReaderMeeting !== false;
const REVIEW_ON = args.enableReviewMeeting !== false;
const SUPER_ON = args.enableSupervision !== false;
const BATCH = 40;
for (const [k, v] of [["novelPath", NOVEL], ["outlinePath", OUTLINE], ["workdir", WORKDIR]] as [string, string][]) {
  if (!v) throw new Error(`缺少启动参数 ${k}：本工作流必须带 novelPath / outlinePath / workdir 起（Amend 不继承参数，重开请重新传齐）`);
}
const REPORTS = `${WORKDIR}/state/reports`;
const EP_DIR = `${WORKDIR}/state/episodes`;
const PLANS = `${WORKDIR}/state/plans`;
const SHOTS = `${WORKDIR}/state/shotgroups`;
const SUPERVISION = `${WORKDIR}/state/supervision`;

// —— 引擎定位（本工作流要在任意小说工程里跑；引擎装在技能目录，不在小说工程里）——
// 候选顺序：显式 engineRoot → 当前目录能 import 到的 dramacraft 包（开发仓库/正式安装优先）
// → 用户级技能目录（小白在小说工程里跑时的常态）→ 当前目录。
// 命中条件：根下同时有 run_batch.py 与 dramacraft/ingestor.py。
let ENGINE = "";
const ENGINE_PROBE = [
  "import importlib.util,os,sys",
  "ok=lambda c: bool(c) and os.path.isfile(os.path.join(c,'run_batch.py')) and os.path.isfile(os.path.join(c,'dramacraft','ingestor.py'))",
  "spec=importlib.util.find_spec('dramacraft')",
  "pkg=os.path.dirname(os.path.dirname(os.path.abspath(spec.origin))) if spec and spec.origin else ''",
  "hint=sys.argv[1] if len(sys.argv)>1 else ''",
  "cands=[hint,pkg,os.path.expanduser('~/.agents/skills/dramacraft'),os.path.expanduser('~/.claude/skills/dramacraft'),os.getcwd()]",
  "print(next((os.path.abspath(c) for c in cands if ok(c)), ''))",
].join("; ");
/** 引擎模块调用器：把引擎根塞进 sys.path 再按 `__main__` 跑模块（不依赖当前目录）。 */
const PY_SHIM = "import runpy,sys;sys.path.insert(0,sys.argv[1]);m=sys.argv[2];sys.argv=[m]+sys.argv[3:];runpy.run_module(m,run_name='__main__')";
function mod(modName: string, rest: string[]): string[] {
  return ["-c", PY_SHIM, ENGINE, modName, ...rest];
}

interface Doc {
  /** 硬伤条目（validate 的失败门，或 lint 的 hard 数组） */
  hard: string[];
  /** 提醒条目（不进回炉） */
  warn: string[];
}
interface Verdict {
  /** pass | continue | escalate | abort */
  verdict: string;
  /** 还没解决的阻塞项（人读） */
  unresolved: string[];
}
interface HumanDecision {
  /** 用户选的选项原文 */
  choice: string;
  /** 用户补的一句话（可为空） */
  note: string;
}
interface SupervisorReport {
  /** 是否放行 */
  pass: boolean;
  /** 红线级问题（pass=false 时非空） */
  severe: string[];
}
interface ContentReport {
  /** 内容层缺口：原著这一段有、剧本丢了或被磨平的关系细节（空数组＝接住了） */
  missing: string[];
}
interface Finding {
  /** 位置：如 "ep3 plan Sc2" */
  where: string;
  /** 一句话：问题是什么 */
  what: string;
  /** 什么证明了它 */
  evidence: string;
  /** verified = 独立复核或确定性检查确认；unconfirmed = 没确认成 */
  status: "verified" | "unconfirmed";
  /** 严重度 */
  severity: "low" | "medium" | "high";
}

function pad(ep: number): string {
  return String(ep).padStart(3, "0");
}
function board(ep: number | string): string {
  return typeof ep === "number" ? String(ep).padStart(2, "0") : ep;
}

/** 机读双判定：报告读不到/不可解析时，exit0 一律当硬失败（没有报告 ≠ 通过）。 */
async function readReport(path: string, exitCode: number): Promise<Doc> {
  let raw: string;
  try {
    raw = await files.read(path);
  } catch {
    if (exitCode === 0) throw new Error(`exit0 但机读报告缺失：${path}（没有报告 ≠ 通过）`);
    return { hard: ["只有退出码，报告没落盘"], warn: [] };
  }
  let doc: { gates?: { label?: string; ok?: boolean; detail?: string }[]; hard?: string[]; warn?: string[] };
  try {
    doc = JSON.parse(raw) as typeof doc;
  } catch {
    if (exitCode === 0) throw new Error(`exit0 但机读报告不可解析：${path}`);
    return { hard: ["只有退出码，报告不可解析"], warn: [] };
  }
  const gates = doc.gates ?? [];
  return {
    hard: [...gates.filter((g) => !g.ok).map((g) => `${g.label ?? ""}${g.detail ? "：" + g.detail : ""}`), ...(doc.hard ?? [])],
    warn: doc.warn ?? [],
  };
}

async function validateEp(ep: number, round: number): Promise<{ doc: Doc; report: string }> {
  // 单集件是"裸"形状（run_batch 汇编与断点安检要这个形状），而 validate.mjs 只认 doc.episodes——
  // 所以走引擎自己的单集验证入口（self_healing 的包壳口径），不在 TS 里复刻包壳。
  const report = `${REPORTS}/val-${ep}-${round}.json`;
  const r = await world.run("python", mod("dramacraft.self_healing", ["--ep",
    `${EP_DIR}/ep${pad(ep)}.json`, "--outline", OUTLINE, ...(ART ? ["--art", ART] : []), "--json", report]),
    { timeoutMs: 600000 });
  return { doc: await readReport(report, r.exitCode), report };
}

async function lintPlan(ep: number): Promise<{ doc: Doc; report: string }> {
  const report = `${REPORTS}/plan-${ep}.json`;
  const r = await world.run("python", mod("dramacraft.plan_lint", [`${PLANS}/ep${pad(ep)}.json`,
    "--script", `${EP_DIR}/ep${pad(ep)}.json`, "--json", report]), { timeoutMs: 300000 });
  return { doc: await readReport(report, r.exitCode), report };
}

async function lintShots(ep: number): Promise<{ doc: Doc; report: string }> {
  const report = `${REPORTS}/shots-${ep}.json`;
  const r = await world.run("python", mod("dramacraft.shot_lint", [`${SHOTS}/ep${pad(ep)}.json`,
    "--plan", `${PLANS}/ep${pad(ep)}.json`, "--script", `${EP_DIR}/ep${pad(ep)}.json`,
    "--target-seconds", String(TARGET), "--workdir", WORKDIR, "--json", report]), { timeoutMs: 300000 });
  return { doc: await readReport(report, r.exitCode), report };
}

/** 熔断算术只在 python 一处（工作流只消费裁决）。 */
async function breaker(ep: number, round: number, gateReport: string): Promise<Verdict> {
  const report = `${REPORTS}/brk-${ep}-${round}.json`;
  await world.run("python", mod("dramacraft.breaker", ["--workdir", WORKDIR, "--ep", String(ep),
    "--round", String(round), "--cap", String(CAP), "--gate-report", gateReport, "--json", report]), { timeoutMs: 300000 });
  let raw: string;
  try {
    raw = await files.read(report);
  } catch {
    // 门报告本身缺失/不可解析时，breaker 按纪律 exit 1 且不落报告——这里不许去读不存在的文件，
    // 直接按"本轮没过门"继续（脚本自己的轮次计数仍受 CAP 约束），把缺失原因带回给工人。
    return { verdict: "continue", unresolved: [`门报告缺失或不可读：${gateReport}（按未过门处理，先修门这一步的输入）`] };
  }
  const doc = JSON.parse(raw) as { verdict?: string; unresolved?: string[] };
  return { verdict: String(doc.verdict ?? "abort"), unresolved: doc.unresolved ?? [] };
}

async function emitChain(ep: number, stage: string): Promise<void> {
  const r = await world.run("python", mod("dramacraft.artifact_chain", ["emit", "--workdir", WORKDIR,
    "--ep", String(ep), "--stage", stage, "--json", `${REPORTS}/chain-${stage}-${ep}.json`]), { timeoutMs: 120000 });
  if (r.exitCode !== 0) {
    throw new Error(`改稿失效链盖不了章（${stage} ep${ep}，exit ${r.exitCode}）：`
      + (r.stdout + r.stderr).slice(-400) + "\n链路断了下游就会拿旧指纹比对，宁停不糊。");
  }
}

/** 监督双件落盘 + 机读回读：放行必须「人读报告非空 + 机读件可解析且 verdict=pass + 已盖链章」三样齐。
 *  监督员回一句话（pass/severe）不算放行——报告不落盘、件被改过、链章缺一，本集都不许放行。 */
async function superviseRecord(ep: number, passed: boolean, findings: string[]): Promise<{ ok: boolean; why: string }> {
  const mdPath = `${SUPERVISION}/ep${pad(ep)}.md`;
  let mdOk = false;
  try {
    mdOk = (await files.read(mdPath)).trim().length > 0;
  } catch {
    mdOk = false;
  }
  const release = passed && mdOk;
  const items = findings.map((f) => String(f).trim()).filter((f) => f.length > 0);
  if (passed && !mdOk) {
    items.push("回话是放行，但监督报告（人读件）没落盘或为空——本轮不算放行");
  }
  const cmd = ["supervise", "--workdir", WORKDIR, "--ep", String(ep), "--verdict", release ? "pass" : "fail"];
  for (const item of items) {
    cmd.push("--finding", item);
  }
  cmd.push("--json", `${REPORTS}/supervision-${ep}.json`);
  const r = await world.run("python", mod("dramacraft.artifact_chain", cmd), { timeoutMs: 120000 });
  if (r.exitCode !== 0) {
    return { ok: false, why: `监督记录写不成（exit ${r.exitCode}）：` + (r.stdout + r.stderr).slice(-300) };
  }
  const recPath = `${SUPERVISION}/ep${pad(ep)}.json`;
  let rec: { verdict?: string; _meta?: { upstreamHashes?: unknown } };
  try {
    rec = JSON.parse(await files.read(recPath)) as { verdict?: string; _meta?: { upstreamHashes?: unknown } };
  } catch {
    return { ok: false, why: `监督机读件缺失或不可解析：${recPath}` };
  }
  const want = release ? "pass" : "fail";
  if (String(rec.verdict ?? "") !== want) {
    return { ok: false, why: `监督机读件 verdict=${String(rec.verdict ?? "（缺）")}，与本次结论「${want}」不一致` };
  }
  if (release && !(rec._meta && rec._meta.upstreamHashes)) {
    return { ok: false, why: `监督放行件没盖链章（缺 _meta.upstreamHashes）：${recPath}` };
  }
  return { ok: release, why: release ? "" : String(items[0] ?? "本轮未放行") };
}

/** 单集件补 `_meta`：工作流模式剧本由写手写在外面，而装配器的缓存安检（有 _meta / schemaVersion≥2 /
 *  outlineHash 对上大纲）是 `--assemble-only` 的硬前提——缺了它，12 门全过的件也会被当成旧版本缓存拒收。
 *  只在 12 门过了之后补：补早了等于给没过门的件发通行证。 */
async function stampMeta(ep: number, writtenBy?: string): Promise<void> {
  const argv = [`${ENGINE}/run_batch.py`, "--stamp-only", `${EP_DIR}/ep${pad(ep)}.json`,
    "--outline", OUTLINE];
  // 写手刚写完的这一集：把「谁写进来的」一并登记（生产记录的来源栏据此写「外部入库」，
  // 不靠猜执笔模型）。在库沿用/人工补章的件不登记写手，来源就如实写「来源未记录」。
  if (writtenBy) argv.push("--stamped-by", writtenBy);
  const r = await world.run("python", argv, { timeoutMs: 120000 });
  if (r.exitCode !== 0) {
    throw new Error(`第 ${ep} 集补 _meta 没成（exit ${r.exitCode}）：` + (r.stdout + r.stderr).slice(-400)
      + "\n没有 _meta 的件过得了 12 门也进不了装配（缓存安检会拒收，整批交付戳跟着降级）。");
  }
}

/** 在库单集件探测：**不用 `files.glob`**。
 *  实测（2026-09-23 T6 复跑）：已过门的 `ep001.json` 明明在盘上，`files.glob` 却**静默返回空**
 *  （同一时刻 `files.read` 读机读报告正常），于是「在库件沿用」分支永不命中，
 *  复跑把整集重写了一遍——既重复烧额度，又把规划/镜头/监督一起拖着重做。
 *  改成让 python 直接问文件系统：存在就报，不存在就不报，没有中间状态。 */
async function existingEps(eps: number[]): Promise<Set<string>> {
  const r = await world.run("python", ["-c",
    "import os,sys;d=sys.argv[1];print('|'.join('%03d'%i for i in map(int,sys.argv[2:]) if os.path.exists(os.path.join(d,'ep%03d.json'%i))))",
    EP_DIR, ...eps.map(String)], { timeoutMs: 120000 });
  const out = r.stdout.trim();
  return new Set(out ? out.split("|") : []);
}

/** 在库阶段件「上游未变」判定（引擎口径：artifact_chain 的三态报告）。
 *  复跑靠它决定沿用还是重做：件在库 **且** `status=fresh`（记录的上游指纹与现在的上游一致）才算能沿用；
 *  查不出来、件不在库、上游变过，一律按重做处理（保守方向 = 重跑，不是复用）。
 *  没有这一层，复跑会把规划/镜头/监督三段全量重做——省的是真额度。 */
async function chainFresh(ep: number, stage: string): Promise<boolean> {
  const report = `${REPORTS}/chain-${stage}-${ep}.json`;
  const r = await world.run("python", mod("dramacraft.artifact_chain",
    ["check", "--workdir", WORKDIR, "--ep", String(ep), "--stage", stage, "--json", report]),
    { timeoutMs: 120000 });
  if (r.exitCode !== 0) return false;
  try {
    const doc = JSON.parse(await files.read(report)) as { status?: string; present?: string[]; stale?: string[] };
    return (doc.present ?? []).indexOf(stage) >= 0 && (doc.stale ?? []).length === 0;
  } catch {
    return false;
  }
}

/** 单集件拍/场形状闸（真跑 2026-09-23 T6 抓出的门洞）：写手件缺 `location`/`type` 时，
 *  12 门按 `line` 认对白照样放行，可下游导演规划按契约读对白，两个相位之后才炸成 22 条硬伤。
 *  口径在引擎一处（`run_batch.py --check-ep`），本脚本只传参；硬伤**并进同一份 12 门报告**，
 *  熔断器才看得见，写手下一轮才拿得到「缺哪个字段」的整改清单。 */
async function checkEpShape(ep: number, mergeInto: string): Promise<Doc> {
  const report = `${REPORTS}/shape-${ep}.json`;
  const r = await world.run("python", [`${ENGINE}/run_batch.py`, "--check-ep", `${EP_DIR}/ep${pad(ep)}.json`,
    "--merge-into", mergeInto, "--json", report], { timeoutMs: 120000 });
  const doc = await readReport(report, r.exitCode);
  if (doc.hard.length > 0) {
    log(`第 ${ep} 集剧本件不合拍/场契约：${doc.hard.length} 条（已并进 12 门报告，交回写手补齐字段）`);
  }
  return doc;
}

/** 问人：走协调员的升级通道（升级提问不超时，等人拍板期间只有它在等）。 */
async function askHuman(coordinator: Agent, question: string, options: string[], context: string): Promise<HumanDecision> {
  return await coordinator.ask<HumanDecision>(
    `请把下面这个问题连同选项**原样升级给用户**（用你的升级提问通道），拿到答复后按用户答复回话。\n` +
    `问题：${question}\n选项：${options.map((o, i) => `${i + 1}) ${o}`).join(" / ")}\n背景：${context}\n` +
    `回话字段：choice = 用户选的选项原文；note = 用户补充的一句话（没有就空串）。`,
  );
}

/** 会议简报（fbs 会议机制卡：开会前必做，参会方只读简报不读全项目文件）。 */
async function writeBrief(kind: string, goal: string, background: string): Promise<string> {
  const out = `${WORKDIR}/state/meetings/brief-${kind}.md`;
  const r = await world.run("python", ["-c",
    "import sys; sys.path.insert(0, sys.argv[1]); from dramacraft.meeting_pack import build_brief, write_brief; "
    + "print(write_brief(sys.argv[2], sys.argv[3], build_brief(sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6])))",
    ENGINE, WORKDIR, kind, title, goal, background], { timeoutMs: 120000 });
  if (r.exitCode !== 0) {
    throw new Error(`会议简报没写成（${kind}，exit ${r.exitCode}）：${(r.stdout + r.stderr).slice(-400)}`
      + "\n没有简报就开会，参会方会去读全项目文件——那是这一版明令禁止的。");
  }
  return out;
}

/** 越权硬闸：派活前对白名单件留底（工人改动能被下一步抓出来）。 */
async function snapshotUpstream(manifest: string, paths: string[]): Promise<void> {
  const r = await world.run("python", mod("dramacraft.artifact_chain", ["snapshot", "--json", manifest, ...paths]),
    { timeoutMs: 120000 });
  if (r.exitCode !== 0) {
    throw new Error(`越权快照没留成（${manifest}，exit ${r.exitCode}）：${(r.stdout + r.stderr).slice(-400)}`
      + "\n没有快照这一步，工人动了白名单外的件也查不出来。");
  }
}

/** 越权硬闸：派活后核对——有件被动过就返回 false（该轮按未过门处理）。 */
async function upstreamIntact(manifest: string): Promise<boolean> {
  const rep = `${manifest}.verify.json`;
  const r = await world.run("python", mod("dramacraft.artifact_chain", ["verify", "--manifest", manifest,
    "--json", rep]), { timeoutMs: 120000 });
  return r.exitCode === 0;
}

/** 会议机读件：简报、并条、决议、分级、纪要、闭环全在 python 一处（本脚本只传参，会议口径零复刻）。
 *  一律核退出码——会议机读件写失败还当成功，等于会议白开（纪要/并条/决议全丢）。 */
async function meetingCli(rest: string[], what: string): Promise<string> {
  const r = await world.run("python", mod("dramacraft.meeting_pack", rest), { timeoutMs: 180000 });
  if (r.exitCode !== 0) {
    throw new Error(`${what}没成（exit ${r.exitCode}）：${(r.stdout + r.stderr).slice(-500)}`
      + "\n会议口径的落盘失败一律当场停，不许当成功往下走。");
  }
  return r.stdout.trim();
}

/** 闭环核账：0 = 关切全决议、4 = 有未决议、其余 = 读不出来。
 *  「读不出来」是核账失败——绝不归入闭环通过，也绝不冒充「没有关切」。 */
async function closureCheck(report: string): Promise<{ exit: number; openIds: string[]; read: boolean; detail: string }> {
  const r = await world.run("python", mod("dramacraft.meeting_pack", ["check-closure",
    "--workdir", WORKDIR, "--json", report]), { timeoutMs: 120000 });
  if (r.exitCode !== 0 && r.exitCode !== 4) {
    return { exit: r.exitCode, openIds: [], read: false,
      detail: `闭环核账 exit ${r.exitCode}：` + (r.stdout + r.stderr).slice(-300) };
  }
  try {
    const doc = JSON.parse(await files.read(report)) as { open?: { id?: string }[] };
    return { exit: r.exitCode, openIds: (doc.open ?? []).map((x) => String(x.id ?? "")), read: true, detail: "" };
  } catch {
    return { exit: r.exitCode, openIds: [], read: false, detail: `闭环报告缺失或不可解析：${report}` };
  }
}

// —— 开工：建目录、读大纲、盘点在库 ——

phase("先看清工作区里已经有什么");
await world.run("python", ["-c", "import os,sys; [os.makedirs(p, exist_ok=True) for p in sys.argv[1:]]",
  REPORTS, PLANS, SHOTS, SUPERVISION, EP_DIR, `${WORKDIR}/state/meetings`], { timeoutMs: 120000 });

// 引擎根：本工作流可以在任意小说工程里跑，引擎不在这个工程里也能找到（见文件头 ENGINE_PROBE）。
const probe = await world.run("python", ["-c", ENGINE_PROBE, ENGINE_HINT], { timeoutMs: 120000 });
ENGINE = probe.stdout.trim();
if (!ENGINE) {
  throw new Error(
    "找不到引擎目录：请把技能包整个放到 ~/.agents/skills/dramacraft（或 ~/.claude/skills/dramacraft），"
    + "或者启动时传 engineRoot 指向技能包目录（该目录下应有 run_batch.py 与 dramacraft/）。");
}
log(`引擎目录：${ENGINE}`);

// 原著转通用编码：真书常见 GBK/GB18030，写手读它容易乱码——统一转成 UTF-8 的整本参考件。
const SRC_REF = `${WORKDIR}/state/source/novel-utf8.md`;
const ingest = await world.run("python", ["-c", [
  "import os,sys",
  "sys.path.insert(0,sys.argv[1])",
  "from dramacraft.ingestor import load_novel",
  "chs=load_novel(sys.argv[2])",
  "os.makedirs(os.path.dirname(sys.argv[3]),exist_ok=True)",
  "fp=open(sys.argv[3],'w',encoding='utf-8')",
  "[fp.write('# %s\\n\\n%s\\n\\n' % (c.title,c.content)) for c in chs]",
  "fp.close()",
  "print(len(chs))",
].join("; "), ENGINE, NOVEL, SRC_REF], { timeoutMs: 600000 });
const novelRef = ingest.exitCode === 0 && ingest.stdout.trim() ? SRC_REF : NOVEL;
log(ingest.exitCode === 0 && ingest.stdout.trim()
  ? `原著已转成 UTF-8 参考件（${ingest.stdout.trim()} 章）：${SRC_REF}`
  : `原著转码跳过（exit ${ingest.exitCode}），写手直接读原文件：${NOVEL}`);

const outline = JSON.parse(await files.read(OUTLINE)) as { title?: string; episodes?: { ep?: number }[] };
const title = String(outline.title ?? "未命名短剧");
const allEps = (outline.episodes ?? []).map((e) => Number(e.ep ?? 0)).filter((n) => n > 0);
const range = Array.isArray(args.episodesRange) ? (args.episodesRange as unknown[]) : null;
const planned = range === null
  ? allEps
  : range.length === 1
    ? allEps.filter((n) => n === Number(range[0]))        // 单值写法 [3] 就是「只跑第 3 集」，不许静默变成空批
    : allEps.filter((n) => n >= Number(range[0] ?? 0) && n <= Number(range[1] ?? 0));
// 大纲里明明有集，筛完却是空的 = 参数写错（旧版会跑完 2 秒、报「0 集未全部走完」，看着像跑过）→ 当场停
if (planned.length === 0 && allEps.length > 0) {
  throw new Error(`集数筛选把大纲里的 ${allEps.length} 集全筛没了：episodesRange=${JSON.stringify(range)}，`
    + `大纲集号=${JSON.stringify(allEps)}。范围要写成 [起, 止]，只跑一集写 [n] 或 [n, n]。`);
}
const chunks: number[][] = [];
for (let i = 0; i < planned.length; i += BATCH) chunks.push(planned.slice(i, i + BATCH));
const batch = chunks[0] ?? [];
log(`《${title}》待跑 ${planned.length} 集，切 ${chunks.length} 批（单批上限 ${BATCH}），本次先跑 ${batch.length} 集`);

artifact.board("episodes", {
  title: "逐集进度", key: "ep", status: "state", cardTitle: "ep",
  columns: ["写作", "规划", "镜头", "质检", "交付", "阻塞", "待人判"],
});
artifact.metrics("numbers", { title: "本批关键数", metrics: [{ field: "planned", label: "待跑集数" }, { field: "delivered", label: "已交付" }] });
report({ planned: planned.length, delivered: 0, novel: NOVEL }, "numbers");

const existing = await existingEps(batch);   // 问文件系统，不用 glob（见 existingEps 注释）
for (const ep of batch) report({ ep: board(ep), state: existing.has(pad(ep)) ? "交付" : "写作", note: "开工盘点" }, "episodes");

const coordinator = agent("现场协调员",
  "你是产线现场协调员：只做一件事——把脚本递过来的问题连同选项原样升级给用户，拿到答复后按答复回话。"
  + "不要替用户做决定，也不要自己改剧本或改任何文件。若某件事你办不到，升级时直说卡在哪。");

// —— 大纲预检与读者会：先看根骨，再动笔（读者会挂点在写作之前，关切直接交给写手）——

phase("大纲预检与读者会：先看根骨，再动笔");
const ogate = await world.run("python", mod("dramacraft.outline_lint", [OUTLINE]), { timeoutMs: 120000 });
if (ogate.exitCode !== 0) {
  throw new Error("大纲预检未过（O1-O6）：\n" + (ogate.stdout + "\n" + ogate.stderr).slice(-900)
    + "\n先把大纲修到过门再来跑产线（根骨不合格，下游全白干）。");
}
log("大纲预检 O1-O6 全过");

let readerGuidance = "";
if (READER_ON) {
  const brief = await writeBrief("reader", `这份大纲撑不撑得起这 ${batch.length} 集`,
    "现在只有大纲与人物表，剧本还没写；只判结构、人物、追看动机，不提改台词的细活。");
  const readers = [1, 2, 3].map((i) => agent(`观众-${i}`,
    "你是目标观众：只说你真实看得懂/看不懂、想不想追下去，指出具体位置；不客套、不夸、不写总结。"));
  await Promise.all(readers.map(async (r, i) => {
    const raw = await r.ask(
      `先读会议简报 ${brief}（只读它，不读全项目文件），再读大纲 ${OUTLINE} 与原著前几章（${novelRef}）。` +
      `把 5 条关切写成 JSON 数组落到 ${REPORTS}/reader-${i + 1}.json，` +
      `每条形如 {"t":"一句话关切","where":"位置","severity":"🔴 严重|🟡 中等|⚪ 轻微"}；` +
      `只谈大纲里真有的东西，别编剧本里还没写的细节。`,
    );
    void raw;
  }));
  const mergedPath = `${REPORTS}/readers-merged.json`;
  await meetingCli(["sync-concerns", "--workdir", WORKDIR,
    "--inputs", [1, 2, 3].map((i) => `${REPORTS}/reader-${i}.json`).join("|"),
    "--json", mergedPath], "读者关切并条落盘");
  const concernDoc = JSON.parse(await files.read(mergedPath)) as { id: string; t: string; votes?: number }[];
  log(`读者会（写作前）：关切已合并去重，本轮 ${concernDoc.length} 条（简报在 ${brief}）`);
  report({ ep: "全批", state: "待人判", note: `写作前观众关切 ${concernDoc.length} 条，已逐条提请人判` }, "episodes");

  // 只问「还没有有效决议」的：同一工作区重跑时，上一轮已决议的原样沿用——不重问，也不占 6 条名额。
  const openNow = await closureCheck(`${REPORTS}/reader-open.json`);
  if (!openNow.read) {
    throw new Error(`读者关切清单读不出来，判断不了哪些已决议：${openNow.detail}`
      + "\n宁停不糊：重问一遍用户等于把额度和耐心一起烧掉，还可能把已决议的项翻回未决议。");
  }
  const asked = concernDoc.filter((c) => openNow.openIds.indexOf(String(c.id)) >= 0).slice(0, 6);
  log(`读者会（写作前）：本次待问 ${asked.length} 条（未决议共 ${openNow.openIds.length} 条，已决议的不再问）`);

  for (const c of asked) {
    const decision = await askHuman(coordinator,
      `观众关切（${c.votes ?? 1} 票）：${c.t}。接受这条意见吗？`,
      ["接受，下一版改", "部分接受（我补一句只改哪部分）", "驳回（我补一句为什么）"],
      "读者会关切清单：接受＝下一轮改；部分接受＝按你补的范围改；驳回＝写进纪要说明理由。"
      + "这份清单是在写作前拿到的，接受的部分会直接交给写手。");
    const verdict = decision.choice.indexOf("驳回") >= 0 ? "驳回"
      : decision.choice.indexOf("部分接受") >= 0 ? "部分接受" : "接受";
    // 决议按稳定 id 落（不按数组下标推算）：重跑时这条决议跟着同一条关切走。
    await meetingCli(["decide-concern", "--workdir", WORKDIR, "--id", String(c.id),
      "--decision", verdict, "--reason", decision.note || "用户当场决定",
      "--json", `${REPORTS}/decision-${c.id}.json`], `关切 ${c.id} 的决议`);
  }
  // 纪要按 id 配对，并核五字段真的落盘（缺字段即退出码非 0 → 当场停）。
  await meetingCli(["minutes-reader", "--workdir", WORKDIR, "--merged", mergedPath,
    "--json", `${REPORTS}/reader-minutes.json`], "读者会纪要");
  // 写作指导一律**按稳定 id 从决议清单重建**（含前几轮已接受的改法），不只看本轮问到的几条：
  // 复跑时旧决议不再重问，若只收本轮提问，写手就会把上一轮定下的改法丢掉。
  // 决议清单读坏 = 指导缺胳膊少腿 → 当场停，不许没指导硬写。
  const guidePath = `${REPORTS}/reader-guidance.json`;
  const guideRun = await world.run("python", mod("dramacraft.meeting_pack",
    ["reader-guidance", "--workdir", WORKDIR, "--merged", mergedPath, "--json", guidePath]),
    { timeoutMs: 120000 });
  if (guideRun.exitCode !== 0) {
    throw new Error(`写作指导没重建出来（exit ${guideRun.exitCode}）：`
      + (guideRun.stdout + guideRun.stderr).slice(-400)
      + "\n指导要按稳定 id 从决议清单重建。它读坏了就不写稿——没指导硬写会把已经接受的改法丢掉。");
  }
  const guideDoc = JSON.parse(await files.read(guidePath)) as { guidance?: string; lines?: number };
  const guideText = String(guideDoc.guidance ?? "");
  readerGuidance = guideText
    ? "\n读者会在此稿之前定下要改的（必须落到纸面）：\n" + guideText
    : "";
  log(`读者会（写作前）：本次新决议 ${asked.length} 条，交给写手的指导共 ${guideDoc.lines ?? 0} 条`
    + `（含前几轮已接受的改法，驳回项不进指导）`);
} else {
  log("读者会按参数关闭（enableReaderMeeting=false）");
}

// —— 逐集出剧本：写作 → 12 门 → 统一熔断回炉 ——

// 内容层复核：12 门只管结构、时长与字段，管不了「读起来像不像原著的缩写、人情味有没有蒸发」。
// 复核员只列缺口、不改稿、不替用户拍板；缺口带回写手下一轮改，到硬顶仍缺就如实登记进交付报告，不伪造通过。
const contentGaps = new Map<number, string[]>();
async function contentReview(ep: number, round: number): Promise<ContentReport> {
  const reviewer = agent(`内容复核-EP${ep}-r${round}`,
    "你是内容复核员：只核对、只列条目，不改任何文件、不做修改决策、不替用户拍板。"
    + "所有依据必须来自实际读取的原著与剧本，不得凭记忆或摘要。");
  const out = await reviewer.ask<ContentReport>(
    `第 ${ep} 集。剧本在 ${EP_DIR}/ep${pad(ep)}.json；原著参考 ${novelRef}。\n` +
    `第一步：用 files.grep 在原著里按剧本里的人名与物件名（人名优先）定位本集对应段落，取最长的那处命中，前后各多读一段。\n` +
    `第二步：把这一段里「人怎么对待人」的细节逐条抠出来——只有这五类算：\n` +
    `① 谁在等谁、谁替谁做了什么（那炷香是谁点的、父亲本来在哪儿等儿子）；\n` +
    `② 谁的话是谁教的（儿子引的那句是不是父亲从小教他的）；\n` +
    `③ 人物临走的那个小动作（叹气、挥手、回头、把东西放轻）；\n` +
    `④ 原著里最好的那句话有没有照原话说（被同义改写磨平了也算丢）；\n` +
    `⑤ 有没有一处不推进剧情、只为见人的细节。\n` +
    `第三步：逐条对到剧本上，写清落在第几拍；把整份复核结果（每条的原著原句与行号、剧本落点、判定）写到 ` +
    `${REPORTS}/content-ep${pad(ep)}-r${round}.json。\n` +
    `最后回话 missing：**只列「原著有、剧本丢了或被磨平」的条目**，每条写清原著原句与建议落到哪一拍；接住了的不要列。\n` +
    `另外单独判一条：整集读起来是不是原著的缩写摘要（一串事件被念完、没有戏）——是的话列进 missing 说清像在哪里。\n` +
    `原著这一段确实没有的关系细节不要硬凑；一条不丢就回空数组。`,
  );
  const missing = Array.isArray(out?.missing) ? out.missing.map(String) : [];
  log(`第 ${ep} 集内容复核（第 ${round} 轮）：${missing.length === 0 ? "关系细节无缺口" : `缺口 ${missing.length} 条`}`);
  return { missing };
}
/** 内容层缺口的交付口径：缺就写明缺几集几条，不许含糊过去 */
function gapLine(): string {
  if (contentGaps.size === 0) return "无缺口（原著关系细节逐条接住）";
  const parts: string[] = [];
  contentGaps.forEach((m, e) => parts.push(`ep${e} 缺 ${m.length} 条`));
  return parts.join("、") + `（明细见 ${REPORTS}/content-ep*.json）`;
}

phase("逐集出剧本：写作 → 12 门 → 回炉");
const writerResults = await Promise.all(batch.map(async (ep) => {
  const epFile = `${EP_DIR}/ep${pad(ep)}.json`;
  if (existing.has(pad(ep))) {
    const pre = await validateEp(ep, 0);          // 复用旧件前先验一遍：半成品不许当成品
    const preShape = await checkEpShape(ep, pre.report);   // 契约形状也要验：12 门对缺 location/type 是漏放的
    if (pre.doc.hard.length === 0 && preShape.hard.length === 0) {
      // 在库件是拿**现在的大纲**过完 12 门的（比断点安检的 outlineHash 比对更强），所以补章是如实记录
      await stampMeta(ep);
      report({ ep: board(ep), state: "交付", note: "在库件过门，本轮沿用" }, "episodes");
      return { ep, state: "交付", hard: [] as string[] };
    }
    report({ ep: board(ep), state: "写作", note: "在库件未过门，重做" }, "episodes");
  }
  const w = agent(`写手-EP${ep}`,
    "你是短剧编剧：按工作区里的上下文写第 N 集剧本，把合法单集 JSON 落到指定路径。"
    + "只忠实改编原著章节，不发明剧情；台词口语化、话留半句；动作写可拍摄的物理动作，不写抽象心理。"
    + "本剧配方是「一集一场戏」：一集只演一个转折——写的是戏，不是原著的缩写与摘录。"
    + "三条硬约束：① 每场必须有目标—阻碍—代价，要有具体的人在挡着主角（不是环境、不是心情）；"
    + "② 禁信息播报：史实、消息、地名、数字只能由动作或冲突带出来，不许用台词念清单；"
    + "③ 一集只写一个转折，不许把大纲里的一拍写成摘要式的一串事件。"
    + "第四条同样不许破：**原著这一场的时间锚、在场人物与情感触发点（某个日子、某件旧事、某个缺席的人）不许丢**——"
    + "这些是这场戏为什么此刻发生的理由；宁可少写一句台词，也不许把触发点压掉或挪到看不出。"
    + "第五条，最容易丢的一条：**人情味不许蒸发**——原著里凡是「人怎么对待人」的地方，都要落成能拍的动作、道具或台词："
    + "① 谁在等谁、谁替谁做了什么（父亲本在儿子书房等他、忌辰那炷香是谁点的）——这类细节一丢，戏就只剩吵架；"
    + "② 谁的话是谁教的（儿子拿父亲从小教他的道理顶回去，就得写明是「爹从小教导」的，不能只剩那句道理）；"
    + "③ 人物走时的那个小动作（叹气、挥手、回头、把东西放轻）不许省；"
    + "④ 原著里最好的那句话**照原话说**，不许用同义改写磨平（「本是同根生，相煎何太急」不许砍成「本是同根生」）；"
    + "⑤ 不许把原著里的悲悯改成狠话——人物怎么说话，就是人物是什么人；"
    + "⑥ 一集里至少留一处**不推进剧情、只为见人**的细节。"
    + "情绪不靠心理描写，靠具体：东西、动作、称呼、留半句。"
    + "②到⑤与「禁信息播报」不冲突：忌辰、谁点的香这些不是史实播报，是关系，用半句话或一个动作带出来。"
    + "某道门你无论如何过不去时，升级说明情况，不要伪造通过。");
  let lastHard: string[] = [];
  for (let round = 1; round <= CAP; round++) {
    await w.ask(
      `第 ${ep} 集，目标 ${TARGET} 秒。先读 ${OUTLINE} 与原著参考（${novelRef}）以及本集应回收的伏笔；` +
      `动笔前把本集对应的原著段落整段读完（前后各多读一段），先把这一段里「人怎么对待人」的地方列出来，再落笔。` +
      `把成稿写成单集 JSON 落到 ${epFile}（字段契约见技能的 ep###.json 契约：` +
      `每一场必须有 location，每一拍必须有 type（action 或 dialogue），对白拍带 speaker+line）。` + readerGuidance +
      (round === 1 ? "" : `\n上一轮没过门，逐条修掉这些再交：\n${lastHard.join("\n")}`),
    );
    const { doc, report: valReport } = await validateEp(ep, round);
    const shapeDoc = await checkEpShape(ep, valReport);   // 形状硬伤并进同一份报告，熔断器才看得见
    const docAll: Doc = { hard: [...doc.hard, ...shapeDoc.hard], warn: [...doc.warn, ...shapeDoc.warn] };
    const v = await breaker(ep, round, valReport);
    if (v.verdict === "pass") {
      // 门过了不等于戏对了：内容层再复核一遍。缺口带回写手下一轮；到硬顶仍缺则登记后交付，不伪造通过。
      const review = await contentReview(ep, round);
      if (review.missing.length === 0) {
        contentGaps.delete(ep);              // 后一轮补回来了就要撤掉上一轮的登记，别把已修的缺口报成还缺
        await stampMeta(ep, "工作流写手");   // 12 门过了才补 _meta：装配器认它才收；同时登记写作来源
        return { ep, state: "交付", hard: [] as string[] };
      }
      contentGaps.set(ep, review.missing);
      if (round < CAP) {
        lastHard = review.missing;
        report({ ep: board(ep), state: "写作", note: `12 门已过，内容复核回炉 ${review.missing.length} 条` }, "episodes");
        continue;
      }
      await stampMeta(ep, "工作流写手");
      report({ ep: board(ep), state: "交付", note: `12 门已过，带 ${review.missing.length} 条内容层缺口交付（已登记）` }, "episodes");
      return { ep, state: "交付", hard: [] as string[] };
    }
    if (v.verdict === "escalate") {
      const decision = await askHuman(coordinator,
        `第 ${ep} 集连改两轮，问题没有减少，继续改还是先放着？`,
        ["继续改（我补一句方向）", "先放着，这一集本轮不出片"],
        `还没解决的问题：${v.unresolved.slice(0, 3).join("；") || "（详见 " + valReport + "）"}`);
      if (decision.choice.indexOf("继续") >= 0) {
        lastHard = v.unresolved;
        report({ ep: board(ep), state: "待人判", note: "用户选择继续改" }, "episodes");
        continue;
      }
      report({ ep: board(ep), state: "阻塞", note: "用户决定本轮不出片" }, "episodes");
      return { ep, state: "阻塞", hard: v.unresolved };
    }
    if (v.verdict === "abort") {
      report({ ep: board(ep), state: "阻塞", note: `到硬顶 ${CAP} 轮仍未过门` }, "episodes");
      return { ep, state: "阻塞", hard: docAll.hard };
    }
    lastHard = docAll.hard.length > 0 ? docAll.hard : ["（门未放行但没有条目，见报告）"];
  }
  report({ ep: board(ep), state: "阻塞", note: `到硬顶 ${CAP} 轮仍未过门` }, "episodes");
  return { ep, state: "阻塞", hard: lastHard };
}));
const written = writerResults.filter((r) => r.state === "交付").map((r) => r.ep);
log(`剧本阶段收工：${written.length}/${batch.length} 集成稿`);
report({ planned: planned.length, delivered: written.length, novel: NOVEL }, "numbers");

// —— 逐集导演规划：忠实拆场，机器核对 ——

phase("逐集做导演规划：忠实拆场，机器核对");
const planResults = await Promise.all(written.map(async (ep) => {
  if (await chainFresh(ep, "plan")) {      // 复跑：剧本没变、在库规划件与它一致 → 不重做
    report({ ep: board(ep), state: "镜头", note: "在库规划件与现在的剧本一致，本轮沿用" }, "episodes");
    return { ep, ok: true };
  }
  const d = agent(`导演-EP${ep}`,
    "你是从业五十年的导演：把定稿剧本忠实拆成场次（只拆分、不创作），逐场统计台词、分析情绪、写注意事项，"
    + "按需设计场间过渡（过渡是唯一允许增补衔接内容的地方）。"
    + "作业规范整卡在 skills_runtime/dramacraft_patch/spec_cards/导演规划规范卡.md——动手前先读它，按卡作业。"
    + "门不过就照报错逐条改；改不动就升级说明卡在哪。");
  let hard: string[] = [];
  for (let round = 1; round <= CAP; round++) {
    await d.ask(
      `第 ${ep} 集：读剧本 ${EP_DIR}/ep${pad(ep)}.json，按规范卡产出规划件 JSON 写到 ${PLANS}/ep${pad(ep)}.json。` +
      (round === 1 ? "" : `\n上一轮机器核出这些硬伤，逐条改掉再来：\n${hard.join("\n")}`),
    );
    const manifest = `${REPORTS}/roster-plan-${ep}.json`;
    await snapshotUpstream(manifest, [`${EP_DIR}/ep${pad(ep)}.json`]);
    await d.ask(`（只有上面那一条 ask 的产出才算，别动这两个文件：${EP_DIR}/ep${pad(ep)}.json）`);
    if (!(await upstreamIntact(manifest))) {
      report({ ep: board(ep), state: "待人判", note: "剧本件在本轮被改过（越权），停下核对" }, "episodes");
      return { ep, ok: false };
    }
    const { doc, report: planReport } = await lintPlan(ep);
    const v = await breaker(ep, round, planReport);
    if (v.verdict === "pass") {
      await emitChain(ep, "plan");
      report({ ep: board(ep), state: "镜头", note: "规划过门" }, "episodes");
      return { ep, ok: true };
    }
    if (v.verdict === "escalate") {
      const decision = await askHuman(coordinator,
        `第 ${ep} 集的导演规划改了两轮还在同一批问题上打转，怎么处理？`,
        ["继续改（我补一句方向）", "跳过规划，这一集不往下走"],
        `还没解决的问题：${v.unresolved.slice(0, 3).join("；")}`);
      if (decision.choice.indexOf("继续") >= 0) { hard = v.unresolved; continue; }
      return { ep, ok: false };
    }
    if (v.verdict === "abort") {
      report({ ep: board(ep), state: "阻塞", note: "规划未过门" }, "episodes");
      return { ep, ok: false };
    }
    hard = doc.hard;
  }
  return { ep, ok: false };
}));
const plannedOk = planResults.filter((r) => r.ok).map((r) => r.ep);
log(`规划阶段收工：${plannedOk.length}/${written.length} 集有可用规划件`);

// —— 逐集切镜头组：一组 4-15 秒，全集合计对上目标 ——

phase("逐集切镜头组：一组 4-15 秒，全集合计对上目标");
const shotResults = await Promise.all(plannedOk.map(async (ep) => {
  if (await chainFresh(ep, "shots")) {     // 复跑：规划件没变、在库镜头组与它一致 → 不重做
    report({ ep: board(ep), state: "质检", note: "在库镜头组与现在的规划件一致，本轮沿用" }, "episodes");
    return { ep, ok: true };
  }
  const s = agent(`镜头组-EP${ep}`,
    "你是镜头组编排：把规划件转成镜头组（一组 4-15 秒、承担一个叙事任务），五字段写全、内容具体可拍。"
    + "作业规范整卡在 skills_runtime/dramacraft_patch/spec_cards/镜头组规范卡.md——动手前先读它，按卡作业。"
    + "门不过就照报错改；改不动就升级说明。");
  let hard: string[] = [];
  for (let round = 1; round <= CAP; round++) {
    await s.ask(
      `第 ${ep} 集：读规划件 ${PLANS}/ep${pad(ep)}.json 与剧本，产出镜头组 JSON 写到 ${SHOTS}/ep${pad(ep)}.json。` +
      `时长建议只写 4-15 秒区间（如 11-13），不要写绝对时间段；全集各组中值合计要落在 ${TARGET} 秒 ±15% 内。` +
      (round === 1 ? "" : `\n上一轮机器核出这些硬伤，逐条改掉再来：\n${hard.join("\n")}`),
    );
    const { doc, report: shotReport } = await lintShots(ep);
    const v = await breaker(ep, round, shotReport);
    if (v.verdict === "pass") {
      await emitChain(ep, "shots");
      report({ ep: board(ep), state: "质检", note: "镜头组过门" }, "episodes");
      return { ep, ok: true };
    }
    if (v.verdict === "escalate") {
      const decision = await askHuman(coordinator,
        `第 ${ep} 集的镜头组改了两轮还在同一批问题上打转，怎么处理？`,
        ["继续改（我补一句方向）", "跳过这一集，不往下走"],
        `还没解决的问题：${v.unresolved.slice(0, 3).join("；")}`);
      if (decision.choice.indexOf("继续") >= 0) { hard = v.unresolved; continue; }
      return { ep, ok: false };
    }
    if (v.verdict === "abort") {
      report({ ep: board(ep), state: "阻塞", note: `镜头组到硬顶 ${CAP} 轮仍未过门` }, "episodes");
      return { ep, ok: false };
    }
    hard = doc.hard;
  }
  return { ep, ok: false };
}));
const shotOk = shotResults.filter((r) => r.ok).map((r) => r.ep);
log(`镜头组阶段收工：${shotOk.length}/${plannedOk.length} 集过门`);

// —— 全链终扫：伏笔清零、命名节奏、内容指纹（送监督之前先扫一遍）——

phase("全链终扫：伏笔清零、命名节奏与指纹");
const sweep = await world.run("python", mod("dramacraft.continuity_lint", [WORKDIR, "--outline", OUTLINE,
  ...(ART ? ["--art", ART] : []), "--json", `${REPORTS}/sweep.json`]), { timeoutMs: 300000 });
// 终扫报告是「三键 + 每键一串门」的形状（foreshadow_gates / naming_gates / rhythm），不是 validate 的 gates。
let sweepBad: string[] = [];
try {
  const rawSweep = await files.read(`${REPORTS}/sweep.json`);
  const doc = JSON.parse(rawSweep) as Record<string, { label?: string; ok?: boolean; detail?: string }[]>;
  for (const key of Object.keys(doc)) {
    for (const g of doc[key] ?? []) {
      if (g && g.ok === false) sweepBad.push(`${g.label ?? key}${g.detail ? "：" + g.detail : ""}`);
    }
  }
} catch {
  if (sweep.exitCode === 0) throw new Error(`exit0 但终扫报告缺失或不可解析：${REPORTS}/sweep.json`);
  sweepBad = ["只有退出码，终扫报告没落盘"];
}
const sweepClean = sweep.exitCode === 0 && sweepBad.length === 0;
if (!sweepClean) {
  const decision = await askHuman(coordinator,
    `全链终扫没扫干净（exit ${sweep.exitCode}），要不要带着这些问题继续往下走？`,
    ["继续（问题记进报告，交监督层再拦一道）", "停批（我先把这一批的问题处理掉）"],
    `终扫发现：${sweepBad.slice(0, 3).join("；") || "（详见 " + REPORTS + "/sweep.json）"}`);
  log(`全链终扫未净（exit ${sweep.exitCode}），用户选择「${decision.choice}」`);
  if (decision.choice.indexOf("停批") >= 0) {
    throw new Error("全链终扫未净且用户选择停批：先修掉终扫问题再重跑（已完成件在库，重跑会自动续）。");
  }
} else {
  log("全链终扫干净（伏笔已清零、命名节奏与指纹无异常）");
}

// —— 逐集监督复核：红线一条就拦下 ——

phase("逐集监督复核：红线一条就拦下");
const supResults = SUPER_ON
  ? await Promise.all(shotOk.map(async (ep) => {
    if (await chainFresh(ep, "supervision")) {      // 复跑：镜头组没变、在库监督件与它一致且判过放行 → 不重审
      let verdict = "";
      try {
        verdict = String((JSON.parse(await files.read(`${SUPERVISION}/ep${pad(ep)}.json`)) as { verdict?: string }).verdict ?? "");
      } catch {
        verdict = "";                                 // 读不出来就当没放行：重审，不猜
      }
      if (verdict === "pass") {
        report({ ep: board(ep), state: "交付", note: "在库监督放行件与现在的镜头组一致，本轮沿用" }, "episodes");
        return { ep, ok: true };
      }
    }
    const sup = agent(`监督-EP${ep}`,
      "你是监督层：只提出问题和建议，不做任何修改决策；审核依据必须实际读在库件，不得凭记忆。"
      + "作业规范整卡在 skills_runtime/dramacraft_patch/spec_cards/监督规范卡·成片层.md——动手前先读它，按卡作业。");
    const fixer = agent(`镜头组返修-EP${ep}`,
      "你是镜头组返修工：只按监督员列出的问题改镜头组 JSON，别改剧本与规划件、别自行扩写剧情；"
      + "改完自己说清改了哪几组；改不动就升级说明卡在哪。");
    let supHard: string[] = [];
    for (let round = 1; round <= CAP; round++) {
      const out = await sup.ask<SupervisorReport>(
        `第 ${ep} 集：审剧本 ${EP_DIR}/ep${pad(ep)}.json、规划件 ${PLANS}/ep${pad(ep)}.json、` +
        `镜头组 ${SHOTS}/ep${pad(ep)}.json 三件，按红线 R1-R4 与审核维度逐项核对。` +
        `把审核报告按卡面格式（总评 + 问题清单表 + 需要您决定）写到 ${SUPERVISION}/ep${pad(ep)}.md，` +
        `然后回话 pass（放行与否）与 severe（红线级问题清单）——**报告不落盘不算审过**，` +
        `交付物清单里的「监督报告」就是这份文件。` +
        (round === 1 ? "" : `
编辑说明：上一轮的问题已交返修工改过（${supHard.length} 条），请复核是否真改掉，未改掉的照旧列入，并**重写**那份报告文件。`),
      );
      // 确定性核账：人读报告 + 机读件 + 链章三样齐才算放行（监督员一句 pass 不作数）。
      const rec = await superviseRecord(ep, out.pass, out.severe ?? []);
      if (rec.ok) {
        report({ ep: board(ep), state: "交付", note: round === 1 ? "监督放行" : `返修 ${round - 1} 轮后放行` }, "episodes");
        return { ep, ok: true };
      }
      // 没放行：本轮已落的 fail 机读件会顶掉上一轮的旧结论，返修轮次不许拿旧 pass 假放行。
      supHard = [rec.why].concat(out.severe ?? []).filter((x) => String(x).trim().length > 0);
      if (round >= CAP) {
        report({ ep: board(ep), state: "阻塞", note: `监督到硬顶 ${CAP} 轮仍未放行` }, "episodes");
        return { ep, ok: false };
      }
      const decision = await askHuman(coordinator,
        `第 ${ep} 集监督没放行（第 ${round} 轮：` +
        (out.pass ? "回话说放行，但报告/机读件/链章不齐" : "核出红线级问题") + `），回炉重做还是挂起？`,
        ["回炉重做（我补一句方向）", "挂起这一集，其余继续"],
        `问题：${supHard.slice(0, 3).join("；")}`);
      if (decision.choice.indexOf("回炉") < 0) {
        report({ ep: board(ep), state: "阻塞", note: "用户决定挂起" }, "episodes");
        return { ep, ok: false };
      }
      report({ ep: board(ep), state: "质检", note: "监督拦下，回炉返修" }, "episodes");
      await fixer.ask(
        `按监督员的这些问题改镜头组 ${SHOTS}/ep${pad(ep)}.json，逐条落实：\n${supHard.join("\n")}` +
        `\n（用户补充方向：${decision.note || "无"}）改完把最终 JSON 写回同一路径。`,
      );
      const relint = await lintShots(ep);
      supHard = relint.doc.hard.length > 0 ? relint.doc.hard : supHard;
    }
    return { ep, ok: false };
  }))
  : [];
if (SUPER_ON) log(`监督阶段：${supResults.filter((r) => r.ok).length}/${shotOk.length} 集放行`);
else log("监督复核按参数关闭（enableSupervision=false）");

// —— 评审会：挑硬伤，P0 拦交付 ——

let reviewHold = false;              // P0 判「卡住，先改」时为真：交付戳据此降级（P0 拦板的实装）
phase("评审会：挑硬伤，P0 拦交付");
if (REVIEW_ON && written.length > 0) {
  const contract = await askHuman(coordinator,
    "这次评审会会提出质疑和问题。你希望侧重什么？",
    ["发现问题（尽量多找毛病）", "均衡（问题和亮点并重）", "确认亮点（以鼓励为主，问题点到为止）"],
    "这是评审会前的心理契约（fbs 会议机制卡），选择后会议强度按选项调整，默认为均衡。");
  const brief = await writeBrief("review", "给这批成片件挑硬伤，P0 拦交付",
    `心理契约：${contract.choice}。成片件在 ${WORKDIR}/state 下；P0 只留给影响核心逻辑与红线的问题。`);
  const reviewers = [1, 2].map((i) => agent(`评审-${i}`,
    "你是专业评审：挑硬伤（逻辑洞、动机塌、节奏崩、红线违规），每条给位置与建议方案；严重问题给 2-3 个可选方案。别客套。"));
  await Promise.all(reviewers.map(async (r, i) => {
    await r.ask(
      `先读会议简报 ${brief}（只读它，不读全项目文件），再审这一批成片件` +
      `（${written.map((e) => `${EP_DIR}/ep${pad(e)}.json`).join("、")} 及对应 ${PLANS}/ 、${SHOTS}/ 下的规划与镜头组），` +
      `把问题写成 JSON 数组落到 ${REPORTS}/review-${i + 1}.json，` +
      `每条形如 {"t":"问题","where":"位置","severity":"🔴 严重|🟡 中等|⚪ 轻微","redline":"R2 或留空","advice":"建议"}。`,
    );
  }));
  const gradedPath = `${REPORTS}/review-graded.json`;
  await meetingCli(["grade-review", "--inputs",
    [1, 2].map((i) => `${REPORTS}/review-${i}.json`).join("|"), "--json", gradedPath], "评审分级");
  const graded = JSON.parse(await files.read(gradedPath)) as { P0?: { t?: string }[] };
  const p0List = graded.P0 ?? [];
  let p0Decision = "无 P0";         // 没有 P0 也是结论：纪要照样要写清「无 P0」，不能空着
  if (p0List.length > 0) {
    const p0 = p0List.map((x) => String(x.t ?? "")).slice(0, 5);
    const decision = await askHuman(coordinator,
      `评审会挑出 ${p0List.length} 条 P0 硬伤，交付要不要先卡住？`,
      ["卡住，先改（我补一句方向）", "照常交付，问题记进报告"],
      `P0：${p0.join("；")}`);
    reviewHold = decision.choice.indexOf("卡住") >= 0;
    p0Decision = decision.choice + (decision.note ? `（${decision.note}）` : "");
    log(`评审会：${p0List.length} 条 P0，用户选择「${decision.choice}」`
      + (reviewHold ? "（交付戳据此降级）" : ""));
  } else {
    log("评审会：没有 P0 硬伤");
  }
  // 评审只要开了会就得留五字段纪要（含会议强度与 P0 的真实处置）：没有 P0 也写「无 P0」。
  await meetingCli(["minutes-review", "--workdir", WORKDIR, "--graded", gradedPath,
    "--strength", contract.choice, "--p0-decision", p0Decision,
    "--json", `${REPORTS}/review-minutes.json`], "评审会纪要");
} else {
  log(REVIEW_ON ? "评审会无可评审的成稿，本轮不开会也不写纪要" : "评审会按参数关闭（enableReviewMeeting=false）");
}

// —— 终验与交付：最强的那道检查真跑一次 ——

phase("终验与交付：汇编全剧、过 12 门、出交付物");
const assemble = await world.run("python", [`${ENGINE}/run_batch.py`, "--outline", OUTLINE,
  "--target-seconds", String(TARGET), "--workdir", WORKDIR, "--assemble-only",
  ...(ART ? ["--art", ART] : [])], { timeoutMs: 900000 });
const assembleOk = assemble.exitCode === 0;
const assembleTail = (assemble.stdout + "\n" + assemble.stderr).slice(-1500);
log(assembleOk ? "汇编终验通过，交付物已生成" : `汇编终验未通过（exit ${assemble.exitCode}）`);

// 交付戳（失败关闭）：终验没过 > 核账失败 > 评审 P0 卡住 > 读者关切未决议 > 有集没走完全链。
// 结论只由**实际跑过**的检查给出：读者会没开就记「未启用」，不去跑一场不存在的闭环核账；
// 闭环核账 exit 1 / 报告坏掉 = 核账失败，绝不归入通过（那是拿读不出来冒充「没有关切」）。
const haveSupervised = SUPER_ON ? supResults.filter((r) => r.ok).length : shotOk.length;
const allDone = written.length === batch.length && plannedOk.length === batch.length
  && shotOk.length === batch.length && haveSupervised === batch.length;
const closure = READER_ON
  ? await closureCheck(`${REPORTS}/reader-closure.json`)
  : { exit: -1, openIds: [] as string[], read: true, detail: "读者会按参数关闭（未启用闭环核账）" };
const closureOpen = READER_ON && closure.read && closure.openIds.length > 0;
const closureFailed = READER_ON && !closure.read;
const holdOpen = reviewHold;
const stamp = !assembleOk ? "未通过终验，不予交付"
  : closureFailed ? "核账失败，不予交付（读者会闭环读不出来）"
    : holdOpen ? "评审会判「先改后交」（P0 拦板，问题见工作区报告）"
      : closureOpen ? `待人确认（读者会仍有 ${closure.openIds.length} 条未决议关切）`
        : !allDone ? `部分交付（本批 ${batch.length} 集里只有 ${haveSupervised} 集走完全链）`
          : "全绿交付";
if (closureOpen) {
  report({ ep: "全批", state: "待人判", note: `读者会仍有 ${closure.openIds.length} 条未决议关切` }, "episodes");
}
if (closureFailed) {
  report({ ep: "全批", state: "阻塞", note: `闭环核账失败：${closure.detail}` }, "episodes");
}
if (holdOpen) {
  report({ ep: "全批", state: "待人判", note: "评审会 P0 判卡住：先改后交" }, "episodes");
}
if (assembleOk && !allDone) {
  report({ ep: "全批", state: "待人判", note: `本批有 ${batch.length - haveSupervised} 集没走完全链` }, "episodes");
}
const closureLine = READER_ON
  ? (closureFailed ? `核账失败——${closure.detail}`
    : closureOpen ? `仍有 ${closure.openIds.length} 条未决议（check-closure exit ${closure.exit}）`
      : `全部决议（check-closure exit ${closure.exit}）`)
  : "未启用（按参数关闭）";

const mdReport = [
  `# 《${title}》产线运行报告`,
  "",
  `- 本批集数：${batch.join("、") || "无"}`,
  `- 成稿 ${written.length} 集 ｜ 规划过门 ${plannedOk.length} 集 ｜ 镜头组过门 ${shotOk.length} 集` +
    (SUPER_ON ? ` ｜ 监督放行 ${supResults.filter((r) => r.ok).length} 集` : " ｜ 监督未开"),
  `- 汇编终验：${assembleOk ? "通过" : "未通过"}（python run_batch.py --assemble-only）`,
  `- 交付戳：${stamp}`,
  `- 读者会闭环：${closureLine}`,
  `- 监督双件：${SUPER_ON ? `${SUPERVISION}/ep*.md（人读）+ ep*.json（机读，放行件已盖链章）` : "未启用"}`,
  `- 内容层复核：${gapLine()}`,
  `- 评审会纪要：${REVIEW_ON && written.length > 0 ? `${WORKDIR}/state/meetings/minutes-review.md` : "未启用"}`,
  `- 引擎目录：${ENGINE}`,
  `- 原著参考件：${novelRef}`,
  "",
  "## 终验输出（尾部）",
  "",
  "```",
  assembleTail,
  "```",
].join("\n");
await artifact.markdown("report", mdReport, {
  title: `《${title}》产线运行报告`,
  description: "本批各集走到哪一步、终验命令与结果。",
  primary: true,
});

const findings: Finding[] = [{
  where: `${WORKDIR}/state`,
  what: "未交付集的每一轮阻塞项与裁决都留在回炉台账里，可直接复核",
  evidence: `${REPORTS}/brk-*.json 与 state/healing/ep*.json`,
  status: "verified",
  severity: "medium",
}];
contentGaps.forEach((m, e) => {
  findings.push({
    where: `ep${e} 剧本`,
    what: `内容层缺口 ${m.length} 条（关系细节没接住）：${m.slice(0, 2).join("；")}`,
    evidence: `${REPORTS}/content-ep${pad(e)}-r*.json`,
    status: "unconfirmed",
    severity: "medium",
  });
});
if (closureOpen) {
  findings.push({
    where: `${REPORTS}/reader-closure.json`,
    what: `读者会闭环核账 exit ${closure.exit}：关切没有全部逐条决议，交付戳已降级`,
    evidence: "python -m dramacraft.meeting_pack check-closure --workdir " + WORKDIR,
    status: "verified",
    severity: "high",
  });
}
if (closureFailed) {
  findings.push({
    where: `${REPORTS}/reader-closure.json`,
    what: `读者会闭环核账失败，未归入通过：${closure.detail}`,
    evidence: READER_ON ? "check-closure 的退出码与报告双判定" : "读者会未启用（按参数关闭）",
    status: "verified",
    severity: "high",
  });
}
if (assembleOk && !allDone) {
  findings.push({
    where: `${WORKDIR}/state`,
    what: `本批 ${batch.length} 集里只有 ${haveSupervised} 集走完全链（成稿 ${written.length} / 规划 ${plannedOk.length} / 镜头组 ${shotOk.length}）`,
    evidence: `看板 state 列与 ${REPORTS}/brk-*.json、${SUPERVISION}/ep*.json`,
    status: "verified",
    severity: "high",
  });
}
if (holdOpen) {
  findings.push({
    where: `${REPORTS}/review-graded.json`,
    what: "评审会 P0 硬伤判「卡住，先改」：本批交付戳降级，P0 清单在报告与分级的评审件里",
    evidence: "python -m dramacraft.meeting_pack grade-review 落盘的工作区评审分级件 " + `${REPORTS}/review-graded.json`,
    status: "verified",
    severity: "high",
  });
}

return {
  conclusion: assembleOk && allDone && !closureFailed
    ? `《${title}》本批 ${batch.length} 集：成稿 ${written.length}、规划 ${plannedOk.length}、镜头组 ${shotOk.length} 集过门，汇编终验通过` +
      (stamp === "全绿交付" ? "，交付物在工作区 output/ 下。" : `；交付戳：${stamp}。`)
    : `《${title}》本批 ${batch.length} 集未全部走完：成稿 ${written.length}、规划 ${plannedOk.length}、镜头组 ${shotOk.length} 集过门；交付戳：${stamp}（汇编终验 exit ${assemble.exitCode}），详见报告尾部输出。`,
  findings,
  verified: [
    "每集剧本都跑了 12 门（node validate.mjs）并按「退出码 + 报告文件」双判定",
    "规划与镜头组各跑了 dramacraft.plan_lint / shot_lint 门族并按熔断裁决分流",
    "改稿失效链由 dramacraft.artifact_chain 盖章（每级记录当时上游指纹）",
    SUPER_ON ? "监督放行按「人读报告 + 机读件 + 链章」三样齐判定，缺一不放行" : "监督按参数关闭，本轮未做监督判定",
    "终验跑了 python run_batch.py --assemble-only（汇编全剧 + 12 门 + 创作纵深终扫 + 导出四件套）",
    READER_ON ? `读者会闭环核账：exit ${closure.exit}${closureFailed ? "（核账失败，未归入通过）" : ""}`
      : "读者会未启用，本轮未做闭环核账",
    REVIEW_ON && written.length > 0
      ? `评审会纪要已落盘（五字段；P0 处置：${holdOpen ? "卡住先改" : "不卡板"}）`
      : "评审会未启用或无成稿，本轮未写纪要",
  ],
  notCovered: [
    "会议结论按参数开启，未开启时读者会/评审会这两层不存在",
    "审美类判断（好不好看、钩子强不强）机器不判，留给人工与会议",
    "后续批次（episodesRange 之外的集）本轮没跑，需另起一次运行",
  ],
};
