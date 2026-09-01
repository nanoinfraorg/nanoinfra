"""Search and install skills from public Agent Skills catalogs."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import stat
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, cast
from urllib.parse import quote

import httpx

from nanoinfra.agent.skills import SkillsLoader
from nanoinfra.security.network import PinnedDNSAsyncTransport
from nanoinfra.security.workspace_policy import WorkspaceBoundaryError, require_path_within

_PROVIDER_ALL = "all"
_PROVIDER_SKILLS_SH = "skills_sh"
_PROVIDER_NANOINFRA = "nanoinfra"

#: What a catalog row is, in the skills-server's vocabulary. A row published before the kinds
#: existed carries none, and reading that as a skill is correct: those rows are skills.
KIND_SKILL = "skill"
KIND_AGENT_PLUGIN = "agent-plugin"
KIND_CONNECTOR = "connector"
_KINDS = {KIND_SKILL, KIND_AGENT_PLUGIN, KIND_CONNECTOR}

#: Where each kind is installed, relative to the workspace. Three directories because three
#: subsystems read them: `SkillsLoader` reads prompt text, the executor reconciles Agent Plugins,
#: and the connector registry looks for declarative packages. A connector unpacked into `skills/`
#: is text nothing will ever activate, which is what happened before the kind was read at all.
_KIND_DIRECTORIES = {
    KIND_SKILL: "skills",
    KIND_AGENT_PLUGIN: "plugins",
    KIND_CONNECTOR: "connector-packages",
}

#: What is still missing after the package is on disk. A skill is readable the moment it lands; the
#: other two are not, and an install that reported success without saying so would be an install
#: that looked finished and did nothing.
_NEXT_STEP_BY_KIND = {
    KIND_AGENT_PLUGIN: (
        "Declare it in tools.agentPlugins and restart. The executor activates plugins; "
        "a chat turn cannot."
    ),
    KIND_CONNECTOR: (
        "Give it a credential and add it to connectors.active, then restart. "
        "Read what it grants first: its hosts are where a token of yours could go."
    ),
}
_PROVIDERS = {_PROVIDER_ALL, _PROVIDER_SKILLS_SH, _PROVIDER_NANOINFRA}
_SEARCH_URL = "https://skills.sh/api/search"
_TRENDING_URL = "https://skills.sh/api/skills/trending/0"
_SKILL_PAGE_BASE_URL = "https://www.skills.sh"
# Default base URL of the self-hosted nanoinfra skills-server catalog
# (submission pipeline + security scan shield + versioning, see
# nanoinfraorg/skills-server). Callers normally pass the configured value
# from Config.skills_marketplace.nanoinfra_base_url; this default matches
# that schema field's own default, so the bare module-level constant is only
# ever a fallback for a caller that doesn't thread config through (e.g. a
# script or test invoking these functions directly).
_DEFAULT_NANOINFRA_BASE_URL = "https://skills.nanoinfra.org"
_ALL_TIME_URLS = (
    "https://skills.sh/api/skills/all-time/0",
    "https://skills.sh/api/skills/all-time/1",
)
_TREND_VALUES_RE = re.compile(r'\\"values\\":\s*\[([0-9,\s]+)\]')
_SOURCE_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?$"
)
_SKILL_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_INSTALL_TIMEOUT_SECONDS = 120
_WEEKLY_CACHE_TTL_SECONDS = 300
_NANOINFRA_MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
_NANOINFRA_MAX_UNPACKED_BYTES = 100 * 1024 * 1024
_NANOINFRA_MAX_FILES = 1_000
# The skills CLI's OpenClaw adapter copies into <workspace>/skills, nanoinfra's layout too.
_CLI_AGENT = "openclaw"
_weekly_cache: dict[tuple[str, str], list[int]] = {}
_weekly_cache_expires_at = 0.0


def _response_json_object(response: httpx.Response) -> dict[str, Any] | None:
    """Narrow an untyped HTTP JSON response at the external-data boundary."""
    payload = cast(object, response.json())
    return cast(dict[str, Any], payload) if isinstance(payload, dict) else None


class SkillsMarketplaceError(Exception):
    """A safe error that can be returned to the WebUI."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def skills_install_supported() -> bool:
    """Return whether the official skills CLI can be launched."""
    return shutil.which("npx") is not None


