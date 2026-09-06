/**
 * Rates go where the model is added — nanoinfraorg/nanoinfra#235.
 *
 * The cost card used to tell the operator to hand-edit `config.json` and type a
 * `"<provider>/<model>"` key the UI already knows. That is a correct sentence and a bad feature:
 * a rate that only exists in a file nobody opens is a rate nobody sets, and an unset rate makes
 * the whole Cost column a column of dashes.
 *
 * The properties pinned here are the ones a careless build would get wrong:
 *
 * - **an empty field is not a rate of zero.** Those are the two facts the editor exists to keep
 *   apart, which is why the draft holds strings and not numbers.
 * - **clearing is explicit**, because `0` is a valid rate and means free.
 * - the cache warning fires **only** when the model actually read cached tokens and the cache rate
 *   is zero, because that is when the cost silently excludes most of the volume.
 * - the live figure recomputes from what is typed, before saving — a rate in a box is
 *   unverifiable, the same rate against the recorded month is checkable against an invoice.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, describe, it, vi } from "vitest";

import { SettingsView } from "@/components/settings/SettingsView";
import { ClientProvider } from "@/providers/ClientProvider";
import { settingsPayload } from "@/tests/fixtures/settings-payload";
import type { SettingsPayload } from "@/lib/types";

function jsonResponse(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as Response;
}

type Preset = SettingsPayload["model_presets"][number];
type Provider = SettingsPayload["providers"][number];

function preset(over: Partial<Preset> = {}): Preset {
  return {
    name: "coding",
    label: "Coding",
    active: true,
    is_default: false,
    model: "kimi-k3",
    provider: "moonshot",
    resolved_provider: "moonshot",
    max_tokens: 8192,
    context_window_tokens: 262_144,
    temperature: 0.1,
    reasoning_effort: null,
    pricing: null,
    pricing_source: null,
    shares_pricing_with: [],
    ...over,
  } as Preset;
}

function provider(over: Partial<Provider> = {}): Provider {
  return {
    name: "moonshot",
    label: "Moonshot",
    configured: true,
    api_key_hint: "sk-…test",
    api_base: "",
    advanced_fields: [],
    pricing: null,
    pricing_free: false,
    is_local: false,
    ...over,
  } as Provider;
}

/** The shared fixture, with the model, provider and usage rows this suite is about. */
function payloadWith(presets: Preset[], providers: Provider[] = [provider()]): SettingsPayload {
  const base = settingsPayload();
  return {
    ...base,
    agent: {
      ...base.agent,
      model: presets[0]?.model ?? "kimi-k3",
      provider: presets[0]?.provider ?? "moonshot",
      resolved_provider: presets[0]?.provider ?? "moonshot",
      model_preset: presets[0]?.name ?? "coding",
    },
    model_presets: presets,
    model_call_order: [presets[0]?.name ?? "coding"],
    model_call_order_editable: true,
    providers,
    usage: {
      days: [],
      total_tokens: 5_700_000,
      total_tokens_30d: 5_700_000,
      total_tokens_365d: 5_700_000,
      peak_day_tokens: 5_700_000,
      current_streak_days: 1,
      longest_streak_days: 1,
      active_days_30d: 1,
      requests_30d: 4,
      window_days: 30,
      // The row the cache warning and the live figure are computed from. 1M in, 200k out,
      // 4M cached reads, 500k cache writes.
      providers_30d: [{
        provider: presets[0]?.provider ?? "moonshot",
        model: presets[0]?.model ?? "kimi-k3",
        total_tokens: 5_700_000,
        // The logical input, which includes the cached halves: 1M fresh + 4M read + 0.5M written.
        prompt_tokens: 5_500_000,
        completion_tokens: 200_000,
        cached_tokens: 4_000_000,
        provider_tokens: 5_700_000,
        estimated_tokens: 0,
        requests: 4,
        failed_requests: 0,
        ttft_ms: 0,
        timed_requests: 0,
        generation_ms: 0,
        measured_completion_tokens: 0,
        cache_write_tokens: 500_000,
      }],
      updated_at: null,
    },
  };
}

/** Every route the panel touches, answered with the payload under test. */
function stubFetch(settings: SettingsPayload) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => jsonResponse(settings)),
  );
}

function renderModels(settings: SettingsPayload) {
  stubFetch(settings);
  return render(
    <ClientProvider client={{} as never} token="tok">
      <SettingsView
        theme="light"
        initialSection="models"
        initialSettings={settings}
        onToggleTheme={() => {}}
        onBackToChat={() => {}}
        onModelNameChange={() => {}}
        onSettingsChange={() => {}}
        onSectionChange={() => {}}
        onLogout={() => {}}
        onRestart={() => {}}
      />
    </ClientProvider>,
  );
}

