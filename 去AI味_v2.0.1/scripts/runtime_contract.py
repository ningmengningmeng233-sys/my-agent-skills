#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""runtime_contract.py - 运行包文件清单的唯一权威（v1.2 Task 4）。

为什么单独成模块：v1.1 把「哪些文件算运行件」这件事抄了四份——构建白名单
`build_release.WHITELIST`、验收必需集合 `accept_release.REQUIRED_RUNTIME_FILES`、
预算的首载与参考名单 `check_budget`、逐字节 parity 清单 `verify_chain_consistency`。
设计方案「先修复的现有问题」第 11 项要求新增运行件同步登记这四处；抄四份的实际后果是
**漏改的那一份不会报错**，只会让发布门拿去比一个错包。集中成一份后，登记一处即四处生效。

本文件只表达**当前包**（v1.2：以 v1.1.3 的十件运行文件为基底 + ``scripts/text_source.py``
+ Task 6 落地的 ``scripts/novel_job.py`` + Task 7 落地的 ``scripts/novel_segments.py``
+ Task 8 落地的 ``scripts/novel_contracts.py``
+ Task 10 落地的 ``scripts/novel_validation.py`` + Task 11 落地的 ``scripts/novel_review.py``
+ Task 12 落地的 ``scripts/novel_output.py``（安全交付合同：no-clobber 另存与受控覆盖）
+ Task 9A 落地的 ``THIRD_PARTY_NOTICES.md``
+ Task 9B 落地的 ``references/editorial-rules-zh.md`` 与 ``references/fiction-zh.md``）。
v1.2 的其余新运行件（novel_* 后续脚本）由产生它的任务在自己的提交里追加到这里，不在本任务预登记：预登记会让门去核一个还不存在的文件，
红的就不是该红的那一条。9A 把两份规则层暂存在 ``docs/v1.2-rule-layers/`` 而未登记，是因为
预算门要求「打包的每份参考都必须被入口路由」，而入口 ``SKILL.md`` 的写入面属于 Task 9B；
9B 已把它们 ``git mv`` 进 ``references/``、登记进必需集合与 parity，并只由入口在命中场景按需路由。

清单的分工（全部是仓库相对 POSIX 路径，绝不出现机器专属绝对路径）：

  REQUIRED_RUNTIME_FILES
    安装包必须携带的运行文件集合。构建器的白名单与验收器的结构/身份门共用它。
  FIRST_LOAD_FILES
    普通首载集合，预算 26000 字符门的计量对象（设计方案「入口预算」）。
  NOVEL_SCENE_LOAD_FILES
    小说场景的实际加载集合。设计方案「入口预算」要求普通文稿与小说**分别**报告实际读取文件与
    字节负担，而四元组表达不出「打包，但只在小说场景加载」：``REQUIRED_RUNTIME_FILES`` 只说
    带不带，``FIRST_LOAD_FILES`` 只说普通首载，``RUNTIME_PARITY_FILES`` 只说逐字节链，
    ``MANDATORY_NOTICE_FILES`` 只说声明类。所以补这第五份清单专门承载「按场景加载」。
    它是 ``FIRST_LOAD_FILES`` 的**超集**（小说也要读普通必读件），多出来的那几件才是小说场景
    额外付的上下文代价；它不得等于首载集合，否则「按场景加载」形同虚设。
    26000 字符的普通首载门只量 ``FIRST_LOAD_FILES``，所以小说规则层进包不会挤占非小说路径预算。
  MANDATORY_NOTICE_FILES
    许可与声明类运行文件。缺件就是成品漏声明（先修问题第 9 项），构建与验收都拦。
  RUNTIME_PARITY_FILES
    开发源与安装版必须逐字节一致的子集（全链一致性门的运行件链）。

``contract_problems()`` 把「这几份清单互相自洽」也变成可执行断言：首载/场景加载/声明/parity 都必须是
必需集合的子集，路径必须仓库相对，主入口不得掉出清单，小说场景集合必须真比普通首载多出参考。
构建器在动手复制文件之前先过 ``validate_contract()`` 这道校验。

``build_installed_runtime()`` / ``install_package()`` 是「临时安装夹具」的唯一实现，
pytest 侧由 ``tests/conftest.py`` 的 ``installed_runtime`` 包装。开发期所有门只跑这种现场构建
出来的 candidate/installed（设计方案「发布证据与实施环境」），真实日常安装目录不被反复覆盖。
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent

