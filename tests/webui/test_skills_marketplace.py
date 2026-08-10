import io
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from nanoinfra.webui.skills_marketplace import (
    SkillsMarketplaceError,
    _validated_zip_entries,
    install_marketplace_skill,
    marketplace_skill_trends,
    search_marketplace_skills,
    trending_marketplace_skills,
)

_NANOINFRA_BASE_URL = "https://skills.nanoinfra.org"


@pytest.mark.asyncio
async def test_search_marketplace_skills_filters_and_marks_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_dir = tmp_path / "skills" / "react-testing"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: react-testing\n---\n", encoding="utf-8")
    seen: dict[str, Any] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return {
                "skills": [
                    {
                        "name": "React Testing",
                        "skillId": "react-testing",
                        "source": "acme/agent-skills",
                        "installs": 42,
                    },
                    {"skillId": "../escape", "source": "acme/agent-skills"},
                    {"skillId": "valid-name", "source": "not-a-repository"},
                ]
            }

    class FakeClient:
        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def get(self, url: str, *, params: dict[str, object]) -> FakeResponse:
            seen.update(url=url, params=params)
            return FakeResponse()

    monkeypatch.setattr(
        "nanoinfra.webui.skills_marketplace.httpx.AsyncClient",
        lambda **_kwargs: FakeClient(),
    )
    monkeypatch.setattr(
        "nanoinfra.webui.skills_marketplace.skills_install_supported",
        lambda: True,
    )
    payload = await search_marketplace_skills(
        "  react   testing  ",
        tmp_path,
        provider="skills_sh",
    )

    assert seen == {
        "url": "https://skills.sh/api/search",
        "params": {"q": "react testing", "limit": 20},
    }
    assert payload == {
        "query": "react testing",
        "provider": "skills_sh",
        "install_supported": True,
        "skills": [
            {
                "id": "acme/agent-skills/react-testing",
                "skill_id": "react-testing",
                "name": "React Testing",
                "source": "acme/agent-skills",
                "provider": "skills_sh",
                "installs": 42,
                "url": "https://skills.sh/acme/agent-skills/react-testing",
                "installed": True,
                "install_supported": True,
                "metric": "installs_total",
            }
        ],
    }


@pytest.mark.asyncio
async def test_search_nanoinfra_skills_normalizes_provider_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return {
                "query": "linux",
                "skills": [
                    {
                        "skill_id": "linux-commands",
                        "display_name": "linux-commands",
                        "description": "Reference guide for common Linux shell commands.",
                        "current_version": 1,
                        "status": "published",
                        "published_at": "2026-08-10T20:25:39Z",
                        "github_path": "linux-commands/",
                        "downloads": 3,
                        "created_at": "2026-08-10T20:25:16Z",
                    }
                ],
            }

    class FakeClient:
        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def get(self, url: str, *, params: dict[str, object]) -> FakeResponse:
            seen.update(url=url, params=params)
            return FakeResponse()

    monkeypatch.setattr(
        "nanoinfra.webui.skills_marketplace.httpx.AsyncClient",
        lambda **_kwargs: FakeClient(),
    )

    payload = await search_marketplace_skills(
        " linux ",
        tmp_path,
        provider="nanoinfra",
        nanoinfra_base_url=_NANOINFRA_BASE_URL,
    )

    assert seen == {
        "url": "/api/v1/search",
        "params": {"q": "linux"},
    }
    assert payload["provider"] == "nanoinfra"
    assert payload["skills"] == [
        {
            "id": "nanoinfra:linux-commands",
            "skill_id": "linux-commands",
            "name": "linux-commands",
            "source": "nanoinfra",
            "provider": "nanoinfra",
            "installs": 3,
            "url": "https://skills.nanoinfra.org/skills/linux-commands",
            "installed": False,
            "install_supported": True,
            "metric": "installs_total",
            "version": "1",
        }
    ]