/** Expand a provider's panel, where its default rates live. */
async function openProvider(label: string) {
  fireEvent.click(await screen.findByRole("button", { name: new RegExp(label) }));
  return screen.findByTestId("provider-pricing");
}

/** …and then its pricing disclosure, whose testid is on the wrapper rather than the button. */
async function openProviderPricing(label: string) {
  const block = await openProvider(label);
  fireEvent.click(within(block).getByRole("button"));
  return block;
}

/** Open the model card, then its Pricing row. */
async function openPricing() {
  const editor = await screen.findByRole("button", { name: /Coding/ });
  fireEvent.click(editor);
  fireEvent.click(await screen.findByTestId("model-pricing-row"));
  return screen.findByTestId("model-pricing-fields");
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("the Pricing row on a model card", () => {
  it("says a model is not priced without opening anything", async () => {
    renderModels(payloadWith([preset()]));

    fireEvent.click(await screen.findByRole("button", { name: /Coding/ }));
    const row = await screen.findByTestId("model-pricing-row");

    expect(row).toHaveTextContent("Not priced");
  });

  it("summarises the two rates a reader cares about when they are set", async () => {
    renderModels(
      payloadWith([
        preset({
          pricing: {
            inputPerMtok: 0.6,
            outputPerMtok: 2.5,
            cacheReadPerMtok: 0.15,
            cacheWritePerMtok: 0.75,
          },
          pricing_source: "model",
        }),
      ]),
    );

    fireEvent.click(await screen.findByRole("button", { name: /Coding/ }));

    expect(await screen.findByTestId("model-pricing-row")).toHaveTextContent(
      "$0.60 in · $2.50 out",
    );
  });

  it("marks a summary that came from the provider default", async () => {
    renderModels(
      payloadWith([
        preset({
          pricing: {
            inputPerMtok: 0.5,
            outputPerMtok: 1.0,
            cacheReadPerMtok: 0,
            cacheWritePerMtok: 0,
          },
          pricing_source: "provider",
        }),
      ]),
    );

    fireEvent.click(await screen.findByRole("button", { name: /Coding/ }));

    expect(await screen.findByTestId("model-pricing-row")).toHaveTextContent(
      "from the provider default",
    );
  });
});

describe("the four fields", () => {
  it("seeds empty fields for an unpriced model rather than zeros", async () => {
    // Seeding zeros would turn "not priced" into "free" the moment somebody pressed save.
    renderModels(payloadWith([preset()]));

    const fields = await openPricing();

    expect(within(fields).getByLabelText("Input")).toHaveValue(null);
    expect(within(fields).getByLabelText("Cache read")).toHaveValue(null);
  });

  it("shows the live figure for the recorded month as the rates are typed", async () => {
    // 1M *fresh* in at $0.60 is $0.60 before anything else is priced — the cached 4M and the
    // written 0.5M are their own buckets and their rates are still empty. A rate in a box is
    // unverifiable; this is the number an operator can hold against an invoice.
    renderModels(payloadWith([preset()]));

    const fields = await openPricing();
    fireEvent.change(within(fields).getByLabelText("Input"), { target: { value: "0.60" } });

    await waitFor(() => {
      expect(screen.getByTestId("model-pricing-preview")).toHaveTextContent("$0.60");
    });
  });

  it("warns when the model reads cached tokens and cache read is still zero", async () => {
    renderModels(payloadWith([preset()]));

    const fields = await openPricing();
    fireEvent.change(within(fields).getByLabelText("Input"), { target: { value: "0.60" } });

    const warning = await screen.findByTestId("model-pricing-cache-warning");
    expect(warning).toHaveTextContent("4,000,000");
  });

  it("drops the warning once the cache rate is stated", async () => {
    renderModels(payloadWith([preset()]));

    const fields = await openPricing();
    fireEvent.change(within(fields).getByLabelText("Input"), { target: { value: "0.60" } });
    await screen.findByTestId("model-pricing-cache-warning");

    fireEvent.change(within(fields).getByLabelText("Cache read"), { target: { value: "0.15" } });

    await waitFor(() => {
      expect(screen.queryByTestId("model-pricing-cache-warning")).not.toBeInTheDocument();
    });
  });

  it("names the other configurations that share the same bill", async () => {
    renderModels(
      payloadWith([
        preset({ shares_pricing_with: ["Creative"] }),
        preset({ name: "creative", label: "Creative", active: false }),
      ]),
    );

    const fields = await openPricing();

    expect(within(fields).getByTestId("model-pricing-shared")).toHaveTextContent("Creative");
    expect(within(fields).getByTestId("model-pricing-shared")).toHaveTextContent(
      "moonshot/kimi-k3",
    );
  });
});

describe("saving", () => {
  it("sends the typed rates as four flat parameters", async () => {
    renderModels(payloadWith([preset()]));

    const fields = await openPricing();
    fireEvent.change(within(fields).getByLabelText("Input"), { target: { value: "0.60" } });
    fireEvent.change(within(fields).getByLabelText("Output"), { target: { value: "2.50" } });
    fireEvent.click(await screen.findByRole("button", { name: /^Save/ }));

    await waitFor(() => {
      const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
      const url = calls.map(([input]) => String(input)).find((value) =>
        value.startsWith("/api/settings/model-configurations/update")
      );
      expect(url).toContain("inputPerMtok=0.6");
      expect(url).toContain("outputPerMtok=2.5");
    });
  });

  it("does not send a rate for a field left empty", async () => {
    // An empty field means unchanged, not zero. Sending `0` would reprice the model to free.
    renderModels(payloadWith([preset()]));

    const fields = await openPricing();
    fireEvent.change(within(fields).getByLabelText("Input"), { target: { value: "0.60" } });
    fireEvent.click(await screen.findByRole("button", { name: /^Save/ }));

    await waitFor(() => {
      const url = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls
        .map(([input]) => String(input))
        .find((value) => value.startsWith("/api/settings/model-configurations/update"));
      expect(url).toContain("inputPerMtok=0.6");
      expect(url).not.toContain("cacheReadPerMtok");
    });
  });

  it("sends an explicit zero, because zero is a price and means free", async () => {
    renderModels(payloadWith([preset()]));

    const fields = await openPricing();
    fireEvent.change(within(fields).getByLabelText("Input"), { target: { value: "0" } });
    fireEvent.click(await screen.findByRole("button", { name: /^Save/ }));

    await waitFor(() => {
      const url = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls
        .map(([input]) => String(input))
        .find((value) => value.startsWith("/api/settings/model-configurations/update"));
      expect(url).toContain("inputPerMtok=0");
    });
  });

  it("asks to clear when a priced model's fields are all emptied", async () => {
    renderModels(
      payloadWith([
        preset({
          pricing: {
            inputPerMtok: 0.6,
            outputPerMtok: 2.5,
            cacheReadPerMtok: 0.15,
            cacheWritePerMtok: 0.75,
          },
          pricing_source: "model",
        }),
      ]),
    );

    const fields = await openPricing();
    fireEvent.click(within(fields).getByTestId("model-pricing-clear"));
    fireEvent.click(await screen.findByRole("button", { name: /^Save/ }));

    await waitFor(() => {
      const url = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls
        .map(([input]) => String(input))
        .find((value) => value.startsWith("/api/settings/model-configurations/update"));
      expect(url).toContain("clearPricing=1");
    });
  });
});

describe("the provider default", () => {
  it("offers one checkbox for a local provider instead of one edit per model", async () => {
    renderModels(
      payloadWith([preset({ provider: "ollama" })], [
        provider({ name: "ollama", label: "Ollama", is_local: true }),
      ]),
    );

    await openProviderPricing("Ollama");

    expect(await screen.findByTestId("provider-pricing-free")).toBeInTheDocument();
    expect(screen.getByText(/one edit instead of one per model/)).toBeInTheDocument();
  });

  it("hides the four fields once everything is declared free", async () => {
    renderModels(
      payloadWith([preset({ provider: "ollama" })], [
        provider({ name: "ollama", label: "Ollama", is_local: true }),
      ]),
    );
    await openProviderPricing("Ollama");

    fireEvent.click(await screen.findByTestId("provider-pricing-free"));

    await waitFor(() => {
      expect(
        screen.queryByLabelText(/Default pricing for this provider — Input/),
      ).not.toBeInTheDocument();
    });
  });

  it("says a provider has no default rather than implying free", async () => {
    renderModels(payloadWith([preset()]));

    const block = await openProvider("Moonshot");

    expect(block).toHaveTextContent("No default");
  });
});

describe("the live figure matches the server", () => {
  it("does not bill a cached token twice", async () => {
    /*
     * `prompt_tokens` is the logical input and includes the cached halves. A preview that charged
     * it at the input rate *and* the cached count at the cache rate would over-bill by 5.7x on
     * real Kimi K3 rates — and would disagree with the table beside it, which is worse than
     * having no preview at all.
     *
     * The fixture is 5.5M prompt = 1M fresh + 4M cached + 0.5M written.
     */
    renderModels(payloadWith([preset()]));

    const fields = await openPricing();
    fireEvent.change(within(fields).getByLabelText("Input"), { target: { value: "3.00" } });
    fireEvent.change(within(fields).getByLabelText("Cache read"), { target: { value: "0.30" } });

    // 1M × $3.00 + 4M × $0.30 = $4.20. The double-count would read $17.70.
    await waitFor(() => {
      expect(screen.getByTestId("model-pricing-preview")).toHaveTextContent("$4.20");
    });
  });
});
