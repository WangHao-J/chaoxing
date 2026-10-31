"""Offline tests for card-page recognition and explicit boolean values."""
import json
import unittest
from unittest.mock import Mock

from card_reader import flag_is_true, parse_course_card, has_card_data_assignment


class CardReaderTests(unittest.TestCase):
    def setUp(self):
        self.legacy = Mock(return_value=([], {}))
        self.extract = Mock(side_effect=lambda data: dict(data.get("defaults") or {}))
        self.process = Mock(side_effect=lambda cards: list(cards))
        self.card = {"type": "workid", "job": True,
                     "property": {"title": "Campus related words test"}}

    def parse(self, html):
        return parse_course_card(html, self.extract, self.process, self.legacy)

    def test_simple_three_argument_interface_needs_no_legacy_decoder(self):
        self.assertEqual(parse_course_card("<html>unknown</html>", self.extract, self.process),
                         ([], {}, False))

    def html(self, **changes):
        data = {"attachments": [self.card], "defaults": {"knowledgeid": "chapter"}, **changes}
        return "<script>var mArg = " + json.dumps(data, ensure_ascii=False, indent=2) + ";</script>"

    def test_multiline_json_and_tabs_around_assignment_are_recognized(self):
        html = self.html().replace("mArg = ", "mArg\t=\r\n\t")
        jobs, info, recognized = self.parse(html)
        self.assertTrue(recognized)
        self.assertEqual(jobs, [self.card])
        self.assertEqual(info, {"knowledgeid": "chapter"})
        self.legacy.assert_not_called()

    def test_spaces_inside_titles_and_nested_properties_are_preserved(self):
        self.card["property"]["title"] = "Campus related words test"
        self.card["property"]["href"] = "https://example.invalid/watch?q=two words"
        jobs, _, recognized = self.parse(self.html())
        self.assertTrue(recognized)
        self.assertEqual(jobs[0]["property"], self.card["property"])

    def test_json_string_with_object_terminator_is_not_truncated(self):
        self.card["property"]["title"] = 'Read }; then "answer" and café'
        jobs, _, recognized = self.parse(self.html())
        self.assertTrue(recognized)
        self.assertEqual(jobs[0]["property"]["title"], self.card["property"]["title"])

    def test_legacy_compact_assignment_still_works(self):
        jobs, _, recognized = self.parse("mArg=" + json.dumps({"attachments": [self.card]}) + ";")
        self.assertTrue(recognized)
        self.assertEqual(jobs, [self.card])

    def test_window_property_assignment_is_recognized(self):
        _, info, recognized = self.parse(self.html().replace("var mArg", "window.mArg"))
        self.assertTrue(recognized)
        self.assertEqual(info["knowledgeid"], "chapter")

    def test_official_empty_string_initializer_does_not_hide_real_card_object(self):
        jobs, info, recognized = self.parse('<script>var mArg = "";</script>' + self.html())
        self.assertTrue(recognized)
        self.assertEqual(jobs, [self.card])
        self.assertEqual(info["knowledgeid"], "chapter")

    def test_empty_object_initializer_is_replaced_by_actual_cards(self):
        jobs, _, recognized = self.parse('var mArg={};' + self.html())
        self.assertTrue(recognized)
        self.assertEqual(jobs, [self.card])

    def test_parsejson_scaffolding_does_not_discard_real_json(self):
        jobs, _, recognized = self.parse('var mArg="";' + self.html() + 'mArg = $.parseJSON(mArg);')
        self.assertTrue(recognized)
        self.assertEqual(jobs, [self.card])

    def test_empty_unused_template_slot_is_unrecognized_without_real_task_data(self):
        html = '<script>var mArg=""; if(typeof mArg=="string"){mArg = $.parseJSON(mArg);}</script>'
        self.assertEqual(self.parse(html), ([], {}, False))
        self.assertFalse(has_card_data_assignment(html))

    def test_initialization_does_not_hide_malformed_real_card(self):
        html = 'var mArg=""; mArg={broken};'
        self.assertEqual(self.parse(html), ([], {}, False))
        self.assertTrue(has_card_data_assignment(html))

    def test_malformed_real_card_after_empty_object_is_not_a_completed_empty_page(self):
        self.assertEqual(self.parse('var mArg={}; mArg={broken};'), ([], {}, False))

    def test_fake_assignment_inside_title_is_not_executed_or_read(self):
        self.card["property"]["title"] = 'mArg={"attachments":[]};'
        jobs, _, recognized = self.parse(self.html())
        self.assertTrue(recognized)
        self.assertEqual(jobs, [self.card])

    def test_invalid_real_card_arrays_remain_diagnostic_failures(self):
        for literal in ('[]', '[null]', '{"attachments":{}}'):
            with self.subTest(literal=literal):
                html = 'var mArg=""; mArg=' + literal + ';'
                self.assertEqual(self.parse(html), ([], {}, False))
                self.assertTrue(has_card_data_assignment(html))

    def test_not_open_retains_existing_behavior_without_guessing_cards(self):
        self.assertEqual(self.parse("<div>该章节未开放</div>"), ([], {"notOpen": True}, True))
        self.process.assert_not_called()
        self.legacy.assert_not_called()

    def test_login_html_remains_unrecognized_and_does_not_process_cards(self):
        self.assertEqual(self.parse("<html><form>登录学习通</form></html>"), ([], {}, False))
        self.legacy.assert_called_once()
        self.process.assert_not_called()

    def test_malformed_json_uses_compatible_fallback_with_failure_diagnostic(self):
        legacy_jobs = [{"type": "manual", "name": "legacy recognized task"}]
        self.legacy.return_value = (legacy_jobs, {"legacy": True})
        self.assertEqual(self.parse('mArg={"attachments":['), (legacy_jobs, {"legacy": True}, False))
        self.process.assert_not_called()

    def test_malformed_response_with_legacy_exception_remains_unrecognized(self):
        self.legacy.side_effect = json.JSONDecodeError("invalid", "x", 0)
        self.assertEqual(self.parse("mArg={ broken };"), ([], {}, False))

    def test_non_object_json_and_invalid_card_shapes_are_unrecognized(self):
        for value in (None, [], 7, {"attachments": None}, {"attachments": {}},
                      {"attachments": ["workid"]}, {"defaults": []}):
            with self.subTest(value=value):
                self.assertEqual(self.parse("mArg=" + json.dumps(value) + ";"), ([], {}, False))
        self.process.assert_not_called()

    def test_empty_attachments_can_be_recognized_without_claiming_completion(self):
        self.assertEqual(self.parse(self.html(attachments=[])),
                         ([], {"knowledgeid": "chapter"}, True))

    def test_empty_object_retains_legacy_empty_result_and_recognition(self):
        self.assertEqual(self.parse("mArg={};"), ([], {}, True))

    def test_unexpected_legacy_result_is_unrecognized(self):
        self.legacy.return_value = (None, None)
        self.assertEqual(self.parse("<html>unknown</html>"), ([], {}, False))

    def test_card_parser_shape_errors_do_not_become_recognized_empty_pages(self):
        self.process.side_effect = KeyError("missing required property")
        self.assertEqual(self.parse(self.html()), ([], {}, False))

    def test_explicit_true_values_only(self):
        for value in (True, 1, "true", "TRUE", "  true  ", "1"):
            with self.subTest(value=value):
                self.assertTrue(flag_is_true(value))
        for value in (False, 0, "false", "False", "0", "", None, [], {}, "yes", 2, 1.0):
            with self.subTest(value=value):
                self.assertFalse(flag_is_true(value))


if __name__ == "__main__":
    unittest.main()
