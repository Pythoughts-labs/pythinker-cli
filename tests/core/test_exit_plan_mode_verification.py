from pythinker_code.tools.plan.__init__ import _plan_lacks_verification_section


def test_plan_lacks_verification_section_detects_missing_heading() -> None:
    assert _plan_lacks_verification_section("## Plan\nDo the thing.\n")
    assert not _plan_lacks_verification_section("## Plan\n## Verification\nmake test\n")


# The ExitPlanMode description assertion lives in
# tests/tools/test_tool_descriptions.py::test_exit_plan_mode_description_requires_verification_section
# (it checks both the "Verification section" heading and the "smallest command,
# test, or check" guidance). This file is scoped to the _plan_lacks_verification_section helper.