async def trending_marketplace_skills(
    workspace_path: Path,
    *,
    limit: int = 8,
    provider: str = _PROVIDER_ALL,
    nanoinfra_base_url: str = _DEFAULT_NANOINFRA_BASE_URL,
) -> dict[str, Any]:
    """Return provider-aware marketplace rankings without mixing metric semantics."""
    selected = _valid_provider(provider)
    if selected == _PROVIDER_NANOINFRA:
        return await _trending_nanoinfra_skills(workspace_path, limit=limit, base_url=nanoinfra_base_url)
    if selected == _PROVIDER_SKILLS_SH:
        return await _trending_skills_sh_skills(workspace_path, limit=limit)

    results = await asyncio.gather(
        _trending_skills_sh_skills(workspace_path, limit=limit),
        _trending_nanoinfra_skills(workspace_path, limit=limit, base_url=nanoinfra_base_url),
        return_exceptions=True,
    )
    payloads = [result for result in results if isinstance(result, dict)]
    if not payloads:
        raise SkillsMarketplaceError(
            "skill marketplaces are temporarily unavailable",
            status=502,
        )
    return {
        "skills": [
            skill
            for payload in payloads
            for skill in payload.get("skills", [])
            if isinstance(skill, dict)
        ],
        "period": "mixed",
        "provider": _PROVIDER_ALL,
        "install_supported": any(bool(payload.get("install_supported")) for payload in payloads),
    }


async def _trending_skills_sh_skills(
    workspace_path: Path,
    *,
    limit: int,
) -> dict[str, Any]:
    """Return a source-diverse snapshot of skills.sh's real 24-hour leaderboard."""
    try:
        async with _skills_client() as client:
            response = await client.get(_TRENDING_URL)
            response.raise_for_status()
            payload = _response_json_object(response) or {}
    except (httpx.HTTPError, ValueError) as exc:
        raise SkillsMarketplaceError(
            "skills.sh trending skills are temporarily unavailable",
            status=502,
        ) from exc

    installed = _installed_skill_names(workspace_path)
    rows = payload.get("skills", [])
    skills: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    for rank, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        row_payload = cast(dict[str, Any], row)
        source = row_payload.get("source")
        if not isinstance(source, str) or source in seen_sources:
            continue
        skill = _marketplace_skill(row_payload, installed, rank=rank)
        if skill is None:
            continue
        seen_sources.add(source)
        skills.append(skill)
        if len(skills) >= min(max(limit, 1), 20):
            break

    return {
        "skills": skills,
        "period": "24h",
        "provider": _PROVIDER_SKILLS_SH,
        "install_supported": skills_install_supported(),
    }


async def search_marketplace_skills(
    query: str,
    workspace_path: Path,
    *,
    limit: int = 20,
    provider: str = _PROVIDER_ALL,
    nanoinfra_base_url: str = _DEFAULT_NANOINFRA_BASE_URL,
    kind: str = "",
) -> dict[str, Any]:
    """Search one or all catalogs and annotate locally installed results.

    ``kind`` narrows to one of the nanoinfra catalog's kinds. Only that catalog carries kinds, so
    naming one selects it: skills.sh publishes skills, and returning its rows beside a request for
    connectors would answer a different question than the one asked.
    """
    normalized = " ".join(query.split())
    if len(normalized) < 2:
        raise SkillsMarketplaceError("search query must contain at least 2 characters")
    if len(normalized) > 100:
        raise SkillsMarketplaceError("search query is too long")

    if kind and kind not in _KINDS:
        raise SkillsMarketplaceError(f"unknown package kind {kind!r}")

    selected = _valid_provider(provider)
    if kind or selected == _PROVIDER_NANOINFRA:
        return await _search_nanoinfra_skills(
            normalized, workspace_path, limit=limit, base_url=nanoinfra_base_url, kind=kind
        )
    if selected == _PROVIDER_SKILLS_SH:
        return await _search_skills_sh_skills(normalized, workspace_path, limit=limit)

    results = await asyncio.gather(
        _search_skills_sh_skills(normalized, workspace_path, limit=limit),
        _search_nanoinfra_skills(normalized, workspace_path, limit=limit, base_url=nanoinfra_base_url),
        return_exceptions=True,
    )
    payloads = [result for result in results if isinstance(result, dict)]
    if not payloads:
        raise SkillsMarketplaceError(
            "skill marketplaces are temporarily unavailable",
            status=502,
        )
    return {
        "query": normalized,
        "skills": [
            skill
            for payload in payloads
            for skill in payload.get("skills", [])
            if isinstance(skill, dict)
        ],
        "provider": _PROVIDER_ALL,
        "install_supported": any(bool(payload.get("install_supported")) for payload in payloads),
    }


