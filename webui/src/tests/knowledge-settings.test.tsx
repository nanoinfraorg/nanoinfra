import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { KnowledgeSettings } from "@/components/settings/KnowledgeSettings";
import type { KnowledgePayload, SettingsPayload } from "@/lib/types";

function knowledgePayload(over: Partial<KnowledgePayload> = {}): KnowledgePayload {
  return {
    enabled: true,
    mode: "lexical",
    indexed_mode: "lexical",
    reindex_interval_s: 900,
    exclude: [".env", "*.pem", "secrets/**"],
    max_file_bytes: 2_097_152,
    max_total_bytes: 209_715_200,
    max_results: 5,
    path: "/workspaces/default/knowledge",
    exists: true,
    documents: 12,
    fragments: 48,
    indexed_bytes: 1_048_576,
    hybrid_available: false,
    hybrid_install_hint: "pip install 'semlix[semantic]'",
    skipped: [],
    errors: [],
    last_run: {
      trigger: "automation",
      finished_at_ms: 1_700_000_000_000,
      added: 2,
      updated: 1,
      removed: 0,
      skipped: 1,
      errors: 0,
      duration_ms: 41,
    },
    ...over,
  };
}

function settingsWith(knowledge: KnowledgePayload | undefined): SettingsPayload {
  return { ...(knowledge ? { knowledge } : {}) } as unknown as SettingsPayload;
}

function renderPanel(
  knowledge: KnowledgePayload | undefined,
  onSaved: (payload: SettingsPayload) => void = () => {},
) {
  return render(
    <KnowledgeSettings token="tok" settings={settingsWith(knowledge)} onSaved={onSaved} />,
  );
}

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("KnowledgeSettings", () => {
  it("stays away when the gateway sends no knowledge block", () => {
    renderPanel(undefined);

    expect(screen.queryByTestId("knowledge-settings")).not.toBeInTheDocument();
  });

  it("greys hybrid out and names the install command when the extra is absent", () => {
    renderPanel(knowledgePayload({ hybrid_available: false }));

    const hybrid = screen.getByRole("option", { name: "Hybrid (BM25F + vectors)" });
    expect(hybrid).toBeDisabled();
    expect(screen.getByTestId("knowledge-hybrid-hint")).toHaveTextContent(
      "pip install 'semlix[semantic]'",
    );
  });

  it("offers hybrid without a hint once the extra is installed", () => {
    renderPanel(knowledgePayload({ hybrid_available: true, hybrid_install_hint: null }));

    expect(screen.getByRole("option", { name: "Hybrid (BM25F + vectors)" })).toBeEnabled();
    expect(screen.queryByTestId("knowledge-hybrid-hint")).not.toBeInTheDocument();
  });

  it("reports what the last run did, including what it refused", () => {
    renderPanel(
      knowledgePayload({
        skipped: [{ path: "logs/huge.log", reason: "too_large", detail: "2400000 bytes" }],
        errors: ["notes/locked.md: could not be read (Permission denied)"],
      }),
    );

    expect(screen.getByTestId("knowledge-counts")).toHaveTextContent("12 documents");
    expect(screen.getByTestId("knowledge-counts")).toHaveTextContent("48 fragments");
    expect(screen.getByTestId("knowledge-last-run")).toHaveTextContent("2 added");
    expect(screen.getByTestId("knowledge-last-run")).toHaveTextContent("41ms");
    // The reason, not just the count: a skipped file nobody is told about is a silent drop.
    expect(screen.getByTestId("knowledge-skip")).toHaveTextContent(
      "logs/huge.log: larger than the per-file limit (2400000 bytes)",
    );
    expect(screen.getByTestId("knowledge-error")).toHaveTextContent("Permission denied");
  });

  it("says when no pass has run yet rather than showing zeroes", () => {
    renderPanel(knowledgePayload({ last_run: null }));

    expect(screen.getByTestId("knowledge-never-ran")).toBeInTheDocument();
    expect(screen.queryByTestId("knowledge-last-run")).not.toBeInTheDocument();
  });

  it("names a stored index built in another mode", () => {
    renderPanel(knowledgePayload({ mode: "lexical", indexed_mode: "hybrid" }));

    expect(screen.getByTestId("knowledge-mode-drift")).toHaveTextContent(
      "The next pass rebuilds it.",
    );
  });

  it("keeps Save disabled until something changes", () => {
    renderPanel(knowledgePayload());

    expect(screen.getByTestId("knowledge-save")).toBeDisabled();

    fireEvent.change(screen.getByTestId("knowledge-max-results"), { target: { value: "8" } });

    expect(screen.getByTestId("knowledge-save")).toBeEnabled();
  });

  it("sends the caps in bytes and the excludes as a JSON array", async () => {
    const savedPayload = settingsWith(knowledgePayload());
    const fetchMock = vi.fn(async () => jsonResponse(savedPayload));
    vi.stubGlobal("fetch", fetchMock);
    const onSaved = vi.fn();
    renderPanel(knowledgePayload(), onSaved);

    fireEvent.change(screen.getByTestId("knowledge-max-file"), { target: { value: "4" } });
    fireEvent.change(screen.getByTestId("knowledge-exclude"), {
      target: { value: ".env\n*.pem\nsecrets/**\nlogs/*.log\n" },
    });
    fireEvent.click(screen.getByTestId("knowledge-save"));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url] = fetchMock.mock.calls[0] as unknown as [string];
    const query = new URLSearchParams(url.split("?")[1]);
    expect(url.startsWith("/api/settings/knowledge/update")).toBe(true);
    expect(query.get("max_file_bytes")).toBe(String(4 * 1024 * 1024));
    expect(JSON.parse(query.get("exclude") ?? "[]")).toEqual([
      ".env",
      "*.pem",
      "secrets/**",
      "logs/*.log",
    ]);
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(savedPayload));
    expect(screen.getByTestId("knowledge-status")).toHaveTextContent("after a restart");
  });

  it("shows the refusal that names the key", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: false,
      status: 400,
      text: async () => "knowledge.reindex_interval_s: Input should be greater than or equal to 60",
      json: async () => ({}),
    } as Response));
    vi.stubGlobal("fetch", fetchMock);
    renderPanel(knowledgePayload());

    fireEvent.change(screen.getByTestId("knowledge-interval"), { target: { value: "10" } });
    fireEvent.click(screen.getByTestId("knowledge-save"));

    await waitFor(() =>
      expect(screen.getByTestId("knowledge-status")).toHaveTextContent(
        "knowledge.reindex_interval_s",
      ),
    );
  });

  it("describes the schedule in the unit the operator typed it in", () => {
    renderPanel(knowledgePayload({ reindex_interval_s: 3600 }));

    expect(screen.getByText(/every hour/)).toBeInTheDocument();
  });
});
