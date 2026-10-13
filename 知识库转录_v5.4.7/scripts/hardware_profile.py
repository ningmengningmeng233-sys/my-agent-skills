# -*- coding: utf-8 -*-
"""硬件自适配：按本机算力选择最合适的本地向量模型。

设计原则（ADR：硬件自适配）：
  1. **只探测不臆测**：显存走 nvidia-smi（WMI 的 AdapterRAM 有 4GB 上限缺陷，
     实测 RTX 2060 会被误报为 4GB 而真实为 6GB）；内存走 psutil 或系统 API。
  2. **保守优先**：探测失败一律按最低档处理，绝不让弱机去加载大模型导致卡死。
  3. **显存按可用量而非标称量**：扣除桌面/浏览器占用，用 memory.free。
  4. **配置可覆盖**：.env 显式指定 INDEX_EMBEDDING_MODEL 时，用户意志优先。
  5. **已装模型优先复用**：本地已有可用模型时不强制用户下载新模型。

分档依据（单精度推理约需 模型体积 × 1.2 的显存余量）：
  qwen3-embedding:0.6b  0.60 GB  → 显存 ≥ 1.5GB 即可
  qwen3-embedding:4b    2.33 GB  → 显存 ≥ 3.5GB
  qwen3-embedding:8b    4.36 GB  → 显存 ≥ 6.0GB
显存不足时退化为纯 CPU 推理（Ollama 自动处理），此时优先小模型保速度。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# 候选模型：(模型名, 体积GB, 所需显存GB)，按**体积降序**（大 → 小）。
# 自适配时从大到小试，第一个能承载的即为最优选择。
CANDIDATES = (
    ("qwen3-embedding:8b", 4.36, 6.0),
    ("qwen3-embedding:4b", 2.33, 3.5),
    ("qwen3-embedding:0.6b", 0.60, 1.5),
)
FALLBACK_MODEL = "qwen3-embedding:0.6b"
# CPU-only 推理时的保守选择（大模型纯 CPU 会慢到不可用）
CPU_ONLY_MODEL = "qwen3-embedding:0.6b"
# 可用内存下限（GB）：小模型 CPU 推理也需留出余量
MIN_RAM_GB = 4.0
# Ollama 默认地址（CLI 显示用）
DEFAULT_OLLAMA_URL = "http://localhost:11434"


@dataclass(frozen=True)
class HardwareProfile:
    """本机算力画像。"""

    gpu_name: str
    vram_total_gb: float
    vram_free_gb: float
    ram_total_gb: float
    ram_free_gb: float
    gpu_available: bool
    probe_notes: tuple[str, ...] = ()

    def summary(self) -> str:
        if self.gpu_available:
            return (f"GPU={self.gpu_name} 显存 {self.vram_free_gb:.1f}/{self.vram_total_gb:.1f}GB"
                    f" | 内存 {self.ram_free_gb:.1f}/{self.ram_total_gb:.1f}GB")
        return f"无可用 CUDA GPU | 内存 {self.ram_free_gb:.1f}/{self.ram_total_gb:.1f}GB"


def _probe_nvidia_smi() -> tuple[str, float, float] | None:
    """用 nvidia-smi 读 GPU 名称与显存（GB）。不可用返回 None。"""
    exe = shutil.which("nvidia-smi")
    if not exe:
        for cand in (
            r"C:\Windows\System32\nvidia-smi.exe",
            r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
        ):
            if os.path.isfile(cand):
                exe = cand
                break
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
        )
        if out.returncode != 0:
            return None
        line = (out.stdout or "").strip().splitlines()[0]
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            return None
        name = parts[0]
        total = float(parts[1]) / 1024.0
        free = float(parts[2]) / 1024.0
        return name, total, free
    except Exception:
        return None


def _probe_ram() -> tuple[float, float]:
    """返回 (总内存GB, 可用内存GB)。探测失败返回 (0, 0)。"""
    try:
        import psutil
        vm = psutil.virtual_memory()
        return vm.total / 1024 ** 3, vm.available / 1024 ** 3
    except Exception:
        pass
    try:
        if os.name == "nt":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return stat.ullTotalPhys / 1024 ** 3, stat.ullAvailPhys / 1024 ** 3
        else:
            pages = os.sysconf("SC_PHYS_PAGES")
            avail = os.sysconf("SC_AVPHYS_PAGES")
            size = os.sysconf("SC_PAGE_SIZE")
            return pages * size / 1024 ** 3, avail * size / 1024 ** 3
    except Exception:
        pass
    return 0.0, 0.0


def detect_hardware() -> HardwareProfile:
    """探测本机算力。任何探测失败都降级为保守画像，不抛异常。"""
    notes: list[str] = []
    gpu = _probe_nvidia_smi()
    if gpu:
        name, total, free = gpu
        gpu_ok = total > 0
        if not gpu_ok:
            notes.append("nvidia-smi 报告显存为 0，按无 GPU 处理")
    else:
        name, total, free, gpu_ok = "", 0.0, 0.0, False
        notes.append("nvidia-smi 不可用，按 CPU 推理处理")
    ram_total, ram_free = _probe_ram()
    if ram_total <= 0:
        notes.append("内存探测失败，按最低档处理")
    return HardwareProfile(
        gpu_name=name,
        vram_total_gb=round(total, 2),
        vram_free_gb=round(free, 2),
        ram_total_gb=round(ram_total, 2),
        ram_free_gb=round(ram_free, 2),
        gpu_available=gpu_ok,
        probe_notes=tuple(notes),
    )


def pick_embedding_model(profile: HardwareProfile,
                         installed: list[str] | None = None) -> tuple[str, str]:
    """按硬件画像挑选向量模型，返回 (模型名, 选择理由)。

    优先级：
      1. 已安装的候选模型中，硬件能承载的最大者（避免重复下载）
      2. 硬件能承载的候选中最大者（提示需下载）
      3. 兜底 0.6b
    """
    installed_set = {str(m).strip() for m in (installed or [])}

    def _can_run(need_vram: float) -> bool:
        if profile.gpu_available:
            # 显存按可用量判断，留 0.3GB 余量给驱动/桌面
            return profile.vram_free_gb >= (need_vram - 0.3)
        # CPU 推理：只看内存，且大模型纯 CPU 太慢，统一走小模型
        return False

    # 1. 已装且能跑的最大候选
    for name, _size, need in CANDIDATES:
        if name in installed_set and _can_run(need):
            return name, f"复用本地已装模型（显存 {profile.vram_free_gb:.1f}GB 可承载，需 {need}GB）"

    # 2. 未装但硬件够用的最大候选（需下载）
    for name, size, need in CANDIDATES:
        if _can_run(need):
            tag = "已装" if name in installed_set else f"需下载 {size}GB"
            return name, f"硬件可承载的最大模型（{tag}，需显存 {need}GB，可用 {profile.vram_free_gb:.1f}GB）"

    # 3. CPU-only 或无 GPU：小模型 + 内存校验
    if profile.ram_total_gb and profile.ram_free_gb >= MIN_RAM_GB:
        return CPU_ONLY_MODEL, (
            f"无可用 GPU，按 CPU 推理选小模型（可用内存 {profile.ram_free_gb:.1f}GB）"
        )

    # 4. 内存也不足：仍返回小模型，由上层决定是否阻塞
    return FALLBACK_MODEL, (
        f"显存与内存均偏紧（显存 {profile.vram_free_gb:.1f}GB / 内存 {profile.ram_free_gb:.1f}GB），"
        "按最低档保守处理"
    )


def _installed_from_disk() -> list[str]:
    """从 Ollama 本地 manifests 目录读取已装模型名（服务未运行时兜底）。

    目录结构：<models>/manifests/<registry>/<namespace>/<model>/<tag>
    模型名还原为 `<model>:<tag>`；`library` 是官方命名空间，不进模型名。

    必要性：服务未运行时若直接判定「什么都没装」，自适配会推荐需下载的大模型，
    随后 404 失败——比如此前 Ollama 停止时误选 qwen3-embedding:4b。
    """
    roots: list[Path] = []
    env_dir = os.environ.get("OLLAMA_MODELS", "").strip()
    if env_dir:
        roots.append(Path(env_dir))
    home = Path.home()
    roots += [
        home / ".ollama" / "models",
        # 原来这里还拼了一次「盘符 + Users + USERNAME」的猜测路径：
        # 家目录那条已覆盖本机场景，多出来这条既让脱敏扫描按私有路径报，
        # 又等于去探测别的用户目录，故删除（行为不变）。
    ]
    for r in roots:
        manifests = r / "manifests"
        if not manifests.is_dir():
            continue
        found: list[str] = []
        try:
            for p in manifests.rglob("*"):
                if not p.is_file():
                    continue
                parts = p.relative_to(manifests).parts
                if len(parts) < 3:
                    continue
                model, tag = parts[-2], parts[-1]
                namespace = parts[-3] if len(parts) >= 3 else ""
                name = f"{model}:{tag}"
                # 非 library 命名空间需保留前缀以区分同名模型
                if namespace and namespace != "library":
                    name = f"{namespace}/{name}"
                found.append(name)
        except Exception:
            continue
        if found:
            return sorted(set(found))
    return []


def list_installed_models(base_url: str = DEFAULT_OLLAMA_URL,
                          timeout: int = 5) -> list[str]:
    """查询 Ollama 已安装模型名。服务不可达时回退读本地 manifests，仍失败返回空。"""
    try:
        import requests
        resp = requests.get(base_url.rstrip("/") + "/api/tags", timeout=timeout)
        resp.raise_for_status()
        names = [m.get("name", "") for m in resp.json().get("models", [])]
        # 服务返回的名称不含自定义命名空间前缀时，用磁盘结果补充（同名去重）
        disk = _installed_from_disk()
        merged = list(dict.fromkeys([n for n in names if n] + disk))
        return merged
    except Exception:
        return _installed_from_disk()


def resolve_embedding_model(base_url: str = "http://localhost:11434") -> tuple[str, str]:
    """一站式解析：探测硬件 + 查已装模型 + 给出选择。

    返回 (模型名, 人类可读理由)。不做任何下载动作。
    """
    profile = detect_hardware()
    installed = list_installed_models(base_url)
    model, reason = pick_embedding_model(profile, installed)
    return model, f"{reason}｜{profile.summary()}"


def is_placeholder_tag(name: str) -> bool:
    """判断是否为占位/无效模型名（供 .env 校验用）。"""
    n = (name or "").strip()
    if not n:
        return True
    return not re.match(r"^[A-Za-z0-9._\-]+(:[A-Za-z0-9._\-]+)?$", n)


def main() -> int:
    """命令行入口：打印本机硬件画像与自适配模型建议。"""
    import argparse
    import json

    parser = argparse.ArgumentParser(description="本机算力探测与向量模型自适配")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--host", default=DEFAULT_OLLAMA_URL, help="Ollama 地址")
    args = parser.parse_args()

    profile = detect_hardware()
    installed = list_installed_models(args.host)
    model, reason = pick_embedding_model(profile, installed)

    if args.json:
        print(json.dumps({
            "hardware": {
                "gpu_available": profile.gpu_available,
                "gpu_name": profile.gpu_name,
                "vram_total_gb": profile.vram_total_gb,
                "vram_free_gb": profile.vram_free_gb,
                "ram_total_gb": profile.ram_total_gb,
                "ram_free_gb": profile.ram_free_gb,
                "probe_notes": list(profile.probe_notes),
            },
            "installed_models": installed,
            "recommended_model": model,
            "reason": reason,
        }, ensure_ascii=False, indent=2))
        return 0

    print("=" * 72)
    print("本机算力探测与向量模型自适配")
    print("=" * 72)
    print(f"硬件画像 : {profile.summary()}")
    if profile.probe_notes:
        for n in profile.probe_notes:
            print(f"           - {n}")
    print(f"已装模型 : {', '.join(installed) if installed else '（Ollama 不可达或未安装模型）'}")
    print()
    print(f"→ 推荐模型: {model}")
    print(f"  选择依据: {reason}")
    print()
    print("分档规则: 显存≥6GB → 8b ｜ ≥3.5GB → 4b ｜ 其余/无GPU → 0.6b")
    print("覆盖方式: .env 设置 INDEX_EMBEDDING_MODEL=<模型名>（用户指定优先）")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(main())