async def _search_skills_sh_skills(
    normalized: str,
    workspace_path: Path,
    *,
    limit: int,
) -> dict[str, Any]:
    try:
        async with _skills_client() as client:
            response = await client.get(
                _SEARCH_URL,
                params={"q": normalized, "limit": min(max(limit, 1), 50)},
            )
            response.raise_for_status()
            payload = _response_json_object(response) or {}
    except (httpx.HTTPError, ValueError) as exc:
        raise SkillsMarketplaceError(
            "skills.sh search is temporarily unavailable",
            status=502,
        ) from exc

    installed = _installed_skill_names(workspace_path)
    rows = payload.get("skills", [])
    skills: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        skill = _marketplace_skill(cast(dict[str, Any], row), installed)
        if skill is not None:
            skills.append(skill)

    return {
        "query": normalized,
        "skills": skills,
        "provider": _PROVIDER_SKILLS_SH,
        "install_supported": skills_install_supported(),
    }


async def _search_nanoinfra_skills(
    normalized: str,
    workspace_path: Path,
    *,
    limit: int,
    base_url: str,
    kind: str = "",
) -> dict[str, Any]:
    params: dict[str, str] = {"q": normalized}
    if kind:
        # Narrowed by the catalog rather than here, so a page showing connectors does not download
        # the rest of the catalog to throw it away.
        params["kind"] = kind
    try:
        async with _nanoinfra_client(base_url) as client:
            response = await client.get("/api/v1/search", params=params)
            response.raise_for_status()
            payload = _response_json_object(response) or {}
    except (httpx.HTTPError, ValueError) as exc:
        raise SkillsMarketplaceError(
            "the nanoinfra skills catalog is temporarily unavailable",
            status=502,
        ) from exc

    installed = _installed_names_by_kind(workspace_path)
    rows = payload.get("skills", [])
    skills: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        skill = _nanoinfra_skill(cast(dict[str, Any], row), installed, base_url=base_url)
        if skill is not None:
            skills.append(skill)
        if len(skills) >= min(max(limit, 1), 50):
            break
    if kind == KIND_CONNECTOR:
        await _attach_connector_grants(skills, base_url=base_url)
    return {
        "query": normalized,
        "skills": skills,
        "provider": _PROVIDER_NANOINFRA,
        "install_supported": True,
        **({"kind": kind} if kind else {}),
    }


#: How many rows of a connector search get their grants fetched. Bounded because each one is a
#: detail request that opens an archive server-side.
_GRANTS_FANOUT = 8


async def _attach_connector_grants(
    skills: list[dict[str, Any]], *, base_url: str
) -> None:
    """Fill in what each connector would grant, for the rows a reader can see.

    The catalog deliberately keeps grants off search: it would open every archive in the catalog to
    answer a keystroke. A *connector* listing is different -- the grants are the content of the list,
    because "what would this be allowed to do" is the question an operator answers before
    installing -- so the client that needs them asks, for the rows it is about to show.

    A row whose detail cannot be read keeps no `grants` key. Absent and "grants nothing" are
    different statements, and the panel renders them differently.
    """
    targets = skills[:_GRANTS_FANOUT]
    if not targets:
        return
    results = await asyncio.gather(
        *(_nanoinfra_package_kind(row["skill_id"], base_url=base_url) for row in targets),
        return_exceptions=True,
    )
    for row, result in zip(targets, results, strict=True):
        if isinstance(result, BaseException):
            continue
        _, grants = result
        if grants:
            row["grants"] = grants