# ---- 当前包（v1.2）的唯一清单 ---------------------------------------------
REQUIRED_RUNTIME_FILES: tuple[str, ...] = (
    "SKILL.md",
    # v1.2.1：收件人说明随包（安装、依赖、能力边界、工作区隐私、备份恢复、卸载）。
    # 刻意放顶层、不进 references/：入口 10240B 预算不允许再路由新参考，预算门只量
    # 首载与参考集合，顶层说明件不占模型上下文。
    "USAGE.md",
    "agents/openai.yaml",
    "assets/less-ai-tone-hero-zh.png",
    "references/contextual-patterns-zh.md",
    "references/editorial-rules-zh.md",
    "references/evidence-rules-zh.md",
    "references/fiction-zh.md",
    # FP 专项迭代：小说伪文艺腔规则层（FP01–FP08），只在小说场景命中 FP 类别或
    # 用户明确要求检查小说文艺腔时加载，绝不进普通首载与声明清单。
    "references/fiction-prose-patterns-zh.md",
    "references/fidelity.md",
    "references/scenes-and-boundaries.md",
    "scripts/file_commit.py",
    "scripts/novel_contracts.py",
    "scripts/novel_job.py",
    "scripts/novel_output.py",
    "scripts/novel_review.py",
    "scripts/novel_segments.py",
    "scripts/novel_validation.py",
    # v2.0.0（alpha.1 P02）：v2 交互状态机与中文交互说明。状态机是授权门权威，
    # 交互文案承载入口三选一/Jev 必问/任务摘要，均属运行件随任务登记。
    "references/v2-interaction-zh.md",
    "scripts/v2_session.py",
    # v2.0.0（alpha.1 P03）：v2 任务接线——正文范围发现与现有 job 的附着层，
    # 复用 novel_job 的锁/认领/事件/恢复，属运行件随任务登记。
    "scripts/v2_task.py",
    # v2.0.0（alpha.1 P04）：textlint 结构候选桥三件套。Python 侧运行时调用
    # node bridge.mjs，桥与规则、Node 依赖锁都必须随包，否则安装后结构扫描
    # 必然失败；node_modules 本体不随包，由 npm ci 按 lock 重建（P17 全链验证）。
    "rules/zh-candidates.mjs",
    "runtime/textlint/bridge.mjs",
    "runtime/textlint/package.json",
    "runtime/textlint/package-lock.json",
    "scripts/v2_structure.py",
    # v2.0.0（alpha.1 P05）：规则级决策与中文报告运行件 + 十三类判据文档。
    "scripts/v2_report.py",
    # v2.0.0（alpha.2 P06）：text-dedup 模板近重复候选适配层。调用上游 fingerprint/
    # cluster/jaccard，属运行件随任务登记；import 链由构建器扫描看不见，必须显式列出。
    "scripts/v2_similarity.py",
    # v2.0.0（alpha.2 P07）：BGE 向量提供方与长文完整覆盖窗口；缓存是可重建附属件。
    # 两者都被 v2_similarity 直接 import，构建器的文本引用扫描看不见，必须显式登记。
    "scripts/v2_embeddings.py",
    "scripts/v2_cache.py",
    # v2.0.0（alpha.2 P08）：分项判定组合政策（五问、三值结论、发布权与校准门）。
    "scripts/v2_judge.py",
    # v2.0.0（alpha.3 P09）：官方 TypeSafe SDK 的 Jev 裁判适配与凭据引用层。
    # 它对官方 SDK 与 keyring 都是**函数内惰性导入**（Jev 是可显式关闭的可选裁判，
    # 接收方没装依赖时整包 import 烟测不能被这一层拖垮），构建器的引用扫描看不见
    # 这种 import，也不会有任何文本引用指向它——只能在此显式登记，缺件即缺门。
    "scripts/v2_jev.py",
    # v2.0.0（alpha.3 P10）：受约束候选生成的接线层（只检查/KEEP/不足不生成、
    # 去味不调润色、补写与磨平标点一律丢）。它不产生文本，只把宿主的建议收成
    # patch 并守住边界，属运行件；P11 的验证与 P12 的预览都要消费它。
    "scripts/v2_edit.py",
    # v2.0.0（alpha.3 P11）：三段文本接进现有 L0–L3 验证与一次有界再检查的接线层。
    # 它自己不判保真，全部结论来自 novel_validation / novel_contracts / novel_review；
    # 没有任何文本引用指向它，构建器扫不到，只能在此显式登记。
    "scripts/v2_verify.py",
    # v2.0.0（alpha.3 P12）：按章预览、逐项接受与「只走现有 publish」的提交前置门。
    # 它自己不写盘，但拒绝发生在写之前——这道门的字节就是「零触碰」的最后一道凭据。
    "scripts/v2_preview.py",
    # v2.0.0（beta P14）：项目反馈作用域与可选中文 NLP 通道。两者都不产生候选、
    # 不进判级面，但「喜恶不算 AI 证据」「默认 none 不隐式下载」这两条合同就写在
    # 字节里；import 链与文本引用都扫不到它们，只能在此显式登记，缺件即缺门。
    "scripts/v2_preferences.py",
    "scripts/v2_nlp.py",
    "references/v2-candidate-rules-zh.md",
    # v1.2.1（C1 缺件修复）：novel_segments.py 顶层 `from runtime_contract import
    # FIRST_LOAD_FILES, REPO_ROOT`，构建清单却漏登记本模块——v1.2.0 包安装后整条
    # 小说通路 import 即崩。import 依赖与文本引用不同，构建器的引用扫描看不见它，
    # 必须在此显式登记（test_release_package 的 -I 隔离加载烟测钉住）。
    "scripts/runtime_contract.py",
    "scripts/text_source.py",
    # v1.2.1（C1 缺件修复）：novel_job.py 顶层 `from tool_paths import AGENT_HOME_DIRS`，
    # 同因漏登记而缺件；resolve_* 其余能力对接收者是惰性可选项，不使用不受伤。
    "scripts/tool_paths.py",
    "scripts/validate-rewrite.py",
    # v2.0.0（alpha.1 P01）：v2 共用数据合同。总工单要求所有 v2 结构集中在该模块、
    # 后续任务只消费它的类型与校验函数，属于随任务登记的运行件；import 链与文本
    # 引用不同，构建器扫描看不见，必须在此显式登记。
    "scripts/v2_contracts.py",
    # v2.0.0（alpha.1 补正 Q04）：v2_task.runtime_identity 复用本模块的
    # package_digest/read_skill_version 计算真实运行件身份，import 链必须随包。
    "scripts/package_identity.py",
    # v2.0.0（beta P17）：精确依赖锁随包。收件人拿到的运行件清单声明了第三方顶层
    # 依赖（numpy/transitions/text-dedup/…），没有锁就复现不出同一套环境，
    # 「装得上但跑不起」与「跑得起但版本不同」两类事故都无法自证；锁本身进 parity，
    # 字节漂了就是环境漂了。
    "requirements-v2.lock",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
)

