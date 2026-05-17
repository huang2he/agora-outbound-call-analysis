"""agent_kda v2 单元测试。

跑法:
    cd ~/.claude/skills/agora-outbound-call-analysis
    .venv/bin/python -m scripts.lib.test_agent_kda
"""

from __future__ import annotations

import pandas as pd

from .agent_kda import (
    LEVEL_FIELDS,
    SYSTEM_USER_TURN_PATTERNS,
    parse_structured_output,
    parse_transcript,
    is_real_user_turn,
    real_user_turn_count,
    is_valid_session,
    total_turn_count,
    passed_level,
    passed_levels_count,
    filled_slots_independent,
    classify_zero_pass_reason,
    compute_tab2_data,
)


def test_parse_robust():
    assert parse_structured_output(None) == {}
    assert parse_structured_output("") == {}
    assert parse_structured_output("null") == {}
    assert parse_structured_output("not-json") == {}
    assert parse_structured_output('{"a": 1}') == {"a": 1}
    assert parse_transcript("[]") == []
    assert parse_transcript("") == []
    assert parse_transcript(None) == []


def test_level1_and_logic():
    """车型关：品牌 AND 型号"""
    assert passed_level({"购车品牌": "丰田"}, 1) is False
    assert passed_level({"购车型号": "凯美瑞"}, 1) is False
    assert passed_level({"购车品牌": "丰田", "购车型号": None}, 1) is False
    assert passed_level({"购车品牌": "丰田", "购车型号": "凯美瑞"}, 1) is True


def test_passed_levels_count_linear():
    """严格线性"""
    so = {"购车品牌": "丰田", "购车型号": "凯美瑞", "购车城市": "上海",
          "购车时间": "6月", "购车姓名": None}
    assert passed_levels_count(so) == 3
    # 跳过中间：姓氏过了但城市没过 → 严格线性下从第 2 关就断
    so2 = {"购车品牌": "丰田", "购车型号": "凯美瑞", "购车城市": None,
           "购车时间": "6月", "购车姓名": "张"}
    assert passed_levels_count(so2) == 1
    # 全过
    so3 = {"购车品牌": "丰田", "购车型号": "凯美瑞", "购车城市": "上海",
           "购车时间": "6月", "购车姓名": "张"}
    assert passed_levels_count(so3) == 4
    # 全空
    assert passed_levels_count({}) == 0


def test_filled_slots_independent():
    """独立看每关填充，无需线性"""
    so = {"购车品牌": "丰田", "购车型号": None, "购车城市": "上海",
          "购车时间": None, "购车姓名": "张"}
    s = filled_slots_independent(so)
    assert "车型" not in s   # 型号缺
    assert "城市" in s
    assert "时间" not in s
    assert "姓氏" in s


def test_is_real_user_turn_silence():
    """metadata.source == silence 的占位 → 非真实用户"""
    fake_silence = {
        "role": "user",
        "content": "识别客户没有响应，走静默兜底逻辑",
        "metadata": {"source": "silence"},
    }
    assert is_real_user_turn(fake_silence) is False


def test_is_real_user_turn_ivr():
    """IVR 语音信箱文本指纹"""
    ivr = {"role": "user",
           "content": "请留下你的姓名和来电原因，我会帮你确认此人是否方便接听。"}
    assert is_real_user_turn(ivr) is False
    voicemail = {"role": "user", "content": "智语音留言，您拨打的电话..."}
    assert is_real_user_turn(voicemail) is False


def test_is_real_user_turn_normal():
    """正常用户说话"""
    ok = {"role": "user", "content": "我想看看丰田凯美瑞。",
          "metadata": {"source": "asr"}}
    assert is_real_user_turn(ok) is True
    # 内容为空 → 非真实
    assert is_real_user_turn({"role": "user", "content": "   "}) is False


