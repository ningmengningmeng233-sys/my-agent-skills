import json


def test_each_check_returns_fix(tmp_path, monkeypatch):
    from engine import doctor
    for item in doctor.run_checks(lib=tmp_path):
        assert set(["name", "ok", "detail", "fix"]).issubset(item.keys())
        if not item["ok"]:
            assert item["fix"], f"{item['name']} 失败却没给修法"


def test_never_prints_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-SECRET-VALUE")
    from engine import doctor
    text = json.dumps(doctor.run_checks(lib=tmp_path), ensure_ascii=False)
    assert "sk-SECRET-VALUE" not in text


# --- F15：体检的配置源必须与运行时同源 -------------------------------------------------
# 实测现象（2026-09-21 冒烟）：同一次 doctor 里「.env 配置: 已配置（<env 文件>）」与
# 「节点池: 无 llm_nodes.json 且 SUMMARY_API_URL 未配置」并排出现——前者读了 env 文件、
# 后者只读进程环境。运行时 llm.load_env() 是读文件的，所以那一行 ❌ 是假告警。

ENV_FILE_BODY = (
    "OPENAI_API_KEY=sk-file-value\n"
    "SUMMARY_API_URL=http://127.0.0.1:9/v1/chat/completions\n"
    "SUMMARY_API_MODEL=file-model\n"
)


def _isolate_config(tmp_path, monkeypatch, env_body):
    """把「配置可能在哪」收窄成单一可控点：候选 .env 只留 tmp 一个，节点池文件一律找不到。

    本机 ~/.asset-extract/.env 真实存在，不隔离的话用例会读到我自己的配置。
    """
    from engine import common, llm_pool
    env_file = tmp_path / "relay.env"
    if env_body is not None:
        env_file.write_text(env_body, encoding="utf-8")
    monkeypatch.setattr(common, "env_candidates", lambda *a, **k: [env_file])
    monkeypatch.setattr(llm_pool, "_candidates", lambda *a, **k: [])
    for key in ("OPENAI_API_KEY", "SUMMARY_API_URL", "SUMMARY_API_MODEL", "ASSET_ENV_PATH"):
        monkeypatch.delenv(key, raising=False)
    # 离线钉死（B8）后包外 .env 的可见入口收敛为显式 ASSET_ENV_PATH（隐式回退被
    # 屏蔽）；这里用显式入口还原「在线生产路径同源」的被测语义，断言不变。
    monkeypatch.setenv("ASSET_ENV_PATH", str(env_file))
    return env_file


def test_node_pool_sees_env_file(tmp_path, monkeypatch):
    """F15 主体：三件套只写在 env 文件里时，节点池也必须判 ✅。"""
    _isolate_config(tmp_path, monkeypatch, ENV_FILE_BODY)
    from engine import doctor
    checks = {c["name"]: c for c in doctor.run_checks(lib=tmp_path)}
    assert checks["节点池"]["ok"], checks["节点池"]["detail"]
    assert checks[".env 配置"]["ok"], checks[".env 配置"]["detail"]


def test_config_read_is_read_only(tmp_path, monkeypatch):
    """反例（不削弱只读承诺）：体检读 env 文件，但不得把值写进进程环境。"""
    import os
    _isolate_config(tmp_path, monkeypatch, ENV_FILE_BODY)
    from engine import doctor
    doctor.run_checks(lib=tmp_path)
    assert "SUMMARY_API_URL" not in os.environ


def test_process_env_still_wins_over_env_file(tmp_path, monkeypatch):
    """反例（口径不得倒挂）：进程环境优先，与运行时 load_dotenv(override=False) 一致。"""
    _isolate_config(tmp_path, monkeypatch, ENV_FILE_BODY)
    monkeypatch.setenv("SUMMARY_API_MODEL", "env-model")
    from engine import common
    values, path = common.env_values()
    assert values["SUMMARY_API_MODEL"] == "env-model"
    assert values["SUMMARY_API_URL"].endswith("/v1/chat/completions")   # 文件里的其他键仍在
    assert path == tmp_path / "relay.env"


def test_missing_config_still_fails(tmp_path, monkeypatch):
    """反例（判据不削弱）：两边都没有时，两项都必须 ❌ 且给出修法。"""
    _isolate_config(tmp_path, monkeypatch, None)
    from engine import doctor
    checks = {c["name"]: c for c in doctor.run_checks(lib=tmp_path)}
    assert not checks["节点池"]["ok"]
    assert checks["节点池"]["fix"]
    assert not checks[".env 配置"]["ok"]
    assert checks[".env 配置"]["fix"]


def test_non_utf8_env_file_does_not_crash(tmp_path, monkeypatch):
    """Block（评审发现）：GB18030/UTF-16 的 .env 不得让体检与 status 整体崩。

    旧实现用 `read_text(errors="replace")` 只读乱码、不崩；本批新增的只读配置视图
    漏了这个编码分支，实测 `env_values` / `_has_llm_config` / `doctor.run_checks`
    全部抛 `UnicodeDecodeError`——用户拿到 traceback 而不是体检报告。
    """
    env_file = tmp_path / "gb.env"
    env_file.write_bytes("OPENAI_API_KEY=sk-x\n# 中文注释：密钥不显示\n".encode("gb18030"))
    _isolate_config(tmp_path, monkeypatch, None)
    from engine import common, doctor
    monkeypatch.setattr(common, "env_candidates", lambda *a, **k: [env_file])
    monkeypatch.setenv("ASSET_ENV_PATH", str(env_file))
    values, _path = common.env_values()          # 不得抛
    assert "OPENAI_API_KEY" not in values
    from maintenance import _has_llm_config
    assert _has_llm_config() in (True, False)    # 不得抛
    checks = {c["name"]: c for c in doctor.run_checks(lib=tmp_path)}   # 不得抛
    assert ".env 配置" in checks
