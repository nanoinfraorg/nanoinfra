from __future__ import annotations

from typing import Any

from nanobot.webui import cli_apps_api


class _FakeManager:
    def __init__(self, *, fresh: bool, apps: list[dict[str, Any]] | None = None) -> None:
        self.fresh = fresh
        self.apps = apps or []
        self.payload_calls: list[bool] = []

    def payload(self, *, cache_only: bool = False) -> dict[str, Any]:
        self.payload_calls.append(cache_only)
        return {
            "apps": list(self.apps),
            "installed_count": 0,
            "catalog_updated_at": "2026-04-18" if self.apps else None,
        }

    def catalog_cache_fresh(self) -> bool:
        return self.fresh

    def installed_payload(self) -> dict[str, Any]:
        return {
            "apps": [
                {
                    "name": "gimp",
                    "display_name": "GIMP",
                    "category": "image",
                    "description": "Image editing",
                    "requires": "Python",
                    "source": "local",
                    "entry_point": "cli-anything-gimp",
                    "install_supported": True,
                    "installed": True,
                    "available": True,
                    "status": "installed",
                    "logo_url": None,
                    "brand_color": None,
                    "skill_installed": True,
                }
            ],
            "installed_count": 1,
            "catalog_updated_at": None,
        }


def test_cli_apps_payload_uses_cache_and_marks_refresh_pending(monkeypatch) -> None:
    manager = _FakeManager(fresh=False)
    refreshes = []
    monkeypatch.setattr(cli_apps_api, "_manager", lambda: manager)
    monkeypatch.setattr(cli_apps_api, "_start_catalog_refresh", lambda: refreshes.append(True))

    payload = cli_apps_api.cli_apps_payload()

    assert manager.payload_calls == [True]
    assert refreshes == [True]
    assert payload["catalog_refresh_pending"] is True
    assert payload["apps"][0]["name"] == "gimp"


def test_cli_apps_payload_skips_refresh_when_cache_is_fresh(monkeypatch) -> None:
    manager = _FakeManager(
        fresh=True,
        apps=[
            {
                "name": "gimp",
                "display_name": "GIMP",
                "category": "image",
                "description": "Image editing",
                "requires": "Python",
                "source": "harness",
                "entry_point": "cli-anything-gimp",
                "install_supported": True,
                "installed": False,
                "available": False,
                "status": "not_installed",
                "logo_url": None,
                "brand_color": None,
                "skill_installed": False,
            }
        ],
    )
    refreshes = []
    monkeypatch.setattr(cli_apps_api, "_manager", lambda: manager)
    monkeypatch.setattr(cli_apps_api, "_start_catalog_refresh", lambda: refreshes.append(True))

    payload = cli_apps_api.cli_apps_payload()

    assert manager.payload_calls == [True]
    assert refreshes == []
    assert payload["catalog_refresh_pending"] is False
    assert payload["apps"][0]["source"] == "harness"