FIRST_LOAD_FILES: tuple[str, ...] = (
    "SKILL.md",
    "references/fidelity.md",
    "references/evidence-rules-zh.md",
)

# 小说场景的实际加载集合：普通必读件 + 只有小说场景才读的叙事层与共用编辑层。
# 数值由 check_budget 报成「小说高命中实际加载」，与「普通高命中实际加载」分开统计
# （设计方案 106 行要求两组数字各报一次）。这两层绝不进 FIRST_LOAD_FILES：
# 26000 字符的普通首载门是非小说路径的预算，不该为小说规则付钱。
NOVEL_SCENE_LOAD_FILES: tuple[str, ...] = (
    "SKILL.md",
    "references/fidelity.md",
    "references/evidence-rules-zh.md",
    "references/editorial-rules-zh.md",
    "references/fiction-zh.md",
    # FP 专项迭代：伪文艺腔层是小说场景在命中 FP 类别／用户点名文艺腔时的第四层选读，
    # 只在小说场景付费，普通文稿路径不加载。
    "references/fiction-prose-patterns-zh.md",
)

# ---- 命中类别/场景后追加的两档（补正 Q06/R06：从 check_budget 上收到本模块）--------
# 为什么上收：这两组集合原先只活在预算门里，v2 的规则加载路由要复用就得 import 预算门，
# 而预算门不是运行包成员；在 v2_report 里另抄一份「命中 ED 读编辑层」就是第二份允许清单，
# 预算改了路由不会跟着改。清单分工不变：REQUIRED 说带不带，FIRST_LOAD / NOVEL_SCENE 说
# 场景地板，这两档说「命中之后追加什么」，``rule_load_plan`` 只做组合，不新增成员。
# 集合与 SKILL.md §8 的对应关系由 tests/test_budget_contract.py 逐件钉住。
ORDINARY_HIT_EXTRA_FILES: tuple[str, ...] = (
    "references/editorial-rules-zh.md",
)
SCENE_HIT_EXTRA_FILES: tuple[str, ...] = (
    "references/contextual-patterns-zh.md",
    "references/scenes-and-boundaries.md",
)

