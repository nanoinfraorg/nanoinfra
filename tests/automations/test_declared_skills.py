"""An automation can say which skills its prompt carries.

A real job hardcoded `curl https://api.github.com/repos/.../issues?state=open` in its prose while
`nanoinfra/skills/github/SKILL.md` existed and nothing in the UI mentioned it
(nanoinfraorg/nanoinfra#160).

Worth stating plainly, because the spec overstated it: a skill is prompt content, not a capability.
Declaring skills narrows what the model is *told about*. It is focus and discovery, not a security
boundary, and none of these tests pretend otherwise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanoinfra.session.automation_turns import (
    AUTOMATION_SKILLS_META,
    automation_declared_skills,
)

# --- reading the declaration ---


def test_no_declaration_means_the_whole_catalogue() -> None:
    assert automation_declared_skills({}) is None
    assert automation_declared_skills(None) is None


def test_a_declaration_round_trips() -> None:
    assert automation_declared_skills({AUTOMATION_SKILLS_META: ["github", "cron"]}) == [
        "github",
        "cron",
    ]


def test_an_empty_declaration_reads_as_no_declaration() -> None:
    """None and [] mean different things to the prompt builder, and "show nothing" is a footgun
    no automation record can express."""
    assert automation_declared_skills({AUTOMATION_SKILLS_META: []}) is None
    assert automation_declared_skills({AUTOMATION_SKILLS_META: ["", "  "]}) is None


@pytest.mark.parametrize("value", ["github", 7, {"github": True}, None])
def test_a_non_list_declaration_is_ignored(value: object) -> None:
    assert automation_declared_skills({AUTOMATION_SKILLS_META: value}) is None


def test_names_are_trimmed_and_blanks_dropped() -> None:
    assert automation_declared_skills({AUTOMATION_SKILLS_META: [" github ", "", "cron"]}) == [
        "github",
        "cron",
    ]


# --- the prompt ---


def _context(tmp_path: Path):
    from nanoinfra.agent.context import ContextBuilder

    workspace = tmp_path / "workspace"
    skills_dir = workspace / "skills"
    for name, description in (
        ("alpha", "Does alpha things"),
        ("beta", "Does beta things"),
    ):
        directory = skills_dir / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\nBody of {name}.\n",
            encoding="utf-8",
        )
    return ContextBuilder(workspace)


def test_without_a_declaration_the_catalogue_is_summarised(tmp_path: Path) -> None:
    context = _context(tmp_path)

    prompt = context.build_system_prompt()

    assert "alpha" in prompt
    assert "beta" in prompt


def test_a_declaration_loads_the_named_skill_in_full(tmp_path: Path) -> None:
    context = _context(tmp_path)

    prompt = context.build_system_prompt(declared_skills=["alpha"])

    assert "Body of alpha." in prompt


def test_a_declaration_drops_the_rest_of_the_catalogue(tmp_path: Path) -> None:
    """The point is a focused prompt: this turn sees what it declared, not everything."""
    context = _context(tmp_path)

    prompt = context.build_system_prompt(declared_skills=["alpha"])

    assert "Does beta things" not in prompt


def test_an_undeclared_skill_is_not_loaded_in_full(tmp_path: Path) -> None:
    context = _context(tmp_path)

    prompt = context.build_system_prompt(declared_skills=["alpha"])

    assert "Body of beta." not in prompt


# --- the records ---


def test_a_cron_job_carries_its_declaration_into_the_turn(tmp_path: Path) -> None:
    from nanoinfra.cron.service import CronService
    from nanoinfra.cron.types import CronSchedule

    service = CronService(tmp_path / "cron" / "jobs.json")
    job = service.add_job(
        name="Blockers",
        schedule=CronSchedule(kind="every", every_ms=3_600_000),
        message="Check blockers",
        session_key="websocket:chat-1",
        origin_channel="websocket",
        origin_chat_id="chat-1",
        skills=["github"],
    )

    assert job.skills == ["github"]
    reloaded = CronService(tmp_path / "cron" / "jobs.json")
    reloaded._load_store()
    stored = reloaded.get_job(job.id)
    assert stored is not None
    assert stored.skills == ["github"]


def test_a_job_written_before_declarations_existed_has_none(tmp_path: Path) -> None:
    import json

    from nanoinfra.cron.service import CronService

    store_path = tmp_path / "cron" / "jobs.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "legacy",
                        "name": "legacy",
                        "enabled": True,
                        "schedule": {"kind": "every", "everyMs": 3_600_000},
                        "payload": {
                            "kind": "agent_turn",
                            "message": "hello",
                            "sessionKey": "websocket:chat-1",
                            "originChannel": "websocket",
                            "originChatId": "chat-1",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    service = CronService(store_path)
    service._load_store()
    stored = service.get_job("legacy")

    assert stored is not None
    assert stored.skills == []


def test_a_trigger_declaration_round_trips(tmp_path: Path) -> None:
    from nanoinfra.triggers.local_store import LocalTriggerStore

    store = LocalTriggerStore(tmp_path)
    trigger = store.create(
        name="CI review",
        channel="websocket",
        chat_id="chat-1",
        session_key="websocket:chat-1",
    )

    assert store.update(trigger.id, skills=["github"]) is not None
    reloaded = LocalTriggerStore(tmp_path).get(trigger.id)
    assert reloaded is not None
    assert reloaded.skills == ["github"]
