/**
 * Settings -> Prompts (nanoinfraorg/nanoinfra#264).
 *
 * The two prompts that run with nobody watching. Each test here is one rule of the panel, and
 * each fails if that rule is dropped:
 *
 * - the two prompts are named and say what they decide;
 * - the text in force is shown, editable, with its source;
 * - the evaluator's requirement is shown, because a replacement that stops asking for
 *   `evaluate_notification` leaves the gate failing closed and silent;
 * - saving text equal to the platform's removes the override rather than storing a copy;
 * - so does emptying the box;
 * - restore puts the platform's text back and deletes the file;
 * - a prompt too large for one header line travels in numbered chunks;
 * - a failed read says the panel is unavailable rather than offering an empty editor.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  WorkspacePromptsSettings,
  type WorkspacePrompt,
  type WorkspacePromptsPayload,
} from "@/components/settings/WorkspacePromptsSettings";

const READ_PATH = "/api/settings/workspace-prompts";
const SAVE_PATH = "/api/settings/workspace-prompts/save";
const PROMPT_HEADER = "X-Nanoinfra-Workspace-Prompt";
const CHUNK_COUNT_HEADER = "X-Nanoinfra-Workspace-Prompt-Chunks";
const PLATFORM_DREAM = "You are the memory consolidation engine.\nWrite conduct to SOUL.md.";
const PLATFORM_EVALUATOR = "Decide whether this result is worth a notification.";
const DREAM_CONTROLS =
  "How memory is organised: which file each learned fact goes to, and how hard stale content "
  + "is pruned.";
const EVALUATOR_REQUIREMENT =
  "It must still tell the model to call the `evaluate_notification` tool. Without that the gate "
  + "fails closed and stays silent.";

function dream(over: Partial<WorkspacePrompt> = {}): WorkspacePrompt {
  return {
    controls: DREAM_CONTROLS,
    max_chars: 32_000,
    name: "dream",
    path: "/home/op/.nanoinfra/workspaces/default/prompts/dream.md",
    platform_text: PLATFORM_DREAM,
    requirement: "",
    source: "platform",
    text: PLATFORM_DREAM,
    ...over,
  };
}

function evaluator(over: Partial<WorkspacePrompt> = {}): WorkspacePrompt {
  return {
    controls: "Whether a heartbeat result is worth notifying you about.",
    max_chars: 32_000,
    name: "evaluator",
    path: "/home/op/.nanoinfra/workspaces/default/prompts/evaluator.md",
    platform_text: PLATFORM_EVALUATOR,
    requirement: EVALUATOR_REQUIREMENT,
    source: "platform",
    text: PLATFORM_EVALUATOR,
    ...over,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return {
    headers: new Headers({ "content-type": "application/json" }),
    json: async () => body,
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  } as Response;
}

function textResponse(text: string, status: number): Response {
  return {
    headers: new Headers({ "content-type": "text/plain" }),
    json: async () => ({}),
    ok: false,
    status,
    text: async () => text,
  } as Response;
}

interface Call {
  url: string;
  headers: Record<string, string>;
}

/**
 * The gateway, as far as this panel can tell.
 *
 * `read` answers the first request and every request after a write, unless `write` returns its
 * own payload -- which is what the route does: the write answers with the read payload, so the
 * panel reads `source` back rather than predicting whether a file was stored or deleted.
 */
function stubGateway({
  read,
  write,
}: {
  read: () => Response;
  write?: () => Response;
}): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: string, init: RequestInit = {}) => {
      const url = String(input);
      calls.push({ headers: (init.headers ?? {}) as Record<string, string>, url });
      if (url.includes(SAVE_PATH)) {
        return Promise.resolve(write ? write() : read());
      }
      return Promise.resolve(read());
    }),
  );
  return calls;
}

/** The write payload, reassembled from its numbered header chunks. */
function writtenValues(call: Call): { name: string; text: string } {
  const count = Number(call.headers[CHUNK_COUNT_HEADER]);
  let encoded = "";
  for (let index = 0; index < count; index += 1) {
    encoded += call.headers[`${PROMPT_HEADER}-${index}`] ?? "";
  }
  return JSON.parse(decodeURIComponent(encoded)) as { name: string; text: string };
}

