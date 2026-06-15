from pythinker_code.tools.plan import ExitPlanMode
from pythinker_code.tools.plan.__init__ import _plan_lacks_verification_section


def test_plan_lacks_verification_section_detects_missing_heading() -> None:
    assert _plan_lacks_verification_section("## Plan\nDo the thing.\n")
    assert not _plan_lacks_verification_section("## Plan\n## Verification\nmake test\n")


def test_exit_plan_mode_description_requires_verification_section():
    tool = ExitPlanMode()
    assert "Verification section" in tool.base.description
