import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import {
  channelUiContribution,
  channelUiOwner,
  channelUiPresentation,
  registeredChannelUiContributions,
} from "@/channel-plugins/registry";

describe("channel UI contributions", () => {
  it("selects channel-owned UI only through the backend manifest entry", () => {
    // A manifest entry is required: no entry, or one naming a file the package does not ship,
    // resolves to nothing.
    expect(channelUiContribution("slack", undefined)).toBeUndefined();
    expect(channelUiContribution("slack", "webui/missing.tsx")).toBeUndefined();
    expect(channelUiContribution("missing", "webui/index.ts")).toBeUndefined();

    const registrations = registeredChannelUiContributions();
    const channels = registrations.map((entry) => entry.channel);
    expect(channels.length).toBeGreaterThan(0);
    expect(new Set(channels).size).toBe(channels.length);
    expect(registrations.every((entry) => /^webui\/index\.tsx?$/.test(entry.webui))).toBe(true);
    expect(channelUiContribution("slack", "webui/index.ts")?.presentation.displayName).toBe("Slack");
  });

  it("no shipped channel contributes a React panel or connect flow", () => {
    // Feishu and weixin were the only two, and both were removed with the other Asia-market
    // channels. The contribution mechanism stays because it is what a future interactive channel
    // would register, so this asserts the current truth rather than a subject that no longer exists.
    const withReactUi = registeredChannelUiContributions().filter((entry) => {
      const contribution = channelUiContribution(entry.channel, entry.webui);
      return contribution?.Panel !== undefined || contribution?.ConnectFlow !== undefined;
    });
    expect(withReactUi).toEqual([]);
  });

  it("resolves an unclaimed alias to itself rather than throwing", () => {
    // The alias table lived in the removed channels' contributions, so nothing claims "lark" now.
    // Presentation has no entry; the owner lookup falls back to the name it was given.
    expect(channelUiPresentation("lark")).toBeUndefined();
    expect(channelUiOwner("lark")).toBe("lark");
  });

  it("keeps the core setup panel independent of concrete channel plugins", () => {
    const source = readFileSync(
      resolve(process.cwd(), "src/components/settings/channels/ChannelSetupPanel.tsx"),
      "utf8",
    );

    // The panel must not name a concrete channel. Kept as a general shape rather than a list of
    // the two channels that used to be hardcoded here.
    expect(source).not.toMatch(/feature\.name\s*===\s*["'][a-z]+["']/);
    // `registry` and `types` are the shared modules; a per-channel import would be the coupling
    // this guards, and naming the two allowed ones keeps that true without listing channels.
    expect(source).not.toMatch(/channel-plugins\/(?!registry|types)[a-z]/);
  });

  it("discovers UI contributions only from channel-owned packages", () => {
    const source = readFileSync(
      resolve(process.cwd(), "src/channel-plugins/registry.ts"),
      "utf8",
    );

    expect(source).toContain("../../../nanoinfra/channels/*/webui/**/*.{ts,tsx}");
    expect(source).not.toContain('"./*/index.tsx"');
  });

  it("derives channel identity from the package directory", () => {
    // Read whatever ships rather than naming channels, so removing one is not a test edit.
    const registrations = registeredChannelUiContributions();
    expect(registrations.length).toBeGreaterThan(0);
    for (const entry of registrations) {
      const source = readFileSync(
        resolve(process.cwd(), `../nanoinfra/channels/${entry.channel}/${entry.webui}`),
        "utf8",
      );
      expect(source).not.toMatch(/\bchannel\s*:/);
    }
  });

  it("includes channel-owned UI in Tailwind's production scan", () => {
    const source = readFileSync(resolve(process.cwd(), "tailwind.config.js"), "utf8");

    expect(source).toContain("../nanoinfra/channels/*/webui/**/*.{ts,tsx}");
  });
});