def test_real_user_turn_count_filters():
    transcript = [
        {"turn_id": 1, "role": "assistant", "content": "您好"},
        {"turn_id": 2, "role": "user", "content": "识别客户没有响应",
         "metadata": {"source": "silence"}},
        {"turn_id": 3, "role": "assistant", "content": "您看车没？"},
        {"turn_id": 4, "role": "user", "content": "嗯。"},
    ]
    assert real_user_turn_count(transcript) == 1


def test_is_valid_session():
    """首句挂断 → 无效；只有系统兜底 → 无效；有 1 句真用户 → 有效"""
    first_hangup = [{"turn_id": 1, "role": "assistant", "content": "您好"}]
    assert is_valid_session(first_hangup) is False

    silence_only = first_hangup + [
        {"role": "user", "content": "识别客户没有响应",
         "metadata": {"source": "silence"}}
    ]
    assert is_valid_session(silence_only) is False

    good = first_hangup + [{"role": "user", "content": "嗯。"}]
    assert is_valid_session(good) is True


def test_zero_pass_reason():
    def make(text):
        return [{"role": "user", "content": text, "metadata": {"source": "asr"}}]
    assert classify_zero_pass_reason(make("我不需要")) == "拒绝/无意向"
    assert classify_zero_pass_reason(make("我在开车")) == "忙/没空"
    assert classify_zero_pass_reason(make("操你妈")) == "客户骂人"
    assert classify_zero_pass_reason(make("你哪位")) == "质疑身份"
    assert classify_zero_pass_reason(make("嗯哦")) == "聊别的/未触及"


def test_compute_tab2_data_shape():
    df = pd.DataFrame({
        "Agent Name": ["A", "A", "A", "B", "B"],
        "Duration (seconds)": [10, 30, 0, 20, 5],
        "Hangup Reason": ["USER_HANGUP", "USER_HANGUP", "NO_ANSWER", "AI_HANGUP", "USER_HANGUP"],
        "Structured Output": [
            '{"购车品牌":"丰田","购车型号":"凯美瑞","购车城市":"上海","购车时间":"6月","购车姓名":"张"}',
            '{"购车品牌":"奔驰","购车型号":"E300"}',  # 1 关
            '{}',
            '{}',  # 真人接听但 0 关
            '{"购车品牌":"宝马","购车型号":"3系","购车城市":"北京"}',  # 2 关
        ],
        "Transcript": [
            '[{"turn_id":1,"role":"assistant","content":"hi"},{"turn_id":2,"role":"user","content":"丰田凯美瑞上海张"}]',
            '[{"turn_id":1,"role":"assistant","content":"hi"},{"turn_id":2,"role":"user","content":"奔驰E300"}]',
            '[]',
            '[{"turn_id":1,"role":"assistant","content":"hi"},{"turn_id":2,"role":"user","content":"我不需要"}]',
            '[{"turn_id":1,"role":"assistant","content":"hi"},{"turn_id":2,"role":"user","content":"宝马3系北京"}]',
        ],
    })
    data = compute_tab2_data(df)
    g = data["global"]
    assert g["n_total"] == 5
    assert g["n_human"] == 4
    assert g["n_valid"] == 4   # 4 个真人接听都有真实 user turn
    # bucket: 0=1, 1=1, 2=1, 3=0, 4=1
    by_lv = {b["level"]: b for b in g["buckets"]}
    assert by_lv[0]["count"] == 1
    assert by_lv[1]["count"] == 1
    assert by_lv[2]["count"] == 1
    assert by_lv[3]["count"] == 0
    assert by_lv[4]["count"] == 1
    # 0 关有 reasons
    assert "拒绝/无意向" in by_lv[0]["reasons"]
    # 各 Agent
    assert len(data["agents"]) == 2


# 单文件直接跑
if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failed = []
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed.append((t.__name__, str(e)))
            print(f"  ✗ {t.__name__}: {e}")
    if failed:
        print(f"\n{len(failed)}/{len(tests)} failed")
        raise SystemExit(1)
    print(f"\n✓ {len(tests)} tests passed")