# v2 会话通路必读的两份（alpha.2 预算与路由审计）：交互合同与十三类候选判据。
# 它们原先只由 SKILL.md §8 的文字路由隐含承载，既不在首载也不在场景档，
# 于是 check_budget 的「实际加载」统计对 v2 通路是盲区——登记成一档，
# 让计量与路由说的是同一件事；这既不放宽任何门，也不把内容搬去不计量目录。
V2_FLOW_EXTRA_FILES: tuple[str, ...] = (
    "references/v2-interaction-zh.md",
    "references/v2-candidate-rules-zh.md",
)

# 共用编辑层的规则前缀：命中 ED 类（含 ED01 五问判序）时普通文稿就要读编辑层；
# 小说层文件（fiction-zh / fiction-prose-patterns）只在 novel 场景进清单。
EDITORIAL_RULE_PREFIX = "ED"


def rule_load_plan(scene: str = "normal", rule_ids=(),
                   cluster_or_scene_hit: bool = False, *,
                   v2_flow: bool = False) -> tuple:
    """按场景与命中类别组合真实加载清单——只读上面几份权威集合，不另设清单。

    ``scene``：``normal``（普通文稿）或 ``novel``（小说稿）；其余值直接拒绝，
    不给「未知场景」猜一个默认清单。``rule_ids``：本次命中的规则 ID，ED 前缀命中
    共用编辑层。``cluster_or_scene_hit``：SKILL.md §8「聚集选读／场景选读」两条命中
    （成片套话、长文、公开发布稿、技术文档、状态汇报、边界存疑）。
    ``v2_flow``：走 v2 会话通路时追加 ``V2_FLOW_EXTRA_FILES``（交互合同 + 候选判据）。

    保证的关系：novel 是 normal 的超集；normal 永不包含小说层文件；普通长文可以按既有
    合同读聚集/场景档，「不读小说层」不等于「不读任何额外参考」。
    """
    if scene == "normal":
        plan = list(FIRST_LOAD_FILES)
    elif scene == "novel":
        plan = list(NOVEL_SCENE_LOAD_FILES)
    else:
        raise ValueError(f"未知场景: {scene!r}（只支持 normal / novel）")
    if any(str(rid).startswith(EDITORIAL_RULE_PREFIX) for rid in rule_ids):
        plan += list(ORDINARY_HIT_EXTRA_FILES)
    if cluster_or_scene_hit:
        plan += list(SCENE_HIT_EXTRA_FILES)
    if v2_flow:
        plan += list(V2_FLOW_EXTRA_FILES)
    out: list = []
    for rel in plan:
        if rel not in out:
            out.append(rel)
    return tuple(out)


MANDATORY_NOTICE_FILES: tuple[str, ...] = (
    "LICENSE",
    # v1.2 Task 9A：设计方案把 THIRD_PARTY_NOTICES.md 定为必需运行文件——「记录 Humanizer-zh、
    # 实际上游来源、版权和 MIT 许可」。它原先只是构建白名单的可选件（带不带看开关），
    # 那是「成品漏声明」的口子（先修问题第 9 项），现在归到这里，构建与验收两头都拦；
    # Task 13 进一步删掉了那个永不改变产物的死开关，声明文件无条件随包。
    "THIRD_PARTY_NOTICES.md",
)

