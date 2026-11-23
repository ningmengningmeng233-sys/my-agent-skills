"""模型资产清单：校验、报告与本地同步，绝不静默联网下载。

大模型（人声分离、说话人分离）由用户按 `download_plan` 自行下载；
OCR 模型随 rapidocr wheel 分发，可用 `sync` 从已安装包复制到 `state/models/ocr`。
任何缺失或损坏只禁用对应增强能力，不阻断原音 ASR。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import paths
from scripts.migrations import file_sha256  # 复用 DB 层同一份哈希实现

MANIFEST_PATH = paths.ROOT / "references" / "model-manifest.json"


@dataclass(frozen=True)
class ModelEntry:
    name: str
    capability: str
    relative_path: str
    size_bytes: int
    sha256: str
    required: bool = False
    url: str = ""
    source: str = ""
    install: str = ""


@dataclass(frozen=True)
class AssetState:
    name: str
    status: str
    path: str
    detail: str
    required: bool
    severity: str
    fix_command: str
    size_bytes: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict:
        data = asdict(self)
        data["ok"] = self.ok
        return data


def load_manifest(path: Path | None = None) -> dict:
    manifest_path = Path(path or MANIFEST_PATH)
    if not manifest_path.is_file():
        raise FileNotFoundError(f"模型清单不存在：{manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


class ModelAssetManager:
    """按清单校验 `state/models` 下的模型文件。"""

    def __init__(self, *, root: Path | None = None, manifest: dict | None = None):
        self.root = Path(root or paths.ROOT)
        data = manifest if manifest is not None else load_manifest()
        self._entries = tuple(
            ModelEntry(
                name=str(item["name"]),
                capability=str(item.get("capability", "")),
                relative_path=str(item["relative_path"]),
                size_bytes=int(item.get("size_bytes", 0) or 0),
                sha256=str(item.get("sha256", "")),
                required=bool(item.get("required", False)),
                url=str(item.get("url", "")),
                source=str(item.get("source", "")),
                install=str(item.get("install", "")),
            )
            for item in data.get("models", []))

    def entries(self) -> tuple[ModelEntry, ...]:
        return self._entries

    def entry(self, name: str) -> ModelEntry:
        for item in self._entries:
            if item.name == name:
                return item
        raise KeyError(name)

    def path_of(self, name: str) -> Path:
        return self.root / self.entry(name).relative_path

    def verify(self, name: str) -> AssetState:
        entry = self.entry(name)
        path = self.path_of(name)
        severity = "error" if entry.required else "warning"
        fix = entry.install or (
            f"按 references/model-manifest.json 恢复 {entry.relative_path}；"
            "可运行 .venv\\Scripts\\python.exe scripts\\model_assets.py --json "
            "查看下载地址与哈希")
        if not path.is_file():
            return AssetState(name=entry.name, status="missing", path=str(path),
                              detail=f"缺少模型文件：{entry.relative_path}",
                              required=entry.required, severity=severity,
                              fix_command=fix)
        size = path.stat().st_size
        if entry.size_bytes and size != entry.size_bytes:
            return AssetState(name=entry.name, status="corrupt", path=str(path),
                              detail=f"文件大小不符（期望 {entry.size_bytes}，实际 {size}）",
                              required=entry.required, severity=severity,
                              fix_command=fix, size_bytes=size)
        actual = file_sha256(path)
        if actual != entry.sha256:
            return AssetState(name=entry.name, status="corrupt", path=str(path),
                              detail="SHA-256 哈希与清单不一致，请重新下载",
                              required=entry.required, severity=severity,
                              fix_command=fix, size_bytes=size)
        return AssetState(name=entry.name, status="ok", path=str(path),
                          detail="已就绪", required=entry.required,
                          severity=severity, fix_command="", size_bytes=size)

    def verify_all(self) -> dict:
        states = {entry.name: self.verify(entry.name) for entry in self._entries}
        missing_optional = [s for s in states.values()
                            if s.status != "ok" and not s.required]
        broken_required = [s for s in states.values()
                           if s.status != "ok" and s.required]
        return {
            "ok": not missing_optional and not broken_required,
            "broken_required": len(broken_required),
            "missing_optional": len(missing_optional),
            "states": states,
        }

    def capability_usable(self, capability: str) -> bool:
        related = [entry for entry in self._entries
                   if entry.capability == capability]
        if not related:
            return False
        if capability == "diarization":
            # 说话人分离需要分割与声纹两个模型同时可用
            return all(self.verify(entry.name).ok for entry in related)
        return any(self.verify(entry.name).ok for entry in related)

    def sync_from_local(self, name: str, source: Path) -> Path:
        """把本地已安装包里的模型复制进 `state/models`；不做任何下载。"""
        target = self.path_of(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(source), target)
        return target

    def sync_ocr_models(self) -> list[Path]:
        """从已安装的 rapidocr 包复制内置模型到 `state/models/ocr`。"""
        import rapidocr

        package_models = Path(rapidocr.__file__).parent / "models"
        copied: list[Path] = []
        for entry in self._entries:
            if entry.capability != "ocr":
                continue
            if self.verify(entry.name).ok:
                continue
            source = package_models / Path(entry.relative_path).name
            if not source.is_file():
                continue
            copied.append(self.sync_from_local(entry.name, source))
        return copied

    def download_plan(self) -> list[dict]:
        """只描述需要用户自己执行的下载步骤。"""
        plan = []
        for entry in self._entries:
            if self.verify(entry.name).ok or not entry.url:
                continue
            plan.append({"name": entry.name,
                         "url": entry.url,
                         "relative_path": entry.relative_path,
                         "sha256": entry.sha256,
                         "size_bytes": entry.size_bytes,
                         "source": entry.source})
        return plan


def main(argv: list[str] | None = None) -> int:
    from scripts.logger import ensure_utf8_stdio

    ensure_utf8_stdio()
    parser = argparse.ArgumentParser(description="模型资产校验")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--sync", action="store_true",
                        help="从已安装的 rapidocr 包复制内置 OCR 模型")
    args = parser.parse_args(argv)

    manager = ModelAssetManager()
    if args.sync:
        copied = manager.sync_ocr_models()
        for path in copied:
            print(f"已复制 {path}")
    report = manager.verify_all()
    payload = {
        "ok": report["ok"],
        "broken_required": report["broken_required"],
        "missing_optional": report["missing_optional"],
        "states": {name: state.as_dict()
                   for name, state in report["states"].items()},
        "download_plan": manager.download_plan(),
    }
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        for name, state in report["states"].items():
            mark = "OK  " if state.ok else ("WARN" if state.severity == "warning"
                                            else "FAIL")
            print(f"[{mark}] {name}: {state.detail}")
        if payload["download_plan"]:
            print("需要手动下载的模型：")
            for item in payload["download_plan"]:
                print(f"  {item['url']}\n    → {item['relative_path']}"
                      f"（sha256 {item['sha256'][:16]}…）")
    return 1 if report["broken_required"] else 0


if __name__ == "__main__":
    sys.exit(main())
