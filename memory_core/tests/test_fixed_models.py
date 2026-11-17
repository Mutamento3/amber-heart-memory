import json
import unittest

from memory_core.testing.fixed_models import FixedModels, MissingFixture


class FixedModelTests(unittest.TestCase):
    def test_all_roles_fixed_and_unknown_input_fails(self):
        request = json.dumps(["合成查询", [7, 9]], ensure_ascii=False)
        models = FixedModels({
            "extract": {"合成材料": '{"experience_mode":"external"}'},
            "embed": {"合成查询": [1.0, 0.0]}, "clean_query": {"合成查询": "查询"},
            "rerank": {request: [1, 0]}, "select": {request: "2,1"},
        })
        self.assertIn("external", models.extract("合成材料"))
        self.assertEqual(models.embed("合成查询"), [1, 0])
        self.assertEqual(models.clean_query("合成查询"), "查询")
        self.assertEqual(models.rerank("合成查询", [7, 9]), [1, 0])
        self.assertEqual(models.select("合成查询", [7, 9]), "2,1")
        with self.assertRaises(MissingFixture): models.embed("未声明的请求")
        vector = models.embed("合成查询"); vector[0] = 123
        self.assertEqual(models.embed("合成查询"), [1, 0])

    def test_fixed_failure_and_invalid_output_are_not_hidden(self):
        models = FixedModels({"extract": {"空": "", "坏": "{", "慢": {"error": "timeout"}}})
        self.assertEqual(models.extract("空"), "")
        self.assertEqual(models.extract("坏"), "{")
        with self.assertRaises(TimeoutError): models.extract("慢")