RUNTIME_PARITY_FILES: tuple[str, ...] = (
    "SKILL.md",
    "USAGE.md",
    "scripts/validate-rewrite.py",
    "scripts/file_commit.py",
    "scripts/novel_contracts.py",
    "scripts/novel_job.py",
    "scripts/novel_output.py",
    "scripts/novel_review.py",
    "scripts/novel_segments.py",
    "scripts/novel_validation.py",
    # v2.0.0（alpha.1 P02）：交互状态机与授权文案进逐字节 parity 链——
    # 授权语义字节漂了就是授权漂了。
    "references/v2-interaction-zh.md",
    "scripts/v2_session.py",
    # v2.0.0（alpha.1 P03）：任务接线（范围发现 + job 附着）同进 parity 链。
    "scripts/v2_task.py",
    # v2.0.0（alpha.1 P04）：结构候选桥与规则进 parity 链——候选语义字节漂了
    # 就是候选漂了。
    "rules/zh-candidates.mjs",
    "runtime/textlint/bridge.mjs",
    "scripts/v2_structure.py",
    # v2.0.0（alpha.2 P06）：候选语义字节漂了就是候选漂了，进逐字节链。
    "scripts/v2_similarity.py",
    # v2.0.0（alpha.2 P07）：模型身份与缓存键形状都在字节里，漂了就是候选漂了。
    "scripts/v2_embeddings.py",
    "scripts/v2_cache.py",
    # v2.0.0（alpha.2 P08）：判定组合政策与发布权门字节漂了就是结论漂了。
    "scripts/v2_judge.py",
    # v2.0.0（alpha.3 P09）：裁判适配层的字节漂了就是「重试几次、概率怎么落册」漂了，
    # 这三件事都是授权与判定的输入，进逐字节链。
    "scripts/v2_jev.py",
    # v2.0.0（alpha.3 P10）：受约束候选生成接线。哪道闸丢弃了宿主的建议、
    # 哪种改动算补写与磨平，都在字节里；漂了就是改写权限漂了，进逐字节链。
    "scripts/v2_edit.py",
    # v2.0.0（alpha.3 P11）：验证接线同样进逐字节链——「哪一份文本被验证过」
    # 由它算出的哈希说话，字节漂了就是结论漂了。
    "scripts/v2_verify.py",
    # v2.0.0（alpha.3 P12）：提交前置门进逐字节链——接受凭据与位置不猜的规则
    # 漂一个字节，就可能把没被接受的改写写进正文。
    "scripts/v2_preview.py",
    # v2.0.0（beta P14）：偏好作用域与 NLP 后端选择的字节都携带合同语义——
    # 「默认 none」「许可未确认不得选」「事件只记摘要」漂一个字节就变味，进逐字节链。
    "scripts/v2_preferences.py",
    "scripts/v2_nlp.py",
    # v2.0.0（beta P17）：锁也进逐字节链——锁漂一个版本号，收件人的环境就不是那套环境。
    "requirements-v2.lock",
    "scripts/v2_report.py",
    "references/v2-candidate-rules-zh.md",
    # v1.2.1（C1）：两件 import 链运行件进逐字节 parity 链。
    "scripts/runtime_contract.py",
    "scripts/text_source.py",
    "scripts/tool_paths.py",
    # v2.0.0（alpha.1 P01）：v2 共用数据合同进逐字节 parity 链——它锁定授权
    # 枚举与校验语义，字节漂了就是授权漂了。
    "scripts/v2_contracts.py",
    "references/fidelity.md",
    "references/evidence-rules-zh.md",
    "references/contextual-patterns-zh.md",
    "references/scenes-and-boundaries.md",
    # 两份规则层也要进链：它们是普通改写的裁判文本，字节漂了就是规则漂了。
    "references/editorial-rules-zh.md",
    "references/fiction-zh.md",
    # FP 专项迭代：伪文艺腔规则层同样是裁判文本，进逐字节 parity 链。
    "references/fiction-prose-patterns-zh.md",
)

ENTRYPOINT = "SKILL.md"
# 仓库相对 POSIX 路径的否定形状：绝对路径、盘符、~ 家目录、反斜杠、上级逃逸
_UNSAFE_PATH_RE = re.compile(r"^(?:/|[A-Za-z]:|~)|\\")