async def _trending_nanoinfra_skills(
    workspace_path: Path,
    *,
    limit: int,
    base_url: str,
) -> dict[str, Any]:
    try:
        async with _nanoinfra_client(base_url) as client:
            response = await client.get("/api/v1/trending")
            response.raise_for_status()
            payload = _response_json_object(response) or {}
    except (httpx.HTTPError, ValueError) as exc:
        raise SkillsMarketplaceError(
            "the nanoinfra skills catalog is temporarily unavailable",
            status=502,
        ) from exc

    installed = _installed_skill_names(workspace_path)
    rows = payload.get("skills", [])
    skills: list[dict[str, Any]] = []
    for rank, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        skill = _nanoinfra_skill(cast(dict[str, Any], row), installed, rank=rank, base_url=base_url)
        if skill is not None:
            skills.append(skill)
        if len(skills) >= min(max(limit, 1), 20):
            break
    return {
        "skills": skills,
        "period": "trending",
        "provider": _PROVIDER_NANOINFRA,
        "install_supported": True,
    }


async def marketplace_skill_trends(
    skill_ids: list[str] | None = None,
) -> dict[str, dict[str, list[int]]]:
    """Return install history independently, filling requested cache misses."""
    requested = _valid_skill_refs(skill_ids or [])
    async with _skills_client() as client:
        weekly_installs = await _load_weekly_installs(client)
        missing = [ref for ref in requested if ref not in weekly_installs]
        if missing:
            weekly_installs.update(await _load_skill_page_trends(client, missing))

    selected = requested or list(weekly_installs)
    return {
        "trends": {
            f"{source}/{skill_id}": values
            for source, skill_id in selected
            if (values := weekly_installs.get((source, skill_id))) is not None
        }
    }


async def install_marketplace_skill(
    source: str,
    skill_id: str,
    workspace_path: Path,
    *,
    provider: str = _PROVIDER_SKILLS_SH,
    version: str = "",
    nanoinfra_base_url: str = _DEFAULT_NANOINFRA_BASE_URL,
) -> dict[str, Any]:
    """Install one normalized marketplace result into ``<workspace>/skills``."""
    selected = _valid_provider(provider, allow_all=False)
    if selected == _PROVIDER_NANOINFRA:
        return await _install_nanoinfra_skill(skill_id, workspace_path, base_url=nanoinfra_base_url)
    return await _install_skills_sh_skill(source, skill_id, workspace_path)


