# -*- coding: utf-8 -*-
"""意图规则 / 中文数字归一化回归测试。

运行方式（容器内，避免本机缺依赖）：
    docker cp bridge.py xiaoai-ha-bridge:/tmp/t/bridge.py
    docker cp tests xiaoai-ha-bridge:/tmp/t/tests
    docker exec -w /tmp/t xiaoai-ha-bridge python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


class TestChineseNumerals(unittest.TestCase):
    def test_normalize_basic(self):
        cases = {
            "把热水器调到四十二度": "把热水器调到42度",
            "把热水器调到42度": "把热水器调到42度",
            "把空调调到二十六度": "把空调调到26度",
            "把燃气热水器调到四十二度": "把燃气热水器调到42度",
            "把温度设置为一百二十度": "把温度设置为120度",
            "音量调到三十五": "音量调到35",
            "两分钟后关闭": "2分钟后关闭",
            "定时七十五分钟": "定时75分钟",
            "一千零五十元": "1050元",
            "一万二千元": "12000元",
            "打开第十台设备": "打开第10台设备",
        }
        for src, expect in cases.items():
            self.assertEqual(bridge._normalize_chinese_numerals(src), expect, src)

    def test_normalize_percent_and_decimal(self):
        self.assertEqual(
            bridge._normalize_chinese_numerals("亮度调到百分之五十"),
            "亮度调到百分之50")
        self.assertEqual(
            bridge._normalize_chinese_numerals("千分之五"), "千分之5")
        self.assertEqual(
            bridge._normalize_chinese_numerals("把温度设到一点五度"),
            "把温度设到1.5度")
        self.assertEqual(
            bridge._normalize_chinese_numerals("把温度设到二十三点五度"),
            "把温度设到23.5度")
        self.assertEqual(
            bridge._normalize_chinese_numerals("把温度设到零点五度"),
            "把温度设到0.5度")

    def test_no_false_positive(self):
        """设备名/日常词里的中文数字不能被改。"""
        for src in ("打开二楼灯", "我们一起走吧", "万一失败了呢",
                    "十分满意", "现在三点一刻", "七点半打开"):
            self.assertEqual(bridge._normalize_chinese_numerals(src), src, src)


RULES = [
    {
        "pattern": "把(?:燃气)?热水器(?:的)?(?:水温|温度|出水温度)?"
                   "(?:调到|设为|设置为|设置到|设到|设成|改为|改成|调成){num}度",
        "action": {
            "domain": "water_heater", "service": "set_temperature",
            "entity_id": "water_heater.real", "temperature": "{0}",
            "reply": "把温度调整到{0}度",
        },
    },
    {
        "pattern": "(?:把)?亮度调(?:到|成)(?:百分之)?{num}[%％]?",
        "action": {
            "domain": "light", "service": "turn_on",
            "entity_id": "light.real", "brightness_pct": "{0}",
            "reply": "亮度已调到{0}%",
        },
    },
    {
        "pattern": "音量调到{num}",
        "action": {
            "domain": "media_player", "service": "volume_set",
            "entity_id": "media_player.real", "volume_level": "{0}",
            "reply": "音量{0}",
        },
    },
    {
        "pattern": "空调温度设为{num}度",
        "action": {
            "domain": "climate", "service": "set_temperature",
            "entity_id": "climate.real", "temperature": "{0}",
            "reply": "空调设为{0}度",
        },
    },
    {
        "pattern": "{num}分钟后关闭热水器",
        "action": {
            "domain": "water_heater", "service": "turn_off",
            "entity_id": "water_heater.real", "delay_minutes": "{0}",
            "reply": "好的",
        },
    },
]


class TestIntentParserChineseNumerals(unittest.TestCase):
    def setUp(self):
        self.parser = bridge.IntentParser(RULES)

    def test_reported_case_chinese(self):
        action = self.parser.parse("把热水器调到四十二度")
        self.assertIsNotNone(action)
        self.assertEqual(action["temperature"], 42)
        self.assertIsInstance(action["temperature"], int)
        self.assertEqual(action["entity_id"], "water_heater.real")
        self.assertEqual(action["reply"], "把温度调整到42度")

    def test_reported_case_arabic(self):
        action = self.parser.parse("把热水器调到42度")
        self.assertIsNotNone(action)
        self.assertEqual(action["temperature"], 42)

    def test_more_chinese_cases(self):
        self.assertEqual(self.parser.parse("把燃气热水器的水温设置为一百二十度")["temperature"], 120)
        self.assertEqual(self.parser.parse("把热水器调到二十三点五度")["temperature"], 23.5)
        self.assertEqual(self.parser.parse("亮度调到百分之五十")["brightness_pct"], 50)
        self.assertEqual(self.parser.parse("音量调到三十五")["volume_level"], 35)
        self.assertEqual(self.parser.parse("空调温度设为二十六度")["temperature"], 26)
        self.assertEqual(self.parser.parse("三十分钟后关闭热水器")["delay_minutes"], 30)

    def test_literal_chinese_pattern_still_works(self):
        parser = bridge.IntentParser([{
            "pattern": "调到四十二度",
            "action": {"domain": "climate", "service": "set_temperature",
                       "entity_id": "climate.literal", "temperature": 42,
                       "reply": "好"},
        }])
        action = parser.parse("调到四十二度")
        self.assertIsNotNone(action)
        self.assertEqual(action["entity_id"], "climate.literal")

    def test_device_name_not_matched(self):
        self.assertIsNone(self.parser.parse("打开二楼灯"))


if __name__ == "__main__":
    unittest.main()