# ---- v2.0.0（alpha.1 P01）：外部运行依赖权威登记表 --------------------------
# v2 直接依赖的第三方库逐项登记：发行包名、最低版本、可选组。登记表是
# scripts/v2_contracts.validate_external_imports 的唯一对照源（不复制第二份）；
# 「发行件在 PyPI/npm 实际存在且 Windows 兼容」由 P01 的安装验证负责并在
# requirements-v2.lock / runtime/textlint/package-lock.json 里锁定精确版本。
# 可选组 None = 核心 v2 依赖；HanLP/LTP 等可选 NLP 通道由 P14 以自己的可选组
# 登记，不进核心组。禁止扩大本表掩盖缺件——每次扩表必须与真实安装验证同任务。
EXTERNAL_DEPENDENCIES: dict = {
    "transitions": {
        "distribution": "transitions", "min_version": "0.9.4",
        "optional_group": None},
    "keyring": {
        "distribution": "keyring", "min_version": "22.0",
        "optional_group": None},
    "typesafe_sdk": {
        "distribution": "typesafe-sdk", "min_version": "0.7.2",
        "optional_group": None},
    "text_dedup": {
        "distribution": "text-dedup", "min_version": "0.4.1",
        "optional_group": None},
    "semhash": {
        "distribution": "semhash", "min_version": "0.5.0",
        "optional_group": None},
    "FlagEmbedding": {
        "distribution": "FlagEmbedding", "min_version": "1.4.2",
        "optional_group": None},
    "numpy": {
        "distribution": "numpy", "min_version": "1.26",
        "optional_group": None},
    # v2.0.0（alpha.2 P06）：v2_similarity 把内存数据集直接交给上游 text-dedup 的
    # fingerprint/cluster，datasets 是我们自己 import 的顶层依赖（不只是传递路径），
    # 按纪律显式登记，包内 import 门才认它。
    "datasets": {
        "distribution": "datasets", "min_version": "2.18.0",
        "optional_group": None},
    # v2.0.0（alpha.2 P07）：v2_embeddings 用 snapshot_download(local_files_only=True)
    # 从钉定本地快照加载 BGE，禁隐式下载；实测版本 1.33.0，登记可用下限。
    "huggingface_hub": {
        "distribution": "huggingface_hub", "min_version": "0.23.0",
        "optional_group": None},
}


CAPABILITY_SCHEMA = "lieflat-v2-capability-report-v1"
DEPENDENCY_LOCK_FILE = "requirements-v2.lock"
#: 本轮明确停用的能力面（照实写，不用「没发现问题」冒充「已验证」）。
DISABLED_CAPABILITIES: tuple[str, ...] = (
    "真实 Jev 调用", "宿主自然触发", "人工校准", "高风险自动提交",
    "真实中文 NLP 组件推理", "整本真实稿效果")


def capability_report(root=None) -> dict:
    """一次性能力披露：随包运行件、精确依赖锁、可选后端与被停用的能力面。

    许可**不在**这里复述：许可与来源声明的唯一落点是 ``THIRD_PARTY_NOTICES.md``
    （登记纪律），本函数只报「装了什么/能不能用/被停用什么」，所以输出里不出现
    任何许可文本，避免同一事实两处说法。
    """
    import v2_nlp

    base = Path(root) if root is not None else REPO_ROOT
    locked = {}
    lock_path = base / DEPENDENCY_LOCK_FILE
    if lock_path.is_file():
        for raw in lock_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # 锁有两种形状：`name==版本` 与 PEP 508 直接引用
            # （transitions 走 git+https 钉提交，因为 PyPI 上没有 0.9.4 发行件）。
            # 只按 `==` 切会把直接引用整条漏掉，登记表与锁的对账就成了假账。
            if "==" in line:
                name, version = line.split("==", 1)
            elif " @ " in line:
                name, version = line.split(" @ ", 1)
            else:
                continue
            locked[name.strip().lower()] = version.strip()
    return {
        "schema": CAPABILITY_SCHEMA,
        "runtime_files": len(REQUIRED_RUNTIME_FILES),
        "dependency_lock": DEPENDENCY_LOCK_FILE,
        "lock_present": lock_path.is_file(),
        "external_dependencies": [
            {"import": name, "distribution": entry["distribution"],
             "min_version": entry["min_version"],
             "optional_group": entry["optional_group"],
             "locked_version": locked.get(entry["distribution"].lower())}
            for name, entry in sorted(EXTERNAL_DEPENDENCIES.items())],
        "optional_backends": [v2_nlp.capability(name) for name in v2_nlp.BACKENDS],
        "disabled_components": list(DISABLED_CAPABILITIES),
        "notices": "THIRD_PARTY_NOTICES.md",
    }


def _path_problems(label: str, paths) -> list:
    problems = []
    seen = set()
    for rel in paths:
        raw = str(rel)
        if not raw.strip():
            problems.append(f"{label} 含空路径")
            continue
        if _UNSAFE_PATH_RE.search(raw):
            problems.append(f"{label} 不是仓库相对 POSIX 路径: {raw}")
        if ".." in Path(raw).parts:
            problems.append(f"{label} 含上级逃逸: {raw}")
        if raw in seen:
            problems.append(f"{label} 重复登记: {raw}")
        seen.add(raw)
    return problems


