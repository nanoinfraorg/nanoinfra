import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  ConnectorCatalogPanel,
  type ConnectorCatalogRow,
} from "@/components/settings/ConnectorCatalogPanel";

/**
 * The panel's reason to exist is one rule: **what a connector grants is on the row, not behind a
 * click.** A connector is requests made with a live credential, and `hosts` is the field that
 * decides where a token of yours could go — so a row showing a name and an Install button would put
 * the only check that matters somewhere nobody looks.
 */

function row(overrides: Partial<ConnectorCatalogRow> = {}): ConnectorCatalogRow {
  return {
    id: "nanoinfra:acme-crm",
    skill_id: "acme-crm",
    name: "Acme CRM",
    source: "nanoinfra",
    provider: "nanoinfra",
    installs: 12,
    url: "https://skills.nanoinfra.org/skills/acme-crm",
    installed: false,
    install_supported: true,
    metric: "installs_total",
    kind: "connector",
    grants: {
      kind: "connector",
      operations: [
        { name: "list_contacts", class: "read", method: "GET", path: "/v1/contacts" },
        { name: "create_contact", class: "mutate.remote", method: "POST", path: "/v1/contacts" },
      ],
      classes: ["read", "mutate.remote"],
      hosts: ["api.acme.example"],
      scopes: ["crm.read", "crm.write"],
    },
    ...overrides,
  };
}

function renderPanel(overrides: Partial<Parameters<typeof ConnectorCatalogPanel>[0]> = {}) {
  const onSearch = vi.fn();
  const onInstall = vi.fn();
  render(
    <ConnectorCatalogPanel
      rows={[row()]}
      loading={false}
      installing={null}
      error={null}
      lastInstalled={null}
      onSearch={onSearch}
      onInstall={onInstall}
      {...overrides}
    />,
  );
  return { onSearch, onInstall };
}

describe("what a connector grants", () => {
  it("names every operation and its capability class", () => {
    renderPanel();

    expect(screen.getByText("list_contacts")).toBeTruthy();
    expect(screen.getByText("create_contact")).toBeTruthy();
    expect(screen.getByText("read")).toBeTruthy();
    expect(screen.getByText("mutate.remote")).toBeTruthy();
  });

  it("names the hosts a token could reach", () => {
    // The field that stops a hostile manifest getting a live credential.
    renderPanel();

    expect(screen.getByText(/api\.acme\.example/)).toBeTruthy();
  });

  it("names the scopes the token would carry", () => {
    renderPanel();

    expect(screen.getByText(/crm\.read, crm\.write/)).toBeTruthy();
  });

  it("says the grants are unknown rather than showing an empty table", () => {
    // Absent and "asks for nothing" are different statements, and rendering the first as the
    // second understates what an install is about to allow.
    renderPanel({ rows: [row({ grants: undefined })] });

    expect(screen.getByText(/could not read this package/)).toBeTruthy();
  });
});

describe("installing", () => {
  it("hands the row back so the caller knows which package", () => {
    const { onInstall } = renderPanel();

    fireEvent.click(screen.getByRole("button", { name: /Install/ }));

    expect(onInstall).toHaveBeenCalledWith(expect.objectContaining({ skill_id: "acme-crm" }));
  });

  it("offers nothing to do on a connector that is already installed", () => {
    renderPanel({ rows: [row({ installed: true })] });

    expect(screen.getByRole("button", { name: /Installed/ })).toBeDisabled();
  });

  it("blocks a second install while one runs", () => {
    // Two archives unpacking into the same directory is not a race worth having.
    renderPanel({ installing: "other-connector" });

    expect(screen.getByRole("button", { name: /Install/ })).toBeDisabled();
  });

  it("says what is still missing after the package lands", () => {
    // Installed is not working: it has no credential and is not in `connectors.active`.
    renderPanel({
      lastInstalled: {
        name: "acme-crm",
        next_step: "Give it a credential and add it to connectors.active, then restart.",
      },
    });

    expect(screen.getByText(/connectors\.active/)).toBeTruthy();
  });
});

describe("searching", () => {
  it("does not search on a query too short to mean anything", () => {
    const { onSearch } = renderPanel({ rows: null });

    fireEvent.change(screen.getByLabelText("Search the catalog"), { target: { value: "a" } });
    fireEvent.keyDown(screen.getByLabelText("Search the catalog"), { key: "Enter" });

    expect(onSearch).not.toHaveBeenCalled();
  });

  it("searches on Enter", () => {
    const { onSearch } = renderPanel({ rows: null });

    fireEvent.change(screen.getByLabelText("Search the catalog"), { target: { value: "crm" } });
    fireEvent.keyDown(screen.getByLabelText("Search the catalog"), { key: "Enter" });

    expect(onSearch).toHaveBeenCalledWith("crm");
  });

  it("says a search found nothing rather than showing an empty panel", () => {
    renderPanel({ rows: [] });

    expect(screen.getByText(/No connectors match/)).toBeTruthy();
  });

  it("shows nothing about results before the first search", () => {
    renderPanel({ rows: null });

    expect(screen.queryByText(/No connectors match/)).toBeNull();
  });

  it("surfaces a failure instead of an empty list", () => {
    renderPanel({ rows: [], error: "the nanoinfra skills catalog is temporarily unavailable" });

    expect(screen.getByText(/temporarily unavailable/)).toBeTruthy();
  });
});
