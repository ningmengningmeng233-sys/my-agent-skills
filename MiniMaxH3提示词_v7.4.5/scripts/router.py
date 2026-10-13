#!/usr/bin/env python3
"""Five-Dimensional Routing Engine and Model Capability Validator for MiniMax H3 Craft."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Literal

ModelType = Literal["H3", "H3-Max", "H3-Max-Turbo"]
OutputProfile = Literal["ref2va-en", "native-drama-series", "native-general"]
WorkflowProfile = Literal["general", "drama-series"]
MediumType = Literal["live-action", "animation", "other"]

# Canonical mode aliases mapping
MODE_ALIASES: dict[str, str] = {
    "t2v": "t2va",
    "t2va": "t2va",
    "i2v": "i2va",
    "i2va": "i2va",
    "first-last-frame": "fl2va",
    "fl2v": "fl2va",
    "fl2va": "fl2va",
    "last-frame": "l2va",
    "l2v": "l2va",
    "l2va": "l2va",
    "multimodal": "ref2va",
    "multimodal-ref": "ref2va",
    "ref2va": "ref2va",
}


def load_model_capabilities() -> dict[str, Any]:
    """Load model capabilities dynamically from config/model-capabilities.json."""
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config",
        "model-capabilities.json",
    )
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("models", {})
        except Exception:
            pass

    # Safe built-in fallback
    return {
        "H3": {
            "verified_status": "VERIFIED_OFFICIAL",
            "modes": ["t2va", "i2va", "fl2va", "l2va", "ref2va"],
            "resolutions": ["768P", "2K"],
            "duration_seconds": {"min": 4, "max": 15},
            "supported_aspect_ratios": ["16:9", "9:16", "1:1", "4:3", "21:9"],
        },
        "H3-Max": {
            "verified_status": "VERIFIED_OFFICIAL",
            "modes": ["t2va", "fl2va", "ref2va"],
            "resolutions": ["480P", "768P"],
            "duration_seconds": {"min": 5, "max": 15},
            "supported_aspect_ratios": ["16:9", "9:16", "1:1"],
        },
        "H3-Max-Turbo": {
            "verified_status": "VERIFIED_OFFICIAL",
            "modes": ["t2va", "fl2va"],
            "resolutions": ["480P", "768P"],
            "duration_seconds": {"min": 5, "max": 15},
            "supported_aspect_ratios": ["16:9", "9:16"],
        },
    }


MODEL_CAPABILITIES = load_model_capabilities()


@dataclass
class RouteDecision:
    model: ModelType
    generation_mode: str
    output_profile: OutputProfile
    workflow_profile: WorkflowProfile
    medium: MediumType
    is_valid: bool
    error_message: str | None = None
    suggested_correction: str | None = None
    loaded_references: list[str] = field(default_factory=list)


def normalize_mode(mode: str) -> str:
    """Normalize user input or legacy mode name to canonical form (t2va, i2va, fl2va, l2va, ref2va)."""
    clean = mode.lower().replace("_", "-").strip()
    return MODE_ALIASES.get(clean, clean)


def load_provider_capabilities() -> dict[str, Any]:
    """Load config/provider-capabilities.json (provider-layer fact source)."""
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config",
        "provider-capabilities.json",
    )
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"records": []}


PROVIDER_CAPABILITIES = load_provider_capabilities()


def provider_lookup(canonical_model: str, provider: str, mode: str) -> dict[str, Any] | None:
    """Return the provider-layer record for (model, provider, mode), if any."""
    for rec in PROVIDER_CAPABILITIES.get("records", []):
        if (
            rec.get("canonical_model") == canonical_model
            and rec.get("provider") == provider
            and rec.get("mode") == normalize_mode(mode)
        ):
            return rec
    return None


def validate_v2_api_call(
    model: str,
    mode: str,
    assets: dict[str, Any] | None = None,
) -> tuple[bool, str | None, str | None]:
    """Fail-closed V2 public-API adapter check.

    Uses provider-layer records only: Turbo (not listed) and H3-Max Ref2VA
    (not supported on public V2) are rejected here even if the internal
    routing matrix knows the canonical mode shape.
    """
    norm_mode = normalize_mode(mode)
    rec = provider_lookup(model, "api_v2", norm_mode)
    if rec is None:
        return (
            False,
            f"公开 V2 无 {model} + {norm_mode} 的能力记录，拒绝构造 API 调用",
            "请检查 config/provider-capabilities.json 是否已校准",
        )
    status = rec.get("status")
    if status == "NOT_LISTED_IN_PUBLIC_API":
        return (
            False,
            f"公开 V2 未列出 {model}（mode={norm_mode}），不可擅自构造 model ID 发起调用",
            "请改用 V2 已列出的 MiniMax-H3 / MiniMax-H3-Max，或走 official_agent 另行取证",
        )
    if status == "NOT_SUPPORTED_IN_PUBLIC_API":
        return (
            False,
            f"公开 V2 不支持 {model} + {norm_mode}，拒绝构造 API 调用",
            "请改用 H3，或等待公开证据后再放行",
        )
    if status != "SUPPORTED":
        return (
            False,
            f"公开 V2 上 {model} + {norm_mode} 状态为 {status}，拒绝构造 API 调用",
            "请补充公开证据后再调用",
        )
    if norm_mode == "ref2va" and assets is not None:
        has_ref = any(assets.get(k) for k in ("reference_image", "reference_video", "reference_audio", "image", "video"))
        has_frame = any(assets.get(k) for k in ("first_frame", "last_frame"))
        if not has_ref:
            return (False, "V2 ref2va 调用必须携带参考素材（reference_image/video/audio）", "请补充参考素材")
        if has_frame:
            return (False, "V2 参考素材与首尾帧互斥，ref2va 调用不得混入 first_frame/last_frame", "请移除首尾帧素材或改用 fl2va")
    return True, None, None


def validate_model_mode_combination(
    model: str,
    mode: str,
    aspect_ratio: str = "16:9",
    resolution: str = "768P",
    duration_s: int = 6,
    assets: dict[str, Any] | None = None,
) -> tuple[bool, str | None, str | None]:
    """Validate model and generation mode combinations to block unsupported settings."""
    if model not in MODEL_CAPABILITIES:
        return (
            False,
            f"未知的 MiniMax 模型型号 '{model}'。可选型号: {list(MODEL_CAPABILITIES.keys())}",
            "请选用 'H3', 'H3-Max' 或 'H3-Max-Turbo'",
        )

    cap = MODEL_CAPABILITIES[model]
    norm_mode = normalize_mode(mode)

    # Determine supported modes by checking mode-level status if present, else fallback to modes list
    modes_dict = cap.get("modes", {})
    if isinstance(modes_dict, dict):
        if norm_mode not in modes_dict:
            return (
                False,
                f"模型型号 '{model}' 不支持生成模式 '{mode}' (规范模式: '{norm_mode}')",
                "建议将模型切换至 'H3' 或选用已知模式",
            )
        mode_info = modes_dict[norm_mode]
        mode_status = mode_info.get("status", "UNVERIFIED") if isinstance(mode_info, dict) else "UNVERIFIED"
        if mode_status not in ("VERIFIED_OFFICIAL", "CONFIRMED_COMMUNITY"):
            return (
                False,
                f"模型型号 '{model}' 对模式 '{mode}' 未经官方证实或不支持 (规范模式: '{norm_mode}', 状态: {mode_status})",
                "建议将模型切换至 'H3' 或选用官方支持模式",
            )
    else:
        supported_modes = set(cap.get("modes", []))
        if norm_mode not in supported_modes:
            return (
                False,
                f"模型型号 '{model}' 不支持生成模式 '{mode}' (规范模式: '{norm_mode}', 支持模式: {sorted(list(supported_modes))})",
                f"建议将模型切换至 'H3' 或将模式调整为 '{sorted(list(supported_modes))[0]}'",
            )

    # Resolution check (exact upper-case match against capability)
    res_upper = resolution.upper()
    valid_res = set(cap.get("resolutions", []))
    if res_upper not in valid_res:
        return (
            False,
            f"模型型号 '{model}' 不支持分辨率 '{resolution}'（官方支持: {sorted(list(valid_res))}）",
            f"建议使用: {sorted(list(valid_res))}",
        )

    # Duration limits check
    min_dur = cap["duration_seconds"]["min"]
    max_dur = cap["duration_seconds"]["max"]
    if duration_s < min_dur or duration_s > max_dur:
        return (
            False,
            f"请求时长 {duration_s} 秒超出模型 '{model}' 的合法范围 ({min_dur}-{max_dur} 秒)",
            f"建议将时长设定在 {min_dur} 至 {max_dur} 秒之间",
        )

    # Aspect ratio check (fl2va/l2va inherit ratio from picture, exempt from strict aspect check if needed)
    valid_ratios = set(cap.get("supported_aspect_ratios", []))
    if norm_mode not in ("fl2va", "l2va") and aspect_ratio not in valid_ratios:
        return (
            False,
            f"模型型号 '{model}' 不支持画幅 '{aspect_ratio}'（支持: {sorted(list(valid_ratios))}）",
            "建议使用 16:9 或 9:16",
        )

    # Asset condition checks (strict constraints for each mode)
    def _asset_present(val: Any) -> bool:
        if isinstance(val, (int, float)):
            return val > 0
        if isinstance(val, (str, bytes, list, dict)):
            return len(val) > 0
        return val is not None and bool(val)

    if assets is not None:
        if norm_mode == "i2va":
            # Supports first_frame or legacy image
            has_first = _asset_present(assets.get("first_frame")) or _asset_present(assets.get("image"))
            if not has_first:
                return (
                    False,
                    "I2VA (图生视频) 模式必须提供起始帧/参考图素材 (first_frame 或 image >= 1)",
                    "请传入输入图片或将生成模式切换为 T2VA",
                )
        elif norm_mode == "fl2va":
            has_first = _asset_present(assets.get("first_frame")) or _asset_present(assets.get("image"))
            has_last = _asset_present(assets.get("last_frame"))
            if not has_first and not has_last:
                return (
                    False,
                    "FL2VA (首尾帧) 模式必须同时提供首帧 (first_frame) 与尾帧 (last_frame) 素材",
                    "请补充首帧与尾帧素材，或选用其他生成模式",
                )
            if not has_first:
                return (
                    False,
                    "FL2VA (首尾帧) 模式缺失首帧素材 (first_frame)",
                    "请补充首帧素材，或切换为仅尾帧 (L2VA) 模式",
                )
            if not has_last:
                return (
                    False,
                    "FL2VA (首尾帧) 模式缺失尾帧素材 (last_frame)",
                    "请补充尾帧素材，或切换为单图生视频 (I2VA) 模式",
                )
        elif norm_mode == "l2va":
            if not _asset_present(assets.get("last_frame")):
                return (
                    False,
                    "L2VA (仅尾帧) 模式必须提供尾帧素材 (last_frame >= 1)",
                    "请提供结束帧/尾帧图片素材，或切换为 T2VA / I2VA 模式",
                )
        elif norm_mode == "ref2va":
            has_img = _asset_present(assets.get("image")) or _asset_present(assets.get("first_frame"))
            has_vid = _asset_present(assets.get("video"))
            if not (has_img or has_vid):
                return (
                    False,
                    "Ref2VA 多模态参考模式必须至少提供图片或视频素材，音频不可作为唯一素材",
                    "请补充参考图片或视频素材",
                )

    return True, None, None


def resolve_reference_plan(profile: str, user_text: str = "") -> list[str]:
    """Resolve which reference markdown cards must be loaded on demand."""
    loaded = []

    if profile == "ref2va-en":
        loaded.append("references/official/ref-en.txt")
        loaded.append("references/official-h3-grammar.md")
        if any(k in user_text for k in ["picture", "image", "asset", "ref", "subject"]):
            loaded.append("references/ref2va-guide.md")
        return loaded

    if profile == "native-general":
        loaded.append("references/official/base-en.txt")
        loaded.append("references/native/general-h3.md")
        return loaded

    # native-drama-series
    loaded.append("references/official-native-chinese-h3.md")
    loaded.append("references/native/drama-series-h3.md")

    # On-demand modular craft cards
    if any(k in user_text for k in ["打斗", "动作", "挥剑", "拳", "跑", "跳", "撞", "飞驰", "冲", "拔刀", "击中"]):
        loaded.append("references/craft/action-physics.md")

    if any(k in user_text for k in ["对白", "台词", "说话", "声音", "音效", "呼唤", "叹息", "喊", "音"]):
        loaded.append("references/craft/dialogue-audio.md")
        loaded.append("references/audio-mix-hierarchy.md")

    if any(k in user_text for k in ["续写", "跨镜", "多镜头", "连戏", "分镜表", "宫格", "第二镜", "接戏"]):
        loaded.append("references/craft/continuation.md")
        loaded.append("references/continuation-bridging.md")

    if any(k in user_text for k in ["文字", "标题", "包装", "字幕", "排版", "logo", "字"]):
        loaded.append("references/typography-packaging.md")

    if any(k in user_text for k in ["镜头", "运镜", "景别", "推镜头", "俯冲", "特写", "反打"]):
        loaded.append("references/craft/camera-editing.md")

    return loaded


def route_request(
    user_prompt: str,
    target_model: str = "H3",
    forced_mode: str | None = None,
    forced_profile: str | None = None,
    forced_workflow: str | None = None,
    aspect_ratio: str = "16:9",
    resolution: str = "768P",
    duration_s: int = 6,
    assets: dict[str, Any] | None = None,
) -> RouteDecision:
    """Five-dimensional production routing engine with negative intention handling."""
    text_lower = user_prompt.lower()

    # 1. Model resolution
    model: ModelType = "H3"
    if "h3-max-turbo" in text_lower or target_model == "H3-Max-Turbo":
        model = "H3-Max-Turbo"
    elif "h3-max" in text_lower or target_model == "H3-Max":
        model = "H3-Max"

    # 2. Generation Mode resolution (t2va / i2va / fl2va / l2va / ref2va)
    if forced_mode:
        mode = normalize_mode(forced_mode)
    elif any(k in text_lower for k in ["首尾帧", "第一帧和最后一帧", "起始帧和结束帧", "fl2va", "fl2v", "首末帧"]):
        mode = "fl2va"
    elif any(k in text_lower for k in ["仅尾帧", "尾帧生成", "结束帧生视频", "最后一帧", "结束帧", "l2va", "l2v"]):
        mode = "l2va"
    elif any(k in text_lower for k in ["参考图", "以图生视频", "图生视频", "i2v", "i2va", "垫图", "首帧"]):
        mode = "i2va"
    elif any(k in text_lower for k in ["多模态", "参考资产", "角色图", "ref2va"]):
        mode = "ref2va"
    else:
        mode = "t2va"

    # 3. Output Profile resolution (with negative routing)
    has_negative_en = any(k in text_lower for k in [
        "不要comfyui", "不用comfyui", "别用comfyui", "不要英文", "不用英文", "非英文",
        "不要给我英文", "别给我英文", "不用给我英文", "不要用英文", "别用英文",
        "no comfyui", "not comfyui", "without comfyui", "no english", "not english"
    ]) or ("不要" in text_lower and "英文" in text_lower) or ("不用" in text_lower and "英文" in text_lower)
    has_explicit_en = any(k in text_lower for k in [
        "ref2va", "comfyui", "英文提示词", "纯英文", "english", "--english", "海外评测"
    ]) or (re.search(r"[a-zA-Z]{4,}", user_prompt) and not re.search(r"[\u4e00-\u9fff]", user_prompt))

    if forced_profile:
        output_profile = forced_profile
    elif has_explicit_en and not has_negative_en:
        output_profile = "ref2va-en"
    else:
        # Check if drama-series or general
        is_general = any(k in user_prompt for k in [
            "产品展示", "手机产品", "旋转特写", "产品", "广告", "风景", "空镜", "特效概念", "概念特效"
        ])
        is_drama = (
            forced_workflow == "drama-series"
            or any(k in user_prompt for k in ["短剧", "网剧", "对白", "文戏", "台词", "分镜组", "角色签名", "剧集", "打斗", "破庙"])
            or not is_general
        )
        output_profile = "native-drama-series" if (is_drama and not is_general) else "native-general"

    # 4. Workflow Profile
    workflow: WorkflowProfile = forced_workflow if forced_workflow else ("drama-series" if output_profile == "native-drama-series" else "general")

    # 5. Medium resolution
    is_animation = any(k in user_prompt for k in ["动画", "3d", "国漫", "二次元", "动漫", "cg", "赛璐璐", "水墨"])
    medium: MediumType = "animation" if is_animation else "live-action"

    # 6. Resolve loaded references
    loaded = resolve_reference_plan(output_profile, user_prompt)

    # 7. Model & Mode Capability Validation (Block invalid combos)
    valid, err_msg, sugg = validate_model_mode_combination(
        model=model,
        mode=mode,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        duration_s=duration_s,
        assets=assets,
    )
    # On the valid path validate_model_mode_combination returns (True, None, None),
    # so the fields below match the previous separate success/failure returns exactly.
    return RouteDecision(
        model=model,
        generation_mode=mode,
        output_profile=output_profile,
        workflow_profile=workflow,
        medium=medium,
        is_valid=valid,
        error_message=err_msg,
        suggested_correction=sugg,
        loaded_references=loaded,
    )
