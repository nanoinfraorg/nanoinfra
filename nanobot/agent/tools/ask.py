"""Tool for pausing a turn until the user answers."""

from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import ArraySchema, StringSchema, tool_parameters_schema


class AskUserInterrupt(BaseException):
    """Internal signal: the runner should stop and wait for user input."""

    def __init__(self, question: str, options: list[str] | None = None) -> None:
        self.question = question
        self.options = [str(option) for option in (options or []) if str(option)]
        super().__init__(question)


@tool_parameters(
    tool_parameters_schema(
        question=StringSchema(
            "The question to ask before continuing. Use this only when the task needs the user's answer."
        ),
        options=ArraySchema(
            StringSchema("A possible answer label"),
            description="Optional choices. The user may still reply with free text.",
        ),
        required=["question"],
    )
)
class AskUserTool(Tool):
    """Ask the user a blocking question."""

    @property
    def name(self) -> str:
        return "ask_user"

    @property
    def description(self) -> str:
        return (
            "Pause and ask the user a question when their answer is required to continue. "
            "Use options for likely answers; the user's reply, typed or selected, is returned as the tool result. "
            "For non-blocking notifications or buttons, use the message tool instead."
        )

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, question: str, options: list[str] | None = None, **_: Any) -> Any:
        raise AskUserInterrupt(question=question, options=options)
