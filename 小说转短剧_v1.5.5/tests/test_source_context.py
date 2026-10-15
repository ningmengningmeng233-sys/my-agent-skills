import pytest
from unittest.mock import MagicMock
from dramacraft.self_healing import generate_episode_with_healing


def test_source_context_injected_into_prompt():
    ep_meta = {"ep": 1, "targetSeconds": 90}
    mock_gateway = MagicMock()
    mock_gateway.call_llm.return_value = '{"title": "测试", "scenes": []}'

    source_ctx = "这是第一章原著正文：北境长城之上，战神归来。"

    # 验证是否支持 source_context 并注入到了 call_llm 的 prompt 参数中
    try:
        generate_episode_with_healing(
            ep_meta=ep_meta,
            gateway=mock_gateway,
            source_context=source_ctx,
            max_retries=1
        )
    except Exception:
        pass

    assert mock_gateway.call_llm.called
    called_prompt = mock_gateway.call_llm.call_args[1].get("prompt") or mock_gateway.call_llm.call_args[0][0]
    assert "【原著对应章节参考】" in called_prompt
    assert "北境长城之上，战神归来" in called_prompt