async def _install_skills_sh_skill(
    source: str,
    skill_id: str,
    workspace_path: Path,
) -> dict[str, Any]:
    if not _SOURCE_RE.fullmatch(source):
        raise SkillsMarketplaceError("invalid skill source")
    if not _valid_skill_id(skill_id):
        raise SkillsMarketplaceError("invalid skill name")

    loader = SkillsLoader(workspace_path)
    existing = {entry["name"]: entry for entry in loader.list_skills(filter_unavailable=False)}
    if skill_id in existing:
        return {"installed": True, "already_installed": True, "name": skill_id}

    workspace = workspace_path.expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        require_path_within(
            workspace / "skills",
            workspace,
            message="skills directory must stay inside the workspace",
        )
    except WorkspaceBoundaryError as exc:
        raise SkillsMarketplaceError(str(exc), status=403) from exc

    npx = shutil.which("npx")
    if npx is None:
        raise SkillsMarketplaceError(
            "Node.js with npx is required to install skills",
            status=503,
        )

    env = os.environ.copy()
    env["DISABLE_TELEMETRY"] = "1"
    command = (
        npx,
        "--yes",
        "skills@latest",
        "add",
        source,
        "--skill",
        skill_id,
        "--agent",
        _CLI_AGENT,
        "--copy",
        "--yes",
    )

    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(workspace),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(
            process.communicate(),
            timeout=_INSTALL_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise SkillsMarketplaceError("skill installation timed out", status=504) from exc

    if process.returncode != 0:
        detail = _safe_output_tail(output)
        message = "skill installation failed"
        if detail:
            message = f"{message}: {detail}"
        raise SkillsMarketplaceError(message, status=502)

    installed = next(
        (
            entry
            for entry in loader.list_skills(filter_unavailable=False)
            if entry["source"] == "workspace" and entry["name"] == skill_id
        ),
        None,
    )
    if installed is None:
        raise SkillsMarketplaceError(
            "installer completed but the skill was not found in this workspace",
            status=502,
        )
    return {"installed": True, "already_installed": False, "name": skill_id}


async def _install_nanoinfra_skill(
    skill_id: str,
    workspace_path: Path,
    *,
    base_url: str,
) -> dict[str, Any]:
    """Install a skill published on the nanoinfra skills-server catalog.

    Unlike the old SkillHub integration, there is no separate
    signature/fingerprint endpoint to verify the download against: the
    catalog already ran the archive through its own submission pipeline and
    security scan shield before ever publishing it (see
    nanoinfraorg/skills-server's docs/architecture.md). Path-safety
    validation on extraction still applies here regardless -- a network
    response is never trusted just because of who served it.
    """
    if not _valid_skill_id(skill_id):
        raise SkillsMarketplaceError("invalid skill name")

    # Asked of the catalog rather than trusted from the client (#207): where the package lands is a
    # security decision -- a connector is requests made with a live credential, a plugin is code the
    # executor runs -- and a caller that could name the destination could put either in `skills/`,
    # or a skill where the executor looks for plugins.
    kind, grants = await _nanoinfra_package_kind(skill_id, base_url=base_url)
    directory = _KIND_DIRECTORIES[kind]

    workspace = workspace_path.expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    # Containment first, before anything reads that directory. Listing what is already installed
    # walks it, and a symlinked `skills/` would have been read through before this ran.
    try:
        skills_root = require_path_within(
            workspace / directory,
            workspace,
            message=f"{directory} directory must stay inside the workspace",
        )
    except WorkspaceBoundaryError as exc:
        raise SkillsMarketplaceError(str(exc), status=403) from exc

    if skill_id in _installed_names_by_kind(workspace_path).get(kind, set()):
        return {
            "installed": True,
            "already_installed": True,
            "name": skill_id,
            "kind": kind,
            "provider": _PROVIDER_NANOINFRA,
        }

    skills_root.mkdir(parents=True, exist_ok=True)
    target = skills_root / skill_id

    try:
        async with _nanoinfra_client(base_url) as client:
            with tempfile.TemporaryDirectory(
                prefix=".nanoinfra-install-",
                dir=skills_root,
            ) as temporary:
                temporary_path = Path(temporary)
                archive_path = temporary_path / f"{skill_id}.zip"
                stage_path = temporary_path / "stage"
                await _download_nanoinfra_archive(client, skill_id, archive_path)
                _validate_nanoinfra_archive(archive_path)
                _extract_nanoinfra_archive(archive_path, stage_path)
                if target.exists():
                    return {
                        "installed": True,
                        "already_installed": True,
                        "name": skill_id,
                        "kind": kind,
                        "provider": _PROVIDER_NANOINFRA,
                    }
                os.replace(stage_path, target)
    except SkillsMarketplaceError:
        raise
    except (httpx.HTTPError, OSError, zipfile.BadZipFile) as exc:
        raise SkillsMarketplaceError(
            "nanoinfra skill installation failed",
            status=502,
        ) from exc

    if kind != KIND_SKILL:
        # A connector or a plugin is on disk and does nothing yet, which is the whole point: the
        # package is written, and giving it a credential or activating it stays a config decision an
        # operator makes. Saying so here is the difference between "installed" and "working".
        return {
            "installed": True,
            "already_installed": False,
            "name": skill_id,
            "kind": kind,
            "provider": _PROVIDER_NANOINFRA,
            "directory": directory,
            "next_step": _NEXT_STEP_BY_KIND[kind],
            **({"grants": grants} if grants else {}),
        }

    loader = SkillsLoader(workspace_path)
    installed = next(
        (
            entry
            for entry in loader.list_skills(filter_unavailable=False)
            if entry["source"] == "workspace" and entry["name"] == skill_id
        ),
        None,
    )
    if installed is None:
        raise SkillsMarketplaceError(
            "installer completed but the skill was not found in this workspace",
            status=502,
        )
    return {
        "installed": True,
        "already_installed": False,
        "name": skill_id,
        "provider": _PROVIDER_NANOINFRA,
    }


async def _nanoinfra_package_kind(
    skill_id: str, *, base_url: str
) -> tuple[str, dict[str, Any] | None]:
    """Ask the catalog what this package is, and what installing it would allow.

    The kind decides which directory the archive is unpacked into, so it is read from the catalog
    rather than accepted from the caller. A row with no kind is a skill: those rows predate the
    kinds, and that is what they are.

    The grants ride along because they are in the same response and an install that cannot say what
    it allowed is an install nobody can review afterwards.
    """
    try:
        async with _nanoinfra_client(base_url) as client:
            response = await client.get(f"/api/v1/skills/{quote(skill_id, safe='')}")
            if response.status_code == 404:
                raise SkillsMarketplaceError(
                    "package not found on the nanoinfra catalog", status=404
                )
            response.raise_for_status()
            payload = _response_json_object(response) or {}
    except SkillsMarketplaceError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise SkillsMarketplaceError(
            "the nanoinfra skills catalog is temporarily unavailable",
            status=502,
        ) from exc

    kind = payload.get("kind")
    kind = kind if isinstance(kind, str) and kind in _KINDS else KIND_SKILL
    grants = payload.get("grants")
    return kind, cast("dict[str, Any] | None", grants if isinstance(grants, dict) else None)


async def _download_nanoinfra_archive(
    client: httpx.AsyncClient,
    skill_id: str,
    destination: Path,
) -> None:
    """Stream a skill's zip archive from the catalog's own download endpoint.

    Unlike SkillHub's flow (a redirect to a separate object-storage host,
    which had to be validated against a pinned hostname allowlist), the
    nanoinfra skills-server serves the archive directly from the same host
    -- there is no redirect to validate.
    """
    received = 0
    async with client.stream(
        "GET",
        f"/api/v1/skills/{quote(skill_id, safe='')}/download",
    ) as response:
        if response.status_code == 404:
            raise SkillsMarketplaceError("skill not found on the nanoinfra catalog", status=404)
        response.raise_for_status()
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > _NANOINFRA_MAX_DOWNLOAD_BYTES:
            raise SkillsMarketplaceError("nanoinfra skill package is too large", status=413)
        with destination.open("wb") as output:
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received > _NANOINFRA_MAX_DOWNLOAD_BYTES:
                    raise SkillsMarketplaceError("nanoinfra skill package is too large", status=413)
                output.write(chunk)


def _validated_zip_entries(archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, str]]:
    """Path-safety-validate every entry in a downloaded skill archive.

    Rejects zip-slip/absolute/traversal paths, symlinks, duplicate paths,
    and archives exceeding the file-count or unpacked-size caps. Used for
    nanoinfra skills-server installs; skills.sh installs go through the
    separate `npx skills` CLI, which handles its own extraction.
    """
    entries: list[tuple[zipfile.ZipInfo, str]] = []
    seen: set[str] = set()
    unpacked = 0
    for info in archive.infolist():
        raw_name = info.filename.replace("\\", "/")
        path = PurePosixPath(raw_name)
        normalized = path.as_posix()
        mode = info.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if (
            not normalized
            or "\x00" in normalized
            or path.is_absolute()
            or ".." in path.parts
            or (path.parts and ":" in path.parts[0])
            or kind == stat.S_IFLNK
            or kind not in {0, stat.S_IFREG, stat.S_IFDIR}
        ):
            raise SkillsMarketplaceError(
                f"skill package contains an unsafe path: {raw_name}",
                status=422,
            )
        if info.is_dir():
            continue
        if normalized in seen:
            raise SkillsMarketplaceError(
                f"skill package contains a duplicate path: {normalized}",
                status=422,
            )
        seen.add(normalized)
        unpacked += info.file_size
        if len(entries) >= _NANOINFRA_MAX_FILES:
            raise SkillsMarketplaceError("skill package contains too many files", status=413)
        if unpacked > _NANOINFRA_MAX_UNPACKED_BYTES:
            raise SkillsMarketplaceError(
                "skill package expands beyond the size limit", status=413
            )
        entries.append((info, normalized))
    if "SKILL.md" not in seen:
        raise SkillsMarketplaceError(
            "skill package does not contain a root SKILL.md",
            status=422,
        )
    return entries


