import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import ai_client


def test_build_messages_contains_draft_and_context():
    cfg = config.load_config()
    cfg["versions"] = 3
    msgs = ai_client.build_messages("请问密钥是从0还是1", "对方：文件序号是多少", cfg)
    assert msgs[0]["role"] == "system"
    assert "请问密钥是从0还是1" in msgs[1]["content"]
    assert "对方：文件序号是多少" in msgs[1]["content"]


def test_build_messages_no_context():
    cfg = config.load_config()
    msgs = ai_client.build_messages("你好", "", cfg)
    assert "我的草稿" in msgs[1]["content"]
    assert "对话上下文" not in msgs[1]["content"]


def test_build_messages_reply_mode():
    cfg = config.load_config()
    msgs = ai_client.build_messages("", "对方：文件序号是多少", cfg, mode="reply")
    assert "对方：文件序号是多少" in msgs[1]["content"]
    assert "对话上下文" in msgs[1]["content"]
    assert "我的草稿" not in msgs[1]["content"]
    assert "起草" in msgs[1]["content"]


def test_parse_versions_json():
    assert ai_client._parse_versions('["a", "b", "c"]', 3) == ["a", "b", "c"]


def test_parse_versions_fenced():
    out = ai_client._parse_versions('```json\n["x", "y"]\n```', 3)
    assert out == ["x", "y"]


def test_parse_versions_fallback():
    out = ai_client._parse_versions("版本1：你好\n版本2：您好", 3)
    assert len(out) >= 1


def test_parse_versions_empty():
    assert ai_client._parse_versions("", 3) == []


# ---------- 网络层：连接类错误要重试，HTTP 错误不重试 ----------

def _cfg_ready():
    cfg = config.load_config()
    cfg["api_key"] = "sk-test-key"
    cfg["log_prompt"] = False          # 测试别往 polish.log 里灌提示词
    return cfg


class _Resp:
    status_code = 200
    text = ""

    def json(self):
        return {"choices": [{"message": {"content": '["版本1","版本2","版本3"]'}}]}


def test_connection_error_is_retried_then_succeeds():
    """TLS 被中间设备掐断（SSLEOFError）这类错误要自动重试一次，重试成功就照常返回。

    实测来源：朋友机器上 `SSLError(SSLEOFError(8, 'UNEXPECTED_EOF_WHILE_READING'))`，
    属于连接类错误，一次重试往往就能过。
    """
    import requests
    from unittest.mock import patch

    calls = {"n": 0}

    def fake_post(url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.SSLError(
                "SSLError(SSLEOFError(8, 'UNEXPECTED_EOF_WHILE_READING'))")
        return _Resp()

    with patch("requests.post", side_effect=fake_post), \
            patch.object(ai_client, "_RETRY_WAIT", 0):
        out = ai_client.polish("明天见", "", _cfg_ready())
    assert out == ["版本1", "版本2", "版本3"], out
    assert calls["n"] == 2, f"应当重试一次，实际请求 {calls['n']} 次"


def test_connection_error_gives_up_after_one_retry():
    """持续失败（比如公司网络一直拦）不能无限重试：重试完仍然抛错，交给上层出提示。"""
    import requests
    from unittest.mock import patch

    calls = {"n": 0}

    def always_fail(url, **kw):
        calls["n"] += 1
        raise requests.exceptions.SSLError("SSLEOFError: EOF occurred")

    with patch("requests.post", side_effect=always_fail), \
            patch.object(ai_client, "_RETRY_WAIT", 0):
        try:
            ai_client.polish("明天见", "", _cfg_ready())
            raise SystemExit("应当抛错")
        except requests.exceptions.SSLError:
            pass
    assert calls["n"] == 2, f"应当只在重试一次后放弃，实际请求 {calls['n']} 次"


def test_http_status_error_is_not_retried():
    """HTTP 状态码错误（401 之类）不是网络问题，不该重试。"""
    from unittest.mock import patch

    calls = {"n": 0}

    def fake_post(url, **kw):
        calls["n"] += 1
        resp = _Resp()
        resp.status_code = 401
        resp.text = "invalid api key"
        return resp

    with patch("requests.post", side_effect=fake_post):
        try:
            ai_client.polish("明天见", "", _cfg_ready())
            raise SystemExit("应当抛错")
        except RuntimeError as e:
            assert "401" in str(e), e
    assert calls["n"] == 1, f"401 不该重试，实际请求 {calls['n']} 次"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {fn.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