function payload(prompts: WorkspacePrompt[]): WorkspacePromptsPayload {
  return { prompts };
}

async function renderPanel(prompts: WorkspacePrompt[], write?: () => Response) {
  const calls = stubGateway({ read: () => jsonResponse(payload(prompts)), write });
  render(<WorkspacePromptsSettings token="tok" />);
  await waitFor(() => expect(screen.getByTestId("prompt-card-dream")).toBeInTheDocument());
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Settings -> Prompts", () => {
  it("names each prompt and says what it decides", async () => {
    const calls = await renderPanel([dream(), evaluator()]);

    // One read, of the route that serves the text in force.
    expect(calls[0].url).toContain(READ_PATH);
    expect(screen.getByText("Dream — memory consolidation")).toBeInTheDocument();
    expect(screen.getByTestId("prompt-controls-dream")).toHaveTextContent(DREAM_CONTROLS);
    expect(screen.getByText("Evaluator — the heartbeat's notification gate")).toBeInTheDocument();
    expect(screen.getByTestId("prompt-controls-evaluator")).toHaveTextContent(
      "Whether a heartbeat result is worth notifying you about.",
    );
  });

  it("shows the text in force and whose text it is", async () => {
    await renderPanel([
      dream({ source: "workspace", text: "My own dream prompt." }),
      evaluator(),
    ]);

    // The text in force, not the platform's, and the panel says which one you are looking at.
    expect(screen.getByTestId("prompt-editor-dream")).toHaveValue("My own dream prompt.");
    expect(screen.getByTestId("prompt-source-dream")).toHaveTextContent(
      "This workspace's own text",
    );
    expect(screen.getByTestId("prompt-editor-evaluator")).toHaveValue(PLATFORM_EVALUATOR);
    expect(screen.getByTestId("prompt-source-evaluator")).toHaveTextContent(
      "The platform's text",
    );
  });

  it("shows the evaluator's requirement, which a silent gate depends on", async () => {
    await renderPanel([dream(), evaluator()]);

    expect(screen.getByTestId("prompt-requirement-evaluator")).toHaveTextContent(
      "evaluate_notification",
    );
    // Dream has no requirement, and an empty one is not rendered as a heading with nothing after.
    expect(screen.queryByTestId("prompt-requirement-dream")).not.toBeInTheDocument();
  });

  it("says where the override file lives, because an editor writes the same file", async () => {
    await renderPanel([dream(), evaluator()]);

    expect(screen.getByTestId("prompt-path-dream")).toHaveTextContent(
      "/home/op/.nanoinfra/workspaces/default/prompts/dream.md",
    );
  });

  it("removes the override when the text saved is the platform's own", async () => {
    // A workspace that replaced the prompt, and an operator putting the default back by hand.
    const calls = await renderPanel(
      [dream({ source: "workspace", text: "My own dream prompt." }), evaluator()],
      () => jsonResponse(payload([dream(), evaluator()])),
    );

    fireEvent.change(screen.getByTestId("prompt-editor-dream"), {
      target: { value: PLATFORM_DREAM },
    });
    // Said before the click, not after: the operator learns what the save will do while they can
    // still decide otherwise.
    expect(screen.getByTestId("prompt-matches-platform-dream")).toHaveTextContent(
      "removes the override file rather than storing a copy",
    );

    fireEvent.click(screen.getByTestId("prompt-save-dream"));

    await waitFor(() =>
      expect(screen.getByTestId("prompt-status-dream")).toHaveTextContent(
        "The override file is gone",
      ),
    );
    const write = calls.find((call) => call.url.includes(SAVE_PATH));
    // The platform's text travels as itself. The panel never sends "" to mean "the default":
    // the server compares against the packaged prompt, and a copy stored under either spelling
    // would still win tomorrow.
    expect(writtenValues(write!)).toEqual({ name: "dream", text: PLATFORM_DREAM });
    expect(screen.getByTestId("prompt-source-dream")).toHaveTextContent("The platform's text");
  });

  it("removes the override when the box is emptied", async () => {
    const calls = await renderPanel(
      [dream({ source: "workspace", text: "My own dream prompt." }), evaluator()],
      () => jsonResponse(payload([dream(), evaluator()])),
    );

    fireEvent.change(screen.getByTestId("prompt-editor-dream"), { target: { value: "" } });
    fireEvent.click(screen.getByTestId("prompt-save-dream"));

    await waitFor(() =>
      expect(screen.getByTestId("prompt-status-dream")).toHaveTextContent(
        "The override file is gone",
      ),
    );
    expect(writtenValues(calls.find((call) => call.url.includes(SAVE_PATH))!).text).toBe("");
  });

  it("puts the platform's text back in the box and deletes the file on restore", async () => {
    const calls = await renderPanel(
      [dream({ source: "workspace", text: "My own dream prompt." }), evaluator()],
      () => jsonResponse(payload([dream(), evaluator()])),
    );

    fireEvent.click(screen.getByTestId("prompt-restore-dream"));

    await waitFor(() =>
      expect(screen.getByTestId("prompt-editor-dream")).toHaveValue(PLATFORM_DREAM),
    );
    expect(writtenValues(calls.find((call) => call.url.includes(SAVE_PATH))!)).toEqual({
      name: "dream",
      text: PLATFORM_DREAM,
    });
    await waitFor(() =>
      expect(screen.getByTestId("prompt-status-dream")).toHaveTextContent(
        "The override file is gone",
      ),
    );
  });

  it("carries a prompt too large for one header line in numbered chunks", async () => {
    const long = "x".repeat(8_192);
    const calls = await renderPanel(
      [dream(), evaluator()],
      () => jsonResponse(payload([dream({ source: "workspace", text: long }), evaluator()])),
    );

    fireEvent.change(screen.getByTestId("prompt-editor-dream"), { target: { value: long } });
    fireEvent.click(screen.getByTestId("prompt-save-dream"));

    await waitFor(() =>
      expect(screen.getByTestId("prompt-status-dream")).toHaveTextContent("Saved"),
    );
    const write = calls.find((call) => call.url.includes(SAVE_PATH))!;
    // More than one chunk, and the text survives the split: an 8 KB body in one header is a
    // connection the gateway drops with no status code at all.
    expect(Number(write.headers[CHUNK_COUNT_HEADER])).toBeGreaterThan(1);
    expect(writtenValues(write).text).toBe(long);
  });

  it("says the panel is unavailable when the read fails, and offers no editor", async () => {
    stubGateway({ read: () => textResponse("no route for /api/settings/workspace-prompts", 404) });
    render(<WorkspacePromptsSettings token="tok" />);

    await waitFor(() =>
      expect(screen.getByTestId("workspace-prompts-unavailable")).toBeInTheDocument(),
    );
    // An empty editor over a failed read invites somebody to type a replacement for a prompt
    // nobody read, and then save it over a working one.
    expect(screen.queryByTestId("prompt-editor-dream")).not.toBeInTheDocument();
    expect(screen.queryByTestId("prompt-editor-evaluator")).not.toBeInTheDocument();
    expect(screen.getByTestId("workspace-prompts-unavailable")).toHaveTextContent(
      "no route for /api/settings/workspace-prompts",
    );
  });

  it("keeps the gateway's own words when a write is refused", async () => {
    await renderPanel(
      [dream(), evaluator()],
      () => textResponse("a workspace prompt is capped at 32,000 characters", 400),
    );

    fireEvent.change(screen.getByTestId("prompt-editor-dream"), { target: { value: "shorter" } });
    fireEvent.click(screen.getByTestId("prompt-save-dream"));

    await waitFor(() =>
      expect(screen.getByTestId("prompt-status-dream")).toHaveTextContent(
        "Not saved: a workspace prompt is capped at 32,000 characters",
      ),
    );
    // The refused text stays in the box. A panel that reverted it would throw away the work and
    // the operator would have nothing to shorten.
    expect(screen.getByTestId("prompt-editor-dream")).toHaveValue("shorter");
  });
});