def _validate_nanoinfra_archive(archive_path: Path) -> None:
    with zipfile.ZipFile(archive_path, "r") as archive:
        _validated_zip_entries(archive)  # raises on any unsafe entry


def _extract_nanoinfra_archive(archive_path: Path, destination: Path) -> None:
    destination.mkdir()
    with zipfile.ZipFile(archive_path, "r") as archive:
        for info, normalized in _validated_zip_entries(archive):
            target = destination.joinpath(*PurePosixPath(normalized).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            mode = (info.external_attr >> 16) & 0o777
            if mode:
                target.chmod(mode & 0o755)


def _skills_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=PinnedDNSAsyncTransport(),
        timeout=10.0,
        follow_redirects=False,
    )


def _nanoinfra_client(base_url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        transport=PinnedDNSAsyncTransport(),
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=False,
    )


def _installed_skill_names(workspace_path: Path) -> set[str]:
    return {
        entry["name"]
        for entry in SkillsLoader(workspace_path).list_skills(filter_unavailable=False)
    }


def _installed_names_by_kind(workspace_path: Path) -> dict[str, set[str]]:
    """What is already installed, per kind.

    Asked of the directory each kind lives in rather than of `SkillsLoader` alone: a connector is
    not a skill, so a catalog row for one was reported as *not installed* forever while its package
    sat in `connector-packages/`, and every listing offered to install it again.
    """
    workspace = workspace_path.expanduser()
    installed: dict[str, set[str]] = {KIND_SKILL: _installed_skill_names(workspace_path)}
    for kind in (KIND_AGENT_PLUGIN, KIND_CONNECTOR):
        directory = workspace / _KIND_DIRECTORIES[kind]
        try:
            installed[kind] = {
                entry.name for entry in directory.iterdir() if entry.is_dir()
            }
        except OSError:
            # No directory yet, or one this process cannot read. Either way nothing of this kind is
            # installed as far as a catalog listing is concerned, and a listing must not fail over
            # a missing directory.
            installed[kind] = set()
    return installed


