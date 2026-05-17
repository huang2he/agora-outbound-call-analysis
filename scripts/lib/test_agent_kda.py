"""agent_kda 模块的最小单元测试。

跑法：
    cd ~/.claude/skills/agora-outbound-call-analysis
    .venv/bin/python -m scripts.lib.test_agent_kda
"""

from __future__ import annotations

import pandas as pd

from .agent_kda import (
    LEVEL_FIELDS,
    parse_structured_output,
    passed_level,
    passed_levels_count,
    turn_at_level_hit,
    detect_friction,
    compute_product_funnel,
    compute_agent_radar,
    compute_agent_ranking,
)


def test_parse_robust():
    assert parse_structured_output(None) == {}
    assert parse_structured_output("") == {}
    assert parse_structured_output("null") == {}
    assert parse_structured_output("not-json") == {}
    assert parse_structured_output('{"a": 1}') == {"a": 1}


def test_level1_and_logic():
    """车型关：品牌 AND 型号 都非空（5/18 修正）"""
    assert passed_level({"购车品牌": "丰田"}, 1) is False
    assert passed_level({"购车型号": "凯美瑞"}, 1) is False
    assert passed_level({"购车品牌": "丰田", "购车型号": None}, 1) is False
    assert passed_level({"购车品牌": "丰田", "购车型号": "凯美瑞"}, 1) is True


def test_levels_basic():
    so = parse_structured_output(
        '{"购车品牌":"丰田","购车型号":"凯美瑞","购车城市":"上海","购车时间":"6月","购车姓名":null}'
    )
    assert passed_level(so, 1) is True
    assert passed_level(so, 2) is True
    assert passed_level(so, 3) is True
    assert passed_level(so, 4) is False
    assert passed_levels_count(so) == 3   # 严格线性: 1,2,3 都过, 4 没过 → 3


def test_full_pass():
    so = parse_structured_output(
        '{"购车品牌":"丰田","购车型号":"凯美瑞","购车城市":"上海","购车时间":"6月","购车姓名":"张先生"}'
    )
    assert passed_levels_count(so) == 4
    assert all(passed_level(so, l) for l in (1, 2, 3, 4))


def test_strict_linear():
    """姓氏过但车型没过 → 严格线性下从第 1 关就断"""
    so = parse_structured_output(
        '{"购车品牌":null,"购车型号":null,"购车城市":"上海","购车时间":"6月","购车姓名":"张先生"}'
    )
    assert passed_levels_count(so) == 0


def test_null_string_treated_as_null():
    assert _val_filled("null") is False  # 字符串 'null' 不算填


def test_turn_at_level_hit():
    transcript = [
        {"turn_id": 1, "role": "assistant", "content": "您好，想买什么车呢？"},
        {"turn_id": 2, "role": "user",      "content": "我想看看丰田凯美瑞。"},
        {"turn_id": 3, "role": "assistant", "content": "好的，您在哪个城市呢？"},
        {"turn_id": 4, "role": "user",      "content": "上海。"},
    ]
    assert turn_at_level_hit(transcript, 1) == 2
    assert turn_at_level_hit(transcript, 2) == 4
    assert turn_at_level_hit(transcript, 3) is None


def test_detect_friction():
    transcript = [
        {"turn_id": 1, "role": "assistant", "content": "您想买什么车？"},
        {"turn_id": 2, "role": "user",      "content": "嗯。"},
        {"turn_id": 3, "role": "assistant", "content": "请问什么车呢？"},
        {"turn_id": 4, "role": "user",      "content": "看看。"},
    ]
    f = detect_friction(transcript)
    assert f[1] == 1   # 车型问了 2 次 → friction
    assert f[2] == 0


def test_product_funnel_shape():
    df = pd.DataFrame({
        "Duration (seconds)": [10, 0, 20, 30],
        "Hangup Reason": ["USER_HANGUP", "NO_ANSWER", "AI_HANGUP", "USER_HANGUP"],
        "Structured Output": [
            '{"购车品牌":"丰田","购车型号":"凯美瑞","购车城市":"上海","购车时间":"6月","购车姓名":"张先生"}',
            'null',
            '{"购车品牌":"奔驰","购车型号":"E300","购车城市":"上海"}',
            '{"购车品牌":"奔驰"}',
        ],
    })
    funnel = compute_product_funnel(df)
    layers = dict(funnel["layers"])
    assert layers["拨打"] == 4
    assert layers["接听"] == 3
    assert layers["真人"] == 3      # 3 个 USER/AI_HANGUP
    assert layers["过车型关"] == 2   # 2 个有品牌+型号
    assert layers["过城市关"] == 2
    assert layers["过时间关"] == 1
    assert layers["全关通过"] == 1


def test_agent_radar_smoke():
    """完整 radar 计算不报错 + 字段齐全。"""
    df = pd.DataFrame({
        "Agent Name": ["A"] * 5,
        "Duration (seconds)": [10, 20, 30, 40, 50],
        "Hangup Reason": ["USER_HANGUP"] * 5,
        "Structured Output": [
            '{"购车品牌":"丰田","购车型号":"凯美瑞","购车城市":"上海","购车时间":"6月","购车姓名":"张先生"}',
            '{"购车品牌":"奔驰","购车型号":"E300","购车城市":"广州","购车时间":"近期","购车姓名":"李"}',
            '{"购车品牌":"奔驰"}',
            '{}',
            '{"购车品牌":"宝马","购车型号":"3系","购车城市":"北京","购车时间":"年底","购车姓名":"王先生"}',
        ],
        "Transcript": [
            '[{"turn_id":1,"role":"user","content":"丰田凯美瑞上海6月张先生"}]',
            '[{"turn_id":1,"role":"user","content":"奔驰E300广州李"}]',
            '[]',
            '[]',
            '[{"turn_id":1,"role":"user","content":"宝马3系北京王"}]',
        ],
    })
    r = compute_agent_radar(df)
    for k in ("击穿率", "轮效", "首杀", "滑顺", "不偏科", "抗挂", "综合分"):
        assert 0 <= r[k] <= 100, f"{k} 越界: {r[k]}"
    assert r["_raw"]["n_human"] == 5
    assert r["_raw"]["n_full"] == 3   # 3 个全过的


def test_ranking_sort():
    df = pd.DataFrame({
        "Agent Name": ["A", "A", "B"],
        "Duration (seconds)": [10, 20, 30],
        "Hangup Reason": ["USER_HANGUP"] * 3,
        "Structured Output": [
            '{"购车品牌":"丰田","购车型号":"凯美瑞","购车城市":"上海","购车时间":"6月","购车姓名":"张"}',
            '{"购车品牌":"奔驰","购车型号":"E300","购车城市":"上海","购车时间":"6月","购车姓名":"李"}',
            '{"购车品牌":"奔驰"}',  # B 一关都没过（型号缺）
        ],
        "Transcript": ['[]'] * 3,
    })
    rank = compute_agent_ranking(df)
    assert rank[0]["agent"] == "A"   # 全过的 A 排第一
    assert rank[1]["agent"] == "B"


# 单文件直接跑
if __name__ == "__main__":
    from .agent_kda import _val_filled  # noqa: F401 (used by test_null_string_treated_as_null)

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
