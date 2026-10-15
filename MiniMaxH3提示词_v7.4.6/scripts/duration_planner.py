#!/usr/bin/env python3
"""Beat packing into 4—10 second clips for MiniMax H3 Craft.

One clip is one prompt group. The packer turns an ordered sequence of atomic
script beats into exactly as many clips as the content needs — never a fixed
count, never fewer than the script target.

Adjudications frozen by this module (design 设计方案 §6—§7; the work order
records the full reasoning):

1. Sequential only. Beats keep document order; a clip never reorders, drops or
   duplicates content, and only a beat marked ``splittable`` may be divided
   across clips.
2. Scene compatibility. A clip never mixes two ``scene_id`` values, so a scene
   change is always a clip boundary — even when the split leaves a group short.
3. Every clip is allocated whole integer seconds, always rounded up, so a plan
   never understates content. Silent understatement is the defect this whole
   work order exists to fix.
4. ``kind="dialogue"`` is speech time and ``kind="action"`` (the default,
   简单动作) may run concurrently with speech, so a clip holding both costs
   ``max(speech, simple action)`` instead of their sum. Every other kind is
   added in full: a kind this module does not recognise is never allowed to
   compress the plan.
5. 最多两名说话人、最多三轮短对白 per clip: exceeding a cap closes the clip. When that
   boundary would leave a clip below the minimum, ``_repair_short_groups`` merges it
   back — the minimum wins at the plan level, because a violating short clip is a
   blocking defect while an extra dialogue turn is not.
6. Long dialogue is cut only at punctuation pauses and the cut text is kept
   verbatim, so concatenating ``ClipDraft.dialogue_text`` reproduces the script
   dialogue character for character — 长台词不删词、不机械加速。
7. A clip below the minimum is repaired by merging or by re-dividing time with a
   neighbouring clip of the same scene — never by compressing a beat. If no
   legal move exists the packer blocks instead of guessing.
8. Anything the packer cannot plan is refused with ``DurationPlanError`` (or a
   subclass) carrying a ``*_`` code: an unplannable script is a question for the
   user, not a number to invent.

This module consumes exactly two upstream sources: the ``clip_seconds`` bounds
of ``load_duration_policy()`` and the beats an upstream parser derived from
:func:`script_duration.parse_script_duration`. It defines no duration defaults
of its own; dialogue timing comes from ``Beat.estimated_seconds`` so that
``speech_chars_per_second`` stays single-sourced upstream.
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

UNPACKABLE_BEATS = "UNPACKABLE_BEATS"
INFEASIBLE_CLIP_RANGE = "INFEASIBLE_CLIP_RANGE"
INVALID_PLAN_INPUT = "INVALID_PLAN_INPUT"

KIND_DIALOGUE = "dialogue"
KIND_ACTION = "action"

# 设计 §6.1：单个片段最多两名说话人、两到三轮短对白。
_MAX_SPEAKERS = 2
_MAX_DIALOGUE_TURNS = 3

# 自然停顿：只在这里切台词。连续的停顿符（``……``）与紧随其后的收尾引号/括号
# 一起跟随停顿，绝不从中间切开。
_PAUSE_CHARS = frozenset("，。；！？：…、,.!?;:—")
_CLOSER_CHARS = frozenset("」』）)】’”\"'")

# 单个节拍允许拆出的片段数上限。一个节拍需要上千段，说明上游估时算错了（例如把毫秒当秒），
# 而按这个数字分配内存会先耗尽内存再抛出非家族异常；这里按「1 小时 ÷ 4 秒」给出宽松上限。
_MAX_PIECES_PER_BEAT = 1000

_STRATEGY_BALANCE = "balance"
_STRATEGY_FILL = "fill"
_STRATEGY_LEAD = "lead"

# 长台词切分顺序：先均衡，再塞满上限，最后用「刚过下限」的小片领跑。三档都失败才阻断。
_SPLIT_STRATEGIES = (_STRATEGY_BALANCE, _STRATEGY_FILL, _STRATEGY_LEAD)

_CLIP_ID_PREFIX = "G"
_CLIP_ID_DIGITS = 3
_EPSILON = 1e-9


class DurationPlanError(RuntimeError):
    """Raised when a beat sequence cannot be planned into legal clips."""


class InfeasibleClipRangeError(DurationPlanError):
    """Raised when the effective clip range is empty or malformed."""


class UnpackableBeatsError(DurationPlanError):
    """Raised when no legal plan exists without inventing or deleting content."""


@dataclass(frozen=True)
class Beat:
    """One atomic script beat: a line of dialogue or one action step."""

    id: str
    scene_id: str
    estimated_seconds: float
    text: str
    kind: str = KIND_ACTION
    splittable: bool = False
    speaker_ids: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ClipDraft:
    """One planned clip: the prompt group the renderer will expand."""

    id: str
    duration_seconds: int
    beat_ids: Tuple[str, ...]
    dialogue_text: str = ""


@dataclass
class _Atom:
    """A packable unit: a whole beat, or one declared piece of a split beat."""

    beat_id: str
    scene_id: str
    seconds: float
    kind: str
    text: str
    splittable: bool
    speaker_ids: Tuple[str, ...]


@dataclass
class _Group:
    """A clip under construction."""

    scene_id: str
    atoms: List["_Atom"] = field(default_factory=list)

    @property
    def seconds(self) -> float:
        return _atoms_seconds(self.atoms)

    @property
    def clipped_seconds(self) -> int:
        return _ceil_seconds(self.seconds)


def audible_char_count(text: str) -> int:
    """Count the characters that take speaking time.

    Punctuation, separators and whitespace cost no character slot: the
    3—4 汉字/秒 speech band already includes normal pausing (设计 §6.1), and the
    script's own commas must not be billed as syllables. Anything else — digits
    and latin letters included — counts, so the estimate errs high, never low.
    """
    if not isinstance(text, str):
        raise DurationPlanError(
            f"{INVALID_PLAN_INPUT}: 台词必须是字符串，得到 {type(text).__name__}"
        )
    return sum(1 for char in text if unicodedata.category(char)[0] not in "PZC")


def estimate_dialogue_seconds(text: str, cps: float) -> float:
    """Seconds needed to speak ``text`` at ``cps`` audible characters per second.

    ``cps`` is passed in by the caller (upstream uses the policy's
    ``speech_chars_per_second`` band); this module holds no default rate.
    """
    if isinstance(cps, bool) or not isinstance(cps, (int, float)):
        raise DurationPlanError(
            f"{INVALID_PLAN_INPUT}: 语速必须是数字（字/秒），得到 {cps!r}"
        )
    if not math.isfinite(cps) or cps <= 0:
        raise DurationPlanError(
            f"{INVALID_PLAN_INPUT}: 语速必须为正的有限值（字/秒），得到 {cps!r}"
        )
    return audible_char_count(text) / cps


def effective_clip_range(model_min: int, model_max: int, policy: dict) -> Tuple[int, int]:
    """Intersect the requested clip range with the policy's clip bounds.

    Both sources are validated here rather than only inside :func:`pack_beats`,
    because the result is a published number (``duration-manifest.json`` 的
    ``clip_range_seconds``): a malformed policy must raise
    :class:`InfeasibleClipRangeError` instead of leaking ``KeyError``/``TypeError``,
    and an empty intersection must raise instead of returning an inverted tuple
    that a ledger would happily print as a valid range.
    """
    for name, value in (("model_min", model_min), ("model_max", model_max)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise InfeasibleClipRangeError(
                f"{INFEASIBLE_CLIP_RANGE}: {name} 必须是正整数秒，得到 {value!r}"
            )
    if model_min > model_max:
        raise InfeasibleClipRangeError(
            f"{INFEASIBLE_CLIP_RANGE}: 模型能力范围下限 {model_min}s 大于上限 {model_max}s"
        )
    policy_low, policy_high = _clip_bounds(policy)
    low = max(model_min, policy_low)
    high = min(model_max, policy_high)
    if low > high:
        raise InfeasibleClipRangeError(
            f"{INFEASIBLE_CLIP_RANGE}: 模型能力范围 {model_min}—{model_max}s 与时长政策 "
            f"{policy_low}—{policy_high}s 没有交集，无法规划任何片段"
        )
    return low, high


def pack_beats(beats, model_min: int, model_max: int, policy: dict) -> List[ClipDraft]:
    """Pack ordered beats into clips of the effective duration range.

    Args:
        beats: Iterable of :class:`Beat`, in script order.
        model_min: Smallest clip the target model accepts, in seconds.
        model_max: Largest clip the target model accepts, in seconds.
        policy: Duration policy dict; consumed for ``clip_seconds`` only.

    Returns:
        list[ClipDraft]: Clips in script order, ids ``G001`` upward. No beats
        means no clips.

    Raises:
        InfeasibleClipRangeError: If the effective clip range is empty or the
            policy/model inputs are malformed.
        UnpackableBeatsError: If no legal plan keeps every clip inside the range
            without compressing, deleting, inventing or re-scening content.
        DurationPlanError: If the beats themselves are malformed.
    """
    low, high = _resolve_range(model_min, model_max, policy)
    beat_list = _validate_beats(beats)
    if not beat_list:
        return []

    # Long dialogue can be cut at any of its pause runs, so which boundary the
    # splitter picks decides whether the neighbours can still balance out. Walk the
    # cutting styles in turn and only then block: a refusal has to mean "no legal
    # plan", not "our first guess stranded a tail".
    first_failure: Optional[UnpackableBeatsError] = None
    for strategy in _SPLIT_STRATEGIES:
        try:
            atoms: List[_Atom] = []
            for beat in beat_list:
                atoms.extend(_beat_atoms(beat, low, high, strategy))
            groups = _greedy_groups(atoms, high)
            _repair_short_groups(groups, low, high)
            return [_to_clip(group, index) for index, group in enumerate(groups, start=1)]
        except UnpackableBeatsError as exc:
            if first_failure is None:
                first_failure = exc
    raise first_failure


def _resolve_range(model_min: int, model_max: int, policy: dict) -> Tuple[int, int]:
    """Return the effective ``(low, high)`` for planning.

    Validation lives in :func:`effective_clip_range` so the range a plan uses and
    the range a manifest publishes can never diverge.
    """
    return effective_clip_range(model_min, model_max, policy)


def _clip_bounds(policy) -> Tuple[int, int]:
    if not isinstance(policy, dict):
        raise InfeasibleClipRangeError(
            f"{INFEASIBLE_CLIP_RANGE}: policy 必须是 dict，得到 {type(policy).__name__}"
        )
    clip = policy.get("clip_seconds")
    if not isinstance(clip, dict):
        raise InfeasibleClipRangeError(
            f'{INFEASIBLE_CLIP_RANGE}: policy["clip_seconds"] 缺失或不是对象'
            '（应形如 {"min": 4, "max": 10}）'
        )
    if set(clip) != {"min", "max"}:
        raise InfeasibleClipRangeError(
            f'{INFEASIBLE_CLIP_RANGE}: policy["clip_seconds"] 的键必须恰好是 min/max，'
            f"得到 {sorted(clip)}"
        )
    low, high = clip["min"], clip["max"]
    for name, value in (("min", low), ("max", high)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise InfeasibleClipRangeError(
                f'{INFEASIBLE_CLIP_RANGE}: policy["clip_seconds"]["{name}"] 必须是整数秒，'
                f"得到 {value!r}"
            )
    if low <= 0 or high <= 0:
        raise InfeasibleClipRangeError(
            f"{INFEASIBLE_CLIP_RANGE}: 片段范围必须为正秒，得到 {low}—{high}"
        )
    if low > high:
        raise InfeasibleClipRangeError(
            f"{INFEASIBLE_CLIP_RANGE}: 片段下限 {low}s 大于上限 {high}s"
        )
    return low, high


def _validate_beats(beats) -> List[Beat]:
    """Return the beats as a list, refusing anything the packer cannot trust."""
    if not isinstance(beats, (tuple, list)) and not hasattr(beats, "__next__"):
        raise DurationPlanError(
            f"{INVALID_PLAN_INPUT}: beats 必须是按剧本顺序的序列或迭代器（tuple/list/生成器），"
            f"得到 {type(beats).__name__}；无序集合无法保证剧本顺序"
        )
    validated: List[Beat] = []
    seen = set()
    for index, beat in enumerate(beats):
        if not isinstance(beat, Beat):
            raise DurationPlanError(
                f"{INVALID_PLAN_INPUT}: beats[{index}] 必须是 Beat，得到 {type(beat).__name__}"
            )
        for name, value in (("id", beat.id), ("scene_id", beat.scene_id), ("text", beat.text)):
            if not isinstance(value, str):
                raise DurationPlanError(
                    f"{INVALID_PLAN_INPUT}: 节拍 {index} 的 {name} 必须是字符串，"
                    f"得到 {type(value).__name__}"
                )
        if not beat.id:
            raise DurationPlanError(f"{INVALID_PLAN_INPUT}: 节拍 {index} 的 id 不能为空")
        if not beat.scene_id:
            raise DurationPlanError(f"{INVALID_PLAN_INPUT}: 节拍 {beat.id!r} 的 scene_id 不能为空")
        if beat.id in seen:
            raise DurationPlanError(
                f"{INVALID_PLAN_INPUT}: 节拍ID {beat.id!r} 重复，来源映射会被破坏"
            )
        seen.add(beat.id)
        if not isinstance(beat.kind, str) or not beat.kind:
            raise DurationPlanError(
                f"{INVALID_PLAN_INPUT}: 节拍 {beat.id!r} 的 kind 必须是非空字符串，"
                f"得到 {beat.kind!r}"
            )
        if not isinstance(beat.splittable, bool):
            raise DurationPlanError(
                f"{INVALID_PLAN_INPUT}: 节拍 {beat.id!r} 的 splittable 必须是布尔值，"
                f"得到 {beat.splittable!r}"
            )
        seconds = beat.estimated_seconds
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise DurationPlanError(
                f"{INVALID_PLAN_INPUT}: 节拍 {beat.id!r} 的 estimated_seconds 必须是数字，"
                f"得到 {seconds!r}"
            )
        if not math.isfinite(seconds) or seconds <= 0:
            raise DurationPlanError(
                f"{INVALID_PLAN_INPUT}: 节拍 {beat.id!r} 的 estimated_seconds 必须为正的有限值，"
                f"得到 {seconds!r}"
            )
        if not isinstance(beat.speaker_ids, (tuple, list)):
            raise DurationPlanError(
                f"{INVALID_PLAN_INPUT}: 节拍 {beat.id!r} 的 speaker_ids 必须是说话人ID序列"
                f"（tuple/list），得到 {type(beat.speaker_ids).__name__}"
            )
        speakers = tuple(beat.speaker_ids)
        for speaker in speakers:
            if not isinstance(speaker, str) or not speaker:
                raise DurationPlanError(
                    f"{INVALID_PLAN_INPUT}: 节拍 {beat.id!r} 的说话人ID必须是非空字符串，"
                    f"得到 {speaker!r}"
                )
        validated.append(
            Beat(
                id=beat.id,
                scene_id=beat.scene_id,
                estimated_seconds=float(seconds),
                text=beat.text,
                kind=beat.kind,
                splittable=beat.splittable,
                speaker_ids=speakers,
            )
        )
    return validated


def _fewest_pieces(beat: Beat, total: float, high: int) -> int:
    """Smallest piece count that keeps every piece within the cap, or refuse.

    Refuses absurd counts before any list is allocated, so a mis-computed upstream
    duration (``1e12`` 秒) becomes a family exception instead of ``MemoryError``.
    """
    fewest = max(1, math.ceil(total / high - _EPSILON))
    if fewest > _MAX_PIECES_PER_BEAT:
        raise UnpackableBeatsError(
            f"{UNPACKABLE_BEATS}: 节拍 {beat.id!r} 声明时长 {total:g}s 需要拆成 {fewest} 段"
            f"（每段不超过 {high}s，单节拍上限 {_MAX_PIECES_PER_BEAT} 段）："
            "声明时长极可能算错，请核对上游估时或拆分该节拍后重试"
        )
    return fewest


def _beat_atoms(beat: Beat, low: int, high: int, strategy: str = _STRATEGY_BALANCE) -> List[_Atom]:
    """Return the packable atoms for one beat, splitting it only when it must."""
    total = float(beat.estimated_seconds)
    if _ceil_seconds(total) <= high:
        return [_atom(beat, total, beat.text if beat.kind == KIND_DIALOGUE else "")]
    if not beat.splittable:
        raise UnpackableBeatsError(
            f"{UNPACKABLE_BEATS}: 节拍 {beat.id!r} 时长 {total:g}s 超过上限 {high}s 且未标记 "
            "splittable；复杂动作不可自动压缩，只能按上游声明的自然边界拆分。"
            "请标记 splittable 或改写该节拍"
        )
    if beat.kind == KIND_DIALOGUE:
        pieces = _split_dialogue(beat, low, high, strategy)
    else:
        pieces = _slice_by_seconds(beat, high)
    return [_atom(beat, seconds, text) for text, seconds in pieces]


def _atom(beat: Beat, seconds: float, text: str) -> _Atom:
    return _Atom(
        beat_id=beat.id,
        scene_id=beat.scene_id,
        seconds=seconds,
        kind=beat.kind,
        text=text,
        splittable=beat.splittable,
        speaker_ids=beat.speaker_ids,
    )


def _split_dialogue(beat: Beat, low: int, high: int, strategy: str = _STRATEGY_BALANCE) -> List[Tuple[str, float]]:
    """Cut long dialogue at natural pauses, keeping every character.

    Only the cap is enforced on the cut itself: a cut piece may fall below the
    minimum because it packs together with its neighbours (设计 §7.4 管的是片段，
    不是切出来的半句)。碎片永远由 ``_repair_short_groups`` 在下限上兜住。

    ``strategy`` picks the cutting style (see ``_SPLIT_STRATEGIES``): balance the
    pieces, pack them up to the cap, or lead with pieces just past the minimum.
    Which style the surrounding packing can absorb depends on the neighbours, so
    :func:`pack_beats` walks them all before refusing.
    """
    text = beat.text
    total = float(beat.estimated_seconds)
    audibles = audible_char_count(text)
    if audibles <= 0:
        raise UnpackableBeatsError(
            f"{UNPACKABLE_BEATS}: 节拍 {beat.id!r} 声明 {total:g}s 但台词没有可发音字符，"
            "无法在自然停顿处拆分；请修正文本或声明时长"
        )

    def seconds_of(piece: str) -> float:
        return audible_char_count(piece) * total / audibles

    fewest = _fewest_pieces(beat, total, high)
    cuts = _pause_cut_positions(text)
    if fewest > len(cuts):
        raise UnpackableBeatsError(
            f"{UNPACKABLE_BEATS}: 节拍 {beat.id!r} 台词 {total:g}s 拆成每段不超过 {high}s "
            f"至少需要 {fewest} 处自然停顿切口，而文本只提供 {len(cuts)} 处；"
            "请在长句中间补标点停顿，或调整片段范围"
        )
    for piece_count in range(fewest, len(cuts) + 1):
        pieces = _try_dialogue_split(text, total, seconds_of, cuts, piece_count, low, high, strategy)
        if pieces is not None:
            return pieces
    raise UnpackableBeatsError(
        f"{UNPACKABLE_BEATS}: 节拍 {beat.id!r} 无法只在自然停顿（标点）处拆成"
        f" {fewest}—{len(cuts)} 段且每段都不超过 {high}s；"
        "请在长句中间补标点停顿，或调整片段范围"
    )


def _try_dialogue_split(text, total, seconds_of, cuts, piece_count, low, high, strategy):
    """Cut one dialogue piece count; return ``None`` when this count cannot work.

    Each style aims the next cut at a different length: ``balance`` the even share of
    what is left, ``fill`` the cap (one full clip per cut), ``lead`` just past the
    minimum (many small pieces the packing can top up).
    """
    pieces: List[Tuple[str, float]] = []
    start = 0
    consumed = 0.0
    for remaining in range(piece_count, 1, -1):
        if strategy == _STRATEGY_FILL:
            target = consumed + high
        elif strategy == _STRATEGY_LEAD:
            target = consumed + low
        else:
            target = consumed + (total - consumed) / remaining
        best: Optional[Tuple[float, int, float]] = None
        for cut in cuts:
            if cut <= start:
                continue
            piece_seconds = seconds_of(text[start:cut])
            if _ceil_seconds(piece_seconds) > high:
                break
            rest_seconds = total - consumed - piece_seconds
            rest_count = remaining - 1
            if _ceil_seconds(rest_seconds) > rest_count * high:
                continue
            candidate = (abs(piece_seconds - target), cut, piece_seconds)
            if best is None or candidate[0] < best[0]:
                best = candidate
        if best is None:
            return None
        cut = best[1]
        pieces.append((text[start:cut], best[2]))
        start = cut
        consumed += best[2]
    final_seconds = seconds_of(text[start:])
    if _ceil_seconds(final_seconds) > high:
        return None
    pieces.append((text[start:], final_seconds))
    return pieces


def _pause_cut_positions(text: str) -> List[int]:
    """Positions a line may be cut at: after a whole pause run, closers included."""
    positions: List[int] = []
    index = 0
    length = len(text)
    while index < length:
        if text[index] in _PAUSE_CHARS:
            end = index + 1
            while end < length and (text[end] in _PAUSE_CHARS or text[end] in _CLOSER_CHARS):
                end += 1
            positions.append(end)
            index = end
        else:
            index += 1
    if not positions or positions[-1] != length:
        positions.append(length)
    return positions


def _slice_by_seconds(beat: Beat, high: int) -> List[Tuple[str, float]]:
    """Divide a splittable action beat at the boundaries upstream declared.

    The cap alone decides the piece count; short pieces are legal here because
    they pack together with their neighbours (设计 §7.4 约束的是片段)。
    """
    total = float(beat.estimated_seconds)
    fewest = _fewest_pieces(beat, total, high)
    return [("", total / fewest)] * fewest


def _atoms_seconds(atoms: Sequence[_Atom]) -> float:
    """Clip cost: speech and simple action overlap; everything else adds up."""
    speech = math.fsum(atom.seconds for atom in atoms if atom.kind == KIND_DIALOGUE)
    overlap = math.fsum(atom.seconds for atom in atoms if atom.kind == KIND_ACTION)
    extra = math.fsum(
        atom.seconds for atom in atoms if atom.kind not in (KIND_DIALOGUE, KIND_ACTION)
    )
    return max(speech, overlap) + extra


def _ceil_seconds(seconds: float) -> int:
    """Allocate whole seconds upward, so a plan never understates its content."""
    return math.ceil(seconds - _EPSILON)


def _speakers(atoms: Sequence[_Atom]) -> List[str]:
    ordered: List[str] = []
    for atom in atoms:
        for speaker in atom.speaker_ids:
            if speaker not in ordered:
                ordered.append(speaker)
    return ordered


def _dialogue_turns(atoms: Sequence[_Atom]) -> int:
    return sum(1 for atom in atoms if atom.kind == KIND_DIALOGUE)


def _caps_violated(atoms: Sequence[_Atom]) -> bool:
    return (
        _dialogue_turns(atoms) > _MAX_DIALOGUE_TURNS
        or len(_speakers(atoms)) > _MAX_SPEAKERS
    )


def _greedy_groups(atoms: Sequence[_Atom], high: int) -> List[_Group]:
    groups: List[_Group] = []
    for atom in atoms:
        if groups and _can_accept(groups[-1], atom, high):
            groups[-1].atoms.append(atom)
            continue
        groups.append(_Group(scene_id=atom.scene_id, atoms=[atom]))
    return groups


def _can_accept(group: _Group, atom: _Atom, high: int) -> bool:
    if group.scene_id != atom.scene_id:
        return False
    candidate = group.atoms + [atom]
    if _ceil_seconds(_atoms_seconds(candidate)) > high:
        return False
    return not _caps_violated(candidate)


def _repair_short_groups(groups: List[_Group], low: int, high: int) -> None:
    """Make every group reach the minimum, or block when no legal move exists.

    Terminates by construction: every successful repair either removes a group
    (a merge) or lifts the short group above the minimum without dropping any other
    group below it (``_cut_atom`` keeps the donor's side at or above ``low``), so the
    number of short groups strictly decreases. The loop therefore needs no iteration
    cap — a cap would be unreachable code — and a cell that cannot be repaired raises
    instead of spinning.
    """
    index = 0
    while index < len(groups):
        if groups[index].clipped_seconds >= low:
            index += 1
            continue
        if not _repair_once(groups, index, low, high):
            raise UnpackableBeatsError(_short_group_message(groups, index, low, high))
        index = max(index - 1, 0)


def _repair_once(groups: List[_Group], index: int, low: int, high: int) -> bool:
    group = groups[index]
    for left, right in ((index - 1, index), (index, index + 1)):
        if left < 0 or right >= len(groups):
            continue
        merged = _merge_groups(groups[left], groups[right], high)
        if merged is not None:
            groups[left : right + 1] = [merged]
            return True
    if index > 0 and groups[index - 1].scene_id == group.scene_id:
        if _donate_tail(groups[index - 1], group, low, high):
            return True
    if index + 1 < len(groups) and groups[index + 1].scene_id == group.scene_id:
        if _donate_head(groups[index + 1], group, low):
            return True
    return False


def _merge_groups(left: _Group, right: _Group, high: int) -> Optional[_Group]:
    if left.scene_id != right.scene_id:
        return None
    atoms = left.atoms + right.atoms
    if _ceil_seconds(_atoms_seconds(atoms)) > high:
        return None
    return _Group(scene_id=left.scene_id, atoms=atoms)


def _donate_tail(donor: _Group, receiver: _Group, low: int, high: int) -> bool:
    """Re-divide time so the receiver that follows the donor reaches the minimum.

    The receiver runs later in the script, so it inherits the donor's *tail*:
    only the last slice moves, which keeps both clips in script order.
    """
    while len(donor.atoms) > 1:
        candidate = donor.atoms[-1]
        donor_atoms = donor.atoms[:-1]
        receiver_atoms = [candidate] + receiver.atoms
        if _ceil_seconds(_atoms_seconds(receiver_atoms)) > high:
            break
        if _ceil_seconds(_atoms_seconds(donor_atoms)) < low:
            break
        donor.atoms = donor_atoms
        receiver.atoms = receiver_atoms
        if receiver.clipped_seconds >= low:
            return True
    cut = _cut_atom(
        donor.atoms[-1], needed=low - receiver.clipped_seconds, low=low, towards="tail"
    )
    if cut is None:
        return False
    head, tail = cut
    donor.atoms = donor.atoms[:-1] + [head]
    receiver.atoms = [tail] + receiver.atoms
    return True


def _donate_head(donor: _Group, receiver: _Group, low: int) -> bool:
    """Re-divide time so the receiver that precedes the donor reaches the minimum.

    The donor runs later in the script, so the receiver inherits its *head*. Only a
    time slice can move: the donor's first atom is exactly the atom the greedy
    refused to add to the receiver's own group, so moving whole atoms is already
    known to overflow the cap.
    """
    cut = _cut_atom(
        donor.atoms[0], needed=low - receiver.clipped_seconds, low=low, towards="head"
    )
    if cut is None:
        return False
    head, tail = cut
    donor.atoms = [tail] + donor.atoms[1:]
    receiver.atoms = receiver.atoms + [head]
    return True


def _cut_atom(atom: _Atom, needed: int, low: int, towards: str) -> Optional[Tuple[_Atom, _Atom]]:
    """Split ``atom`` so the moved side (``towards``) reaches ``needed`` seconds.

    Guarantees the caller can rely on: the side that stays behind still
    *allocates* at least ``low`` seconds (so a donation never creates a short
    clip), the moved side is the tightest legal cut at or above ``needed`` (so a
    repair never over-shifts), and ``atom`` itself never exceeds the cap — hence a
    receiver below ``low`` that gains at most the remainder of an atom also stays
    inside the cap.
    """
    if not atom.splittable or needed <= 0:
        return None
    if atom.kind != KIND_DIALOGUE:
        head_seconds = atom.seconds - needed if towards == "tail" else float(needed)
        tail_seconds = atom.seconds - head_seconds
        if _ceil_seconds(head_seconds if towards == "tail" else tail_seconds) < low:
            return None
        return (_cut_piece(atom, "", head_seconds), _cut_piece(atom, "", tail_seconds))
    best: Optional[Tuple[float, str, float, str, float]] = None
    for head_text, head_seconds, tail_text, tail_seconds in _dialogue_cut_candidates(atom):
        kept_seconds = head_seconds if towards == "tail" else tail_seconds
        moved_seconds = tail_seconds if towards == "tail" else head_seconds
        if _ceil_seconds(kept_seconds) < low:
            continue
        if moved_seconds + _EPSILON < needed:
            continue
        if best is None or moved_seconds < best[0]:
            best = (moved_seconds, head_text, head_seconds, tail_text, tail_seconds)
    if best is None:
        return None
    return (_cut_piece(atom, best[1], best[2]), _cut_piece(atom, best[3], best[4]))


def _dialogue_cut_candidates(atom: _Atom):
    """Yield every cut of a dialogue atom as ``(head_text, head_s, tail_text, tail_s)``."""
    audibles = audible_char_count(atom.text)
    if audibles <= 0:
        return
    for cut in _pause_cut_positions(atom.text):
        head_text = atom.text[:cut]
        tail_text = atom.text[cut:]
        if not head_text or not tail_text:
            continue
        head_seconds = audible_char_count(head_text) * atom.seconds / audibles
        yield head_text, head_seconds, tail_text, atom.seconds - head_seconds


def _cut_piece(atom: _Atom, text: str, seconds: float) -> _Atom:
    return _Atom(
        beat_id=atom.beat_id,
        scene_id=atom.scene_id,
        seconds=seconds,
        kind=atom.kind,
        text=text,
        splittable=atom.splittable,
        speaker_ids=atom.speaker_ids,
    )


def _short_group_message(groups: List[_Group], index: int, low: int, high: int) -> str:
    group = groups[index]
    return (
        f"{UNPACKABLE_BEATS}: 第 {index + 1} 段（场景 {group.scene_id!r}，"
        f"{group.seconds:g}s → {group.clipped_seconds}s）低于下限 {low}s，"
        f"且无法在 {low}—{high}s 内通过合并或回分修复："
        "相邻片段换场不得混段，或可切分节拍不足。"
        "请补足该场景内容、调整片段范围，或把相邻节拍标记为 splittable"
    )


def _to_clip(group: _Group, index: int) -> ClipDraft:
    """Render one group, listing each contributing source beat once, in order.

    A repaired clip can hold several pieces of the same beat (设计 §11 允许同一来源
    ID 分片跨片段），but the source list is a set of the beats this clip renders —
    repeating the id would read as duplicated coverage downstream.
    """
    return ClipDraft(
        id=f"{_CLIP_ID_PREFIX}{index:0{_CLIP_ID_DIGITS}d}",
        duration_seconds=group.clipped_seconds,
        beat_ids=tuple(dict.fromkeys(atom.beat_id for atom in group.atoms)),
        dialogue_text="".join(
            atom.text for atom in group.atoms if atom.kind == KIND_DIALOGUE
        ),
    )