def runtime_references(required=REQUIRED_RUNTIME_FILES) -> tuple:
    """必需集合里的运行参考（文件名，排序）——预算门的参考名单由它推导。"""
    return tuple(sorted(Path(rel).name for rel in required
                        if str(rel).startswith("references/")))


def contract_problems(required, first_load, notices, parity,
                      novel=NOVEL_SCENE_LOAD_FILES) -> list:
    """各份清单互相之间必须成立的关系；返回所有不成立的地方（空列表 = 自洽）。

    关系方向是刻意的：首载 / 场景加载 / 声明 / parity 都是「必需集合」的子集。子集关系破了，
    就意味着某处代码在要求一个构建器不会打包、验收器也不认识的文件——那是静默失效，比红更糟。
    小说场景集合另加三条它自己的关系：必须含入口、必须是首载的超集、必须真比普通首载多读
    至少一份参考（否则「按场景加载」只是把首载换了个名字，小说层的加载条件就成了空话）。
    """
    problems: list = []
    for label, paths in (("REQUIRED_RUNTIME_FILES", required),
                         ("FIRST_LOAD_FILES", first_load),
                         ("NOVEL_SCENE_LOAD_FILES", novel),
                         ("MANDATORY_NOTICE_FILES", notices),
                         ("RUNTIME_PARITY_FILES", parity)):
        problems += _path_problems(label, paths)
    required_set = {str(r) for r in required}
    if ENTRYPOINT not in required_set:
        problems.append(f"主入口 {ENTRYPOINT} 不在必需运行文件集合里")
    for label, paths in (("FIRST_LOAD_FILES", first_load),
                         ("NOVEL_SCENE_LOAD_FILES", novel),
                         ("MANDATORY_NOTICE_FILES", notices),
                         ("RUNTIME_PARITY_FILES", parity)):
        for rel in paths:
            if str(rel) not in required_set:
                problems.append(f"{label} 含 REQUIRED_RUNTIME_FILES 之外的条目: {rel}")
    if ENTRYPOINT not in {str(r) for r in first_load}:
        problems.append(f"首载清单没有以 {ENTRYPOINT} 开头/包含入口: {tuple(first_load)}")
    novel_set = {str(r) for r in novel}
    if ENTRYPOINT not in novel_set:
        problems.append(f"小说场景集合不含主入口 {ENTRYPOINT}: {tuple(novel)}")
    missing_from_novel = sorted({str(r) for r in first_load} - novel_set)
    if missing_from_novel:
        problems.append(f"小说场景集合漏掉了普通必读件: {missing_from_novel}")
    extra_refs = sorted(rel for rel in novel_set - {str(r) for r in first_load})
    if not extra_refs:
        problems.append(f"小说场景集合没有比普通首载多读任何参考，按场景加载形同虚设: {tuple(novel)}")
    for rel in extra_refs:
        if not (rel.startswith("references/") and rel.endswith(".md")):
            problems.append(f"小说场景额外加载的必须是 references/ 下的 .md: {rel}")
    notice_set = {str(r) for r in notices}
    overlap = sorted(novel_set & notice_set)
    if overlap:
        problems.append(f"许可与声明类文件不该被算进场景加载集合: {overlap}")
    return problems


def validate_contract(required=REQUIRED_RUNTIME_FILES, first_load=FIRST_LOAD_FILES,
                      notices=MANDATORY_NOTICE_FILES, parity=RUNTIME_PARITY_FILES,
                      novel=NOVEL_SCENE_LOAD_FILES) -> None:
    """清单自洽性闸门：不自洽就 ValueError，调用方（构建器/测试）据此拒绝继续。"""
    problems = contract_problems(required, first_load, notices, parity, novel)
    if problems:
        raise ValueError("运行时清单不自洽，拒绝据此构建/核对：\n  " + "\n  ".join(problems))


def _load_build_release():
    """按需取构建器：不在模块顶部 import，避免与 build_release 形成导入环。"""
    here = str(SCRIPTS_DIR)
    if here not in sys.path:
        sys.path.insert(0, here)
    import build_release
    return build_release


# 夹具标记：放在夹具目录**旁边**而不是里面——包目录多一个文件就会被验收判成
# 「白名单外文件」。有标记才允许被本门清空重建：没有标记的目录哪怕带
# release-manifest.json 也可能是别人的真实安装版，静默删掉它不可接受。
FIXTURE_MARKER_SUFFIX = ".lieflat-fixture.json"
FIXTURE_MARKER_SCHEMA = "lieflat-fixture-marker-v1"


