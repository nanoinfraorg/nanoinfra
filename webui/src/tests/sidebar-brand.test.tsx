/**
 * The brand asset in the sidebar header.
 *
 * The header holds two different jobs, and one asset cannot do both. The open rail is 272 px wide
 * and shows the wordmark, because the wordmark states the product name. The collapsed rail is
 * 56 px wide. The wordmark asks for 165 px at a 28 px height, so it misses the rail by 109 px.
 * The square mark stays there.
 *
 * The favicon keeps the square mark for a different reason. A browser draws the favicon at 16 px.
 * The word is not legible at that size.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Sidebar } from "@/components/Sidebar";
import type { NanoinfraClient } from "@/lib/nanoinfra-client";
import type { ConnectionStatus } from "@/lib/types";
import { ClientProvider } from "@/providers/ClientProvider";

const MARK_SRC = "/brand/nanoinfra_mark.svg";
const WORDMARK_SRC = "/brand/nanoinfra_wordmark.svg";
const INDEX_HTML = readFileSync(resolve(process.cwd(), "index.html"), "utf8");

function fakeClient(): NanoinfraClient {
  return {
    status: "open",
    operatorActor: "webui",
    onStatus: (handler: (value: ConnectionStatus) => void) => {
      handler("open");
      return () => {};
    },
    onOperatorActor: (handler: (value: string | null) => void) => {
      handler("webui");
      return () => {};
    },
  } as unknown as NanoinfraClient;
}

function renderSidebar(collapsed: boolean) {
  const noop = () => {};
  const { container } = render(
    <ClientProvider client={fakeClient()} token="tok">
      <Sidebar
        sessions={[]}
        activeKey={null}
        loading={false}
        newChatActive={false}
        collapsed={collapsed}
        onNewChat={noop}
        onSelect={noop}
        onRequestDelete={noop}
        onTogglePin={noop}
        onRequestRename={noop}
        onToggleArchive={noop}
        onReorderSessions={noop}
        onToggleGroup={noop}
        onRequestRenameProject={noop}
        onNewChatInProject={noop}
        onOpenSettings={noop}
        onOpenApps={noop}
        onOpenSkills={noop}
        onOpenAutomations={noop}
        onOpenDiagrams={noop}
        onOpenServers={noop}
        onOpenSecrets={noop}
        onOpenApprovals={noop}
        onOpenAgents={noop}
        onOpenSearch={noop}
        onToggleArchived={noop}
        onCollapse={noop}
        onExpand={noop}
      />
    </ClientProvider>,
  );
  const brand = container.querySelector("img");
  if (!brand) throw new Error("The sidebar header shows no brand image");
  return { brand, container };
}

describe("Sidebar brand asset", () => {
  it("shows the wordmark when the rail is open", () => {
    const { brand } = renderSidebar(false);

    expect(brand.getAttribute("src")).toBe(WORDMARK_SRC);
  });

  it("sizes the wordmark by height and leaves the width free", () => {
    const { brand } = renderSidebar(false);

    // The wordmark is 1300 x 220. A fixed width squashes it, so only the height is fixed.
    expect(brand.className).toContain("h-7");
    expect(brand.className).toContain("w-auto");
  });

  it("keeps the square mark in the collapsed rail", () => {
    const { brand, container } = renderSidebar(true);

    // The rail is 56 px wide. The wordmark asks for 165 px, so it misses by 109 px.
    expect(brand.getAttribute("src")).toBe(MARK_SRC);
    expect(container.querySelector(`img[src="${WORDMARK_SRC}"]`)).toBeNull();
    expect(screen.getByRole("button", { name: "Toggle sidebar" })).toBeInTheDocument();
  });

  it("keeps both assets decorative and states the product name no second time", () => {
    const open = renderSidebar(false);

    /*
     * The image is decoration in both states, so ``alt`` stays empty.
     *
     * A collapsed rail gets its name from the button, which names the control. An open
     * rail makes that button inert, so nothing there needs a name. A screen reader
     * reads the product name from the document title. An ``alt`` of "nanoinfra" would
     * say the name a second time.
     */
    expect(open.brand.getAttribute("alt")).toBe("");
    expect(open.container.textContent).not.toContain("nanoinfra");

    open.container.remove();
    const rail = renderSidebar(true);

    expect(rail.brand.getAttribute("alt")).toBe("");
    expect(rail.container.querySelectorAll('img[alt="nanoinfra"]')).toHaveLength(0);
  });

  it("keeps the favicon on the square mark", () => {
    // A favicon at 16 px cannot show a word. The wordmark must not reach index.html.
    expect(INDEX_HTML).toContain(`<link rel="icon" type="image/svg+xml" href="${MARK_SRC}" />`);
    expect(INDEX_HTML).not.toContain("wordmark");
  });
});