@pytest.mark.asyncio
async def test_trending_marketplace_skills_diversifies_sources_and_keeps_rank(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return {
                "skills": [
                    {
                        "name": "First",
                        "skillId": "first",
                        "source": "acme/skills",
                        "installs": 50,
                    },
                    {
                        "name": "Second from same source",
                        "skillId": "second",
                        "source": "acme/skills",
                        "installs": 49,
                    },
                    {
                        "name": "Another",
                        "skillId": "another",
                        "source": "other/skills",
                        "installs": 30,
                    },
                ]
            }

    class FakeClient:
        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def get(self, url: str) -> FakeResponse:
            assert url == "https://skills.sh/api/skills/trending/0"
            return FakeResponse()

    monkeypatch.setattr(
        "nanoinfra.webui.skills_marketplace.httpx.AsyncClient",
        lambda **_kwargs: FakeClient(),
    )
    payload = await trending_marketplace_skills(tmp_path, provider="skills_sh")

    assert payload["period"] == "24h"
    assert [(skill["name"], skill["rank"]) for skill in payload["skills"]] == [
        ("First", 1),
        ("Another", 3),
    ]


@pytest.mark.asyncio
async def test_marketplace_skill_trends_returns_history_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        text = r"<script>\"values\":[3,5,8,13]</script>"

        def raise_for_status(self) -> None:
            pass

    class FakeClient:
        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def get(self, url: str) -> FakeResponse:
            assert url == "https://www.skills.sh/other/skills/second"
            return FakeResponse()

    async def weekly_installs(_client: object) -> dict[tuple[str, str], list[int]]:
        return {
            ("acme/skills", "first"): [2, 4, 3, 8],
        }

    monkeypatch.setattr(
        "nanoinfra.webui.skills_marketplace.httpx.AsyncClient",
        lambda **_kwargs: FakeClient(),
    )
    monkeypatch.setattr(
        "nanoinfra.webui.skills_marketplace._load_weekly_installs",
        weekly_installs,
    )

    assert await marketplace_skill_trends(
        [
            "acme/skills/first",
            "other/skills/second",
            "invalid",
        ]
    ) == {
        "trends": {
            "acme/skills/first": [2, 4, 3, 8],
            "other/skills/second": [3, 5, 8, 13],
        }
    }


@pytest.mark.asyncio
async def test_search_marketplace_skills_returns_safe_upstream_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingClient:
        async def __aenter__(self) -> "FailingClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def get(self, *_args: object, **_kwargs: object) -> None:
            raise httpx.ConnectError("private network detail")

    monkeypatch.setattr(
        "nanoinfra.webui.skills_marketplace.httpx.AsyncClient",
        lambda **_kwargs: FailingClient(),
    )

    with pytest.raises(SkillsMarketplaceError) as exc_info:
        await search_marketplace_skills("react", tmp_path, provider="skills_sh")

    assert exc_info.value.status == 502
    assert exc_info.value.message == "skills.sh search is temporarily unavailable"


@pytest.mark.asyncio
async def test_install_marketplace_skill_uses_official_cli_and_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, None]:
            skill_dir = tmp_path / "skills" / "react-testing"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: react-testing\n---\n",
                encoding="utf-8",
            )
            return b"installed", None

        def kill(self) -> None:
            raise AssertionError("successful install must not be killed")

    async def create_subprocess_exec(*command: str, **kwargs: object) -> FakeProcess:
        seen.update(command=command, **kwargs)
        return FakeProcess()

    monkeypatch.setattr(
        "nanoinfra.webui.skills_marketplace.shutil.which",
        lambda executable: "/usr/local/bin/npx" if executable == "npx" else None,
    )
    monkeypatch.setattr(
        "nanoinfra.webui.skills_marketplace.asyncio.create_subprocess_exec",
        create_subprocess_exec,
    )

    result = await install_marketplace_skill(
        "acme/agent-skills",
        "react-testing",
        tmp_path,
    )

    assert result == {
        "installed": True,
        "already_installed": False,
        "name": "react-testing",
    }
    assert seen["command"] == (
        "/usr/local/bin/npx",
        "--yes",
        "skills@latest",
        "add",
        "acme/agent-skills",
        "--skill",
        "react-testing",
        "--agent",
        "openclaw",
        "--copy",
        "--yes",
    )
    assert seen["cwd"] == str(tmp_path.resolve())
    assert seen["env"]["DISABLE_TELEMETRY"] == "1"


@pytest.mark.asyncio
async def test_install_nanoinfra_skill_downloads_and_extracts_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_buffer = io.BytesIO()
    skill_content = b"---\nname: linux-commands\ndescription: Linux command reference.\n---\n"
    with zipfile.ZipFile(archive_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("SKILL.md", skill_content)
    archive_bytes = archive_buffer.getvalue()

    class FakeResponse:
        def __init__(self, *, status_code: int = 200, headers: dict[str, str] | None = None) -> None:
            self.status_code = status_code
            self.headers = headers or {}

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise httpx.HTTPStatusError(
                    "failed",
                    request=httpx.Request("GET", "https://example.com"),
                    response=httpx.Response(self.status_code),
                )

        async def __aenter__(self) -> "FakeResponse":
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def aiter_bytes(self):
            yield archive_bytes[:12]
            yield archive_bytes[12:]

    class FakeClient:
        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        def stream(self, method: str, url: str) -> FakeResponse:
            assert method == "GET"
            assert url == "/api/v1/skills/linux-commands/download"
            return FakeResponse(headers={"content-length": str(len(archive_bytes))})

    monkeypatch.setattr(
        "nanoinfra.webui.skills_marketplace.httpx.AsyncClient",
        lambda **_kwargs: FakeClient(),
    )

    result = await install_marketplace_skill(
        "",
        "linux-commands",
        tmp_path,
        provider="nanoinfra",
        nanoinfra_base_url=_NANOINFRA_BASE_URL,
    )

    assert result == {
        "installed": True,
        "already_installed": False,
        "name": "linux-commands",
        "provider": "nanoinfra",
    }
    assert (tmp_path / "skills" / "linux-commands" / "SKILL.md").read_bytes() == skill_content


def test_nanoinfra_archive_rejects_path_traversal() -> None:
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as archive:
        archive.writestr("SKILL.md", "---\nname: safe\n---\n")
        archive.writestr("../outside.sh", "#!/bin/sh\n")
    archive_buffer.seek(0)

    with zipfile.ZipFile(archive_buffer) as archive:
        with pytest.raises(SkillsMarketplaceError, match="unsafe path"):
            _validated_zip_entries(archive)


@pytest.mark.asyncio
async def test_install_marketplace_skill_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_dir = tmp_path / "skills" / "already-here"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: already-here\n---\n", encoding="utf-8")
    launch = pytest.fail
    monkeypatch.setattr(
        "nanoinfra.webui.skills_marketplace.asyncio.create_subprocess_exec",
        launch,
    )

    result = await install_marketplace_skill("acme/agent-skills", "already-here", tmp_path)

    assert result == {
        "installed": True,
        "already_installed": True,
        "name": "already-here",
    }


@pytest.mark.asyncio
async def test_install_marketplace_skill_rejects_symlinked_skills_root(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    try:
        (workspace / "skills").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")

    with pytest.raises(SkillsMarketplaceError) as exc_info:
        await install_marketplace_skill(
            "",
            "ima-skills",
            workspace,
            provider="nanoinfra",
            nanoinfra_base_url=_NANOINFRA_BASE_URL,
        )

    assert exc_info.value.status == 403
    assert list(outside.iterdir()) == []