def _marketplace_skill(
    row: dict[str, Any],
    installed: set[str],
    *,
    rank: int | None = None,
) -> dict[str, Any] | None:
    source = row.get("source")
    skill_id = row.get("skillId")
    if not isinstance(source, str) or not _SOURCE_RE.fullmatch(source):
        return None
    if not isinstance(skill_id, str) or not _valid_skill_id(skill_id):
        return None
    display_name = row.get("name")
    if not isinstance(display_name, str) or not display_name.strip():
        display_name = skill_id
    installs = row.get("installs")
    skill: dict[str, Any] = {
        "id": f"{source}/{skill_id}",
        "skill_id": skill_id,
        "name": display_name.strip(),
        "source": source,
        "provider": _PROVIDER_SKILLS_SH,
        "installs": installs if isinstance(installs, int) and installs >= 0 else 0,
        "url": f"https://skills.sh/{source}/{skill_id}",
        "installed": skill_id in installed,
        "install_supported": skills_install_supported(),
        "metric": "installs_24h" if rank is not None else "installs_total",
    }
    if rank is not None:
        skill["rank"] = rank
    return skill


def _nanoinfra_skill(
    row: dict[str, Any],
    installed: set[str] | dict[str, set[str]],
    *,
    rank: int | None = None,
    base_url: str,
) -> dict[str, Any] | None:
    """Map one row of a nanoinfra skills-server API response (search,
    trending) into the shape the WebUI's marketplace UI already expects.

    Response shape (see nanoinfraorg/skills-server's docs/api.md):
    ``{"skill_id", "display_name", "description", "current_version",
    "status", "published_at", "github_path", "downloads", "created_at"}``.
    A quarantined skill never appears in search/trending results at all --
    the catalog itself excludes those -- so there's no quarantine flag to
    thread through here.
    """
    skill_id = row.get("skill_id")
    if not isinstance(skill_id, str) or not _valid_skill_id(skill_id):
        return None
    display_name = row.get("display_name") or skill_id
    if not isinstance(display_name, str) or not display_name.strip():
        display_name = skill_id

    downloads = row.get("downloads")
    version = row.get("current_version")
    kind = row.get("kind")
    kind = kind if isinstance(kind, str) and kind in _KINDS else KIND_SKILL

    skill: dict[str, Any] = {
        "kind": kind,
        "id": f"{_PROVIDER_NANOINFRA}:{skill_id}",
        "skill_id": skill_id,
        "name": display_name.strip(),
        "source": _PROVIDER_NANOINFRA,
        "provider": _PROVIDER_NANOINFRA,
        "installs": downloads if isinstance(downloads, int) and downloads >= 0 else 0,
        "url": f"{base_url.rstrip('/')}/skills/{quote(skill_id, safe='')}",
        "installed": skill_id in (
            installed.get(kind, set()) if isinstance(installed, dict) else installed
        ),
        "install_supported": True,
        "metric": "installs_total",
        "version": str(version) if isinstance(version, int) else "",
    }
    if rank is not None:
        skill["rank"] = rank
    return skill


