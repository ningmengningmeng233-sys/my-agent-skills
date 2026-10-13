# OCR 合成 fixture

本目录只放**生成脚本**，不放二进制视频：`synthetic_video.py` 会用 PIL 画帧、
再用 FFmpeg 把帧合成一段短视频，产物写入调用方给的临时目录。

## 为什么需要它

真实抖音样本含隐私且体积大，不适合进 Git。合成样本能稳定复现三类画面：

| 时间段 | 画面内容 | 期望 OCR 分类 |
|---|---|---|
| 0.0–3.0s | 深色背景 + 底部连续字幕「端午粽子」 | `speech_subtitle` |
| 3.0–6.0s | 浅色背景（场景切换）+ 底部字幕「不分南北」+ 中部参数行「参数 2026-06-19 1.2 GB」 | `speech_subtitle` + `static_info` |

因此它同时覆盖：基础 1 fps 抽帧、`scene>0.25` 场景补帧、底部字幕激活、
相邻字幕合并、静态参数与语音字幕的分类差异。

## 用法

```python
from tests.fixtures.ocr.synthetic_video import build_synthetic_video

video = build_synthetic_video(Path(tmpdir))   # 返回 .mp4 路径
```

依赖系统 FFmpeg 与 Windows 自带中文字体（`C:/Windows/Fonts/msyh.ttc`）；
字体缺失时回退到 PIL 默认字体，此时中文字形不可读，相关断言会自动跳过。
