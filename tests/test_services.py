from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
import services
from ai_client import _parse_json


class YanyanMvpTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["AI_DISABLED"] = "1"
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DATA_DIR = Path(self.temp_dir.name)
        db.UPLOAD_DIR = db.DATA_DIR / "uploads"
        db.DB_PATH = db.DATA_DIR / "test.db"
        services.UPLOAD_DIR = db.UPLOAD_DIR
        db.init_db()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        os.environ.pop("AI_DISABLED", None)

    def test_full_process_generates_exactly_four_types(self) -> None:
        session_id = services.create_session("全流程面试", "计算机", {}, "")
        rows = services.generate_session_questions(session_id)
        self.assertEqual(4, len(rows))
        self.assertEqual(set(services.INTERVIEW_TYPES), {row["interview_type"] for row in rows})

    def test_professional_material_beats_system_bank(self) -> None:
        session_id = services.create_session("专业面", "计算机", {"专业面": 2}, "")
        services.save_material(
            session_id=session_id,
            material_type="专业资料",
            file_name="机器学习笔记.txt",
            data="支持向量机的间隔最大化原理\n神经网络的反向传播算法".encode("utf-8"),
        )
        rows = services.generate_session_questions(session_id)
        self.assertTrue(all(row["source_type"] == "专业资料" for row in rows))

    def test_school_exam_has_highest_priority_for_every_type(self) -> None:
        session_id = services.create_session("全流程面试", "会计学", {}, "")
        services.save_material(session_id=session_id, material_type="简历", file_name="简历.txt", data=b"project")
        services.save_material(session_id=session_id, material_type="专业资料", file_name="笔记.txt", data=b"accounting")
        services.save_material(
            session_id=session_id,
            material_type="院校面试真题",
            file_name="真题.txt",
            data="请介绍你的研究计划？\n为什么选择本校？".encode("utf-8"),
        )
        rows = services.generate_session_questions(session_id)
        self.assertTrue(all(row["source_type"] == "院校面试真题" for row in rows))

    def test_resume_does_not_override_professional_system_bank(self) -> None:
        session_id = services.create_session("专业面", "会计学", {"专业面": 1}, "")
        services.save_material(session_id=session_id, material_type="简历", file_name="简历.txt", data=b"project")
        rows = services.generate_session_questions(session_id)
        self.assertEqual("系统题库", rows[0]["source_type"])

    def test_answer_and_report_round_trip(self) -> None:
        session_id = services.create_session("科研面", "人工智能", {"科研面": 1}, "")
        question = services.generate_session_questions(session_id)[0]
        result = services.submit_answer(
            session_id,
            question["session_question_id"],
            "首先我负责数据清洗与模型评估；其次通过消融实验验证方法，最后准确率提升了8%。",
        )
        services.finish_session(session_id)
        report = services.get_report(session_id)
        self.assertGreater(result["overall_score"], 50)
        self.assertEqual(1, report["summary"]["answered_questions"])
        self.assertEqual("completed", report["session"]["status"])

    def test_short_answer_creates_one_followup(self) -> None:
        session_id = services.create_session("科研面", "人工智能", {"科研面": 1}, "")
        question = services.generate_session_questions(session_id)[0]
        evaluation = services.submit_answer(session_id, question["session_question_id"], "我做了一个项目。")
        followup_id = services.maybe_generate_followup(
            session_id, question["session_question_id"], "我做了一个项目。", evaluation
        )
        rows = services.get_session_questions(session_id)
        self.assertIsNotNone(followup_id)
        self.assertEqual(2, len(rows))
        self.assertEqual(1, rows[1]["is_followup"])
        second_attempt = services.maybe_generate_followup(
            session_id, question["session_question_id"], "我做了一个项目。", evaluation
        )
        self.assertIsNone(second_attempt)

    def test_deepseek_material_question_is_saved(self) -> None:
        session_id = services.create_session("科研面", "人工智能", {"科研面": 1}, "关注实验设计")
        services.save_material(
            session_id=session_id,
            material_type="简历",
            file_name="简历.txt",
            data="我负责医学影像分割项目的数据清洗与消融实验。".encode("utf-8"),
        )
        with patch.object(services, "generate_interview_question", return_value="你如何设计消融实验以验证各模块贡献？"):
            rows = services.generate_session_questions(session_id)
        self.assertEqual("deepseek", rows[0]["generation_method"])
        self.assertIn("消融实验", rows[0]["question_text"])

    def test_deepseek_answer_evaluation_is_saved(self) -> None:
        session_id = services.create_session("科研面", "人工智能", {"科研面": 1}, "")
        question = services.generate_session_questions(session_id)[0]
        ai_result = {
            "logic_score": 81.0,
            "completeness_score": 78.0,
            "accuracy_score": 84.0,
            "clarity_score": 82.0,
            "response_score": 79.0,
            "overall_score": 80.8,
            "weak_dimension": "完整度",
            "diagnosis": "回答说明了实验过程，但没有交代你的个人职责边界。",
            "suggestion": "补充你独立负责的步骤，并给出一个量化结果。",
            "reference_structure": "研究目标、个人职责、关键行动、实验结果、复盘。",
            "evaluation_method": "deepseek",
        }
        with patch.object(services, "analyze_interview_answer", return_value=ai_result):
            result = services.submit_answer(session_id, question["session_question_id"], "我完成了实验。")
        report = services.get_report(session_id)
        self.assertEqual("deepseek", result["evaluation_method"])
        self.assertEqual("deepseek", report["details"][0]["evaluation_method"])

    def test_identical_answer_reuses_saved_evaluation(self) -> None:
        session_id = services.create_session("科研面", "人工智能", {"科研面": 1}, "")
        question = services.generate_session_questions(session_id)[0]
        answer = "我负责数据处理，并通过消融实验验证不同模块的贡献。"
        first = services.submit_answer(session_id, question["session_question_id"], answer)
        with patch.object(services, "analyze_interview_answer", side_effect=AssertionError("不应重复调用 API")):
            second = services.submit_answer(session_id, question["session_question_id"], answer)
        self.assertEqual(first["overall_score"], second["overall_score"])

    def test_json_parser_accepts_code_fence_and_explanation(self) -> None:
        payload = _parse_json('结果如下：\n```json\n{"ok": true}\n```')
        self.assertIs(payload["ok"], True)


if __name__ == "__main__":
    unittest.main()