async def _load_weekly_installs(
    client: httpx.AsyncClient,
) -> dict[tuple[str, str], list[int]]:
    global _weekly_cache, _weekly_cache_expires_at

    now = time.monotonic()
    if now < _weekly_cache_expires_at:
        return _weekly_cache

    responses = await asyncio.gather(
        *(client.get(url) for url in _ALL_TIME_URLS),
        return_exceptions=True,
    )
    history: dict[tuple[str, str], list[int]] = {}
    successful = False
    for response in responses:
        if isinstance(response, BaseException):
            continue
        try:
            response.raise_for_status()
            payload = _response_json_object(response) or {}
        except (httpx.HTTPError, ValueError):
            continue
        successful = True
        rows = payload.get("skills", [])
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_payload = cast(dict[str, Any], row)
            source = row_payload.get("source")
            skill_id = row_payload.get("skillId")
            values = row_payload.get("weeklyInstalls")
            if isinstance(source, str) and isinstance(skill_id, str) and isinstance(values, list):
                clean = [
                    value
                    for value in cast(list[object], values)
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                ]
                if len(clean) >= 2:
                    history[(source, skill_id)] = clean

    if successful:
        _weekly_cache = history
        _weekly_cache_expires_at = now + _WEEKLY_CACHE_TTL_SECONDS
    return history


def _valid_skill_refs(skill_ids: list[str]) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    for value in skill_ids[:20]:
        if "/" not in value:
            continue
        source, skill_id = value.rsplit("/", 1)
        ref = (source, skill_id)
        if _SOURCE_RE.fullmatch(source) and _valid_skill_id(skill_id) and ref not in refs:
            refs.append(ref)
    return refs


async def _load_skill_page_trends(
    client: httpx.AsyncClient,
    refs: list[tuple[str, str]],
) -> dict[tuple[str, str], list[int]]:
    semaphore = asyncio.Semaphore(6)

    async def fetch(ref: tuple[str, str]) -> tuple[tuple[str, str], list[int]]:
        source, skill_id = ref
        try:
            async with semaphore:
                response = await client.get(f"{_SKILL_PAGE_BASE_URL}/{source}/{skill_id}")
            response.raise_for_status()
        except httpx.HTTPError:
            return ref, []

        match = _TREND_VALUES_RE.search(response.text)
        if match is None:
            return ref, []
        values = [int(value) for value in match.group(1).split(",") if value.strip()]
        return ref, values if len(values) >= 2 else []

    return dict(await asyncio.gather(*(fetch(ref) for ref in refs)))


def _valid_skill_id(value: str) -> bool:
    return len(value) <= 64 and _SKILL_RE.fullmatch(value) is not None


def _valid_provider(value: str, *, allow_all: bool = True) -> str:
    normalized = value.strip().lower() or _PROVIDER_ALL
    allowed = _PROVIDERS if allow_all else _PROVIDERS - {_PROVIDER_ALL}
    if normalized not in allowed:
        raise SkillsMarketplaceError("invalid skill marketplace provider")
    return normalized


def _safe_output_tail(output: bytes | None) -> str:
    if not output:
        return ""
    text = _ANSI_RE.sub("", output.decode("utf-8", errors="replace"))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " · ".join(lines[-3:])[-600:]
