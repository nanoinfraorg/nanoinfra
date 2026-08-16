# tests/gates/suspension_wait.py
"""One suspension wait for every gate harness -- nanoinfraorg/nanoinfra#82.

Three harnesses held three copies of this wait, and every copy carried the same two defects.
One copy is what the copies were: a fix reached one file and left two behind, and the file that
failed on CI was not the file somebody had last read.

**This is a sibling module and not the conftest, and not an import of the conftest.** ``tests``
holds no ``__init__.py``, so ``from tests.gates.conftest import ...`` raises
``ModuleNotFoundError`` — that mistake has now cost three separate lanes in this repository. A
fixture is the usual answer, and it does not fit here: the caller is a method of a harness class
rather than a test, so it can take no fixture. pytest adds the directory of a test file to
``sys.path`` when that directory is not a package, so a sibling module imports by bare name.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

#: How long one suspension may take. The work includes a real ansible-inventory subprocess for a
#: group scope, and this number only bounds a machine that is slow. A refusal reports itself at
#: once, so the budget never delays a real failure.
SUSPEND_BUDGET_S = 30.0


def _finished_task_answer(task: "asyncio.Task[Any]") -> str:
    """What a finished handle call answered, for a wait that expected a suspension."""
    if task.cancelled():
        return "the call was cancelled"
    error = task.exception()
    if error is not None:
        return f"{type(error).__name__}: {error}"
    return repr(task.result())


async def wait_for_one_pending(
    pending: Any,
    timeout_s: float | None = None,
    task: "asyncio.Task[Any] | None" = None,
) -> Any:
    """Wait until the executor suspends one action, then return that record (#82).

    Three test harnesses held their own copy of this wait, and every copy carried the same two
    defects. One copy is what the copies were: a fix reached one file and left two behind, and
    the file that failed on CI was not the file somebody had last read.

    *task* is the ``handle`` call this wait belongs to, and passing it changes what a failure
    teaches. An action that **refused** instead of suspending finishes that task, and this wait
    then reports the refusal at once. Without it the same run waited out the whole budget and
    reported "never suspended", which names the symptom and hides the cause.

    That is also why the budget is generous rather than tight. A real refusal fails immediately,
    so a large budget delays no real failure. It only stops a slow machine from reading as a
    broken gate. The old budget was 5 seconds, and one group action measured 3.4 of them under
    coverage on a machine faster than the CI runner, which made that pass a coin flip.

    *pending* is the store rather than the harness, because three harnesses have three shapes and
    all three hold one store.
    """
    deadline = time.monotonic() + (SUSPEND_BUDGET_S if timeout_s is None else timeout_s)
    while time.monotonic() < deadline:
        items = pending.pending()
        if items:
            return items[0]
        if task is not None and task.done():
            raise AssertionError(
                "the executor answered instead of suspending the action: "
                + _finished_task_answer(task)
            )
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"the executor never suspended an action within {deadline}s. A group action resolves its "
        "host set through a real ansible-inventory subprocess, so a slow machine needs the "
        "budget, and a refusal reports itself at once when the caller passes its task."
    )