def fixture_marker_path(directory) -> Path:
    directory = Path(directory)
    return directory.parent / (directory.name + FIXTURE_MARKER_SUFFIX)


def is_fixture_dir(directory) -> bool:
    """这个目录是不是本工具链自己构建过的临时夹具。"""
    return fixture_marker_path(directory).is_file()


def write_fixture_marker(directory, built_by: str) -> Path:
    """登记「此目录由现场构建产生」；只写相对事实（不含绝对源路径与时间戳）。"""
    marker = fixture_marker_path(directory)
    digest = None
    manifest = Path(directory) / "release-manifest.json"
    if manifest.is_file():
        try:
            digest = json.loads(manifest.read_text(encoding="utf-8")).get("package_digest")
        except (OSError, ValueError):
            digest = None
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"schema": FIXTURE_MARKER_SCHEMA,
                                  "built_by": str(built_by),
                                  "package_digest": digest},
                                 ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8", newline="\n")
    return marker


def build_candidate(source_root, candidate_dir) -> Path:
    """从当前源码现场构建一份发布候选到 candidate_dir（目录已存在即拒绝）。

    v1.2 Task 13：许可/声明类运行件无条件随包，构建器不再有 include 开关，
    这里也就不再传任何能造出「缺声明包」的参数。
    """
    candidate_dir = Path(candidate_dir)
    source_root = Path(source_root)
    build_release = _load_build_release()
    build_release.build(source_root, candidate_dir)
    write_fixture_marker(candidate_dir, "runtime_contract.build_candidate")
    return candidate_dir


def install_package(candidate_dir, installed_dir) -> Path:
    """把候选整份复制成安装版（含 release-manifest.json，逐字节相同）。

    安装版是**复制**而不是再构建一次：门要比的「候选 → 安装版」这一环，
    只有拿同一份字节去比才有意义。
    """
    candidate_dir = Path(candidate_dir)
    installed_dir = Path(installed_dir)
    if not (candidate_dir / "release-manifest.json").is_file():
        raise SystemExit(f"[STOP] 候选缺少 release-manifest.json，拒绝据此安装: {candidate_dir}")
    if installed_dir.exists():
        raise SystemExit(f"[STOP] 安装夹具目录已存在，拒绝覆盖既有证据: {installed_dir}")
    installed_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(candidate_dir, installed_dir)
    write_fixture_marker(installed_dir, "runtime_contract.install_package")
    return installed_dir


def build_installed_runtime(work_dir, source_root=None, name="runtime") -> tuple:
    """在 work_dir 下建一对独立夹具：``(candidate_dir, installed_dir)``。

    每次调用都用自己的 ``name`` 子目录，互不复用、互不覆盖；写操作只在 work_dir 内，
    绝不触碰任何真实安装目录。这就是 ``installed_runtime`` pytest 夹具的实现本体，
门与测试都走这一份，不再有第二套「手搓安装目录」的写法。

v1.2.1（发布脱敏轮）：``scripts/runtime_contract.py`` 与 ``scripts/tool_paths.py`` 登记进必需集合与
parity。它们被 novel_job.py / novel_segments.py 顶层 import，是运行件而不是开发件——v1.2.0 清单漏
登记这两个模块，装出的包小说通路 import 即崩（构建器的文本引用扫描看不见 import 语句，构建侧由
build_release 的 import 完整性门补位）。
"""
    root = Path(work_dir)
    base = root / str(name)
    candidate = build_candidate(Path(source_root) if source_root else REPO_ROOT,
                                base / "candidate")
    return candidate, install_package(candidate, base / "installed")


if __name__ == "__main__":
    for _label, _paths in (("REQUIRED_RUNTIME_FILES", REQUIRED_RUNTIME_FILES),
                           ("FIRST_LOAD_FILES", FIRST_LOAD_FILES),
                           ("NOVEL_SCENE_LOAD_FILES", NOVEL_SCENE_LOAD_FILES),
                           ("MANDATORY_NOTICE_FILES", MANDATORY_NOTICE_FILES),
                           ("RUNTIME_PARITY_FILES", RUNTIME_PARITY_FILES)):
        print(f"{_label} ({len(_paths)}):")
        for _rel in _paths:
            print(f"  {_rel}")
    validate_contract()
    print("清单自洽: PASS")
