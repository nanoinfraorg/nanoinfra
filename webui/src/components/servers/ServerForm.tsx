import { useCallback, useEffect, useMemo, useState } from "react";
import { ChevronLeft, Plus, Save, Trash2 } from "lucide-react";

import { ServerNotesPanel } from "./ServerNotesPanel";
import { Input } from "@/components/ui/input";
import { fetchSecrets, type ServerDetail, type ServerValues, type SecretSummary } from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";

const PROVIDER_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "ssh", label: "SSH" },
  { value: "ansible-runner", label: "Ansible Runner" },
  { value: "ssm", label: "SSM" },
  { value: "api", label: "API" },
];

const NO_SECRET_VALUE = "";

interface ServerFormProps {
  server: ServerDetail | null;
  onBack: () => void;
  onSave: (values: ServerValues) => Promise<void>;
}

/**
 * Create/edit form for a single server. `secretRef` is always a dropdown
 * sourced from the Secrets list (by id) — never a free-text field, and never
 * loads/displays any secret's actual value.
 */
export function ServerForm({ server, onBack, onSave }: ServerFormProps) {
  const { getToken } = useClient();
  const [name, setName] = useState(server?.name ?? "");
  const [providerId, setProviderId] = useState(server?.providerId ?? PROVIDER_OPTIONS[0].value);
  const [config, setConfig] = useState<Array<{ key: string; value: string }>>(() =>
    Object.entries(server?.config ?? {}).map(([key, value]) => ({ key, value })),
  );
  const [secretRef, setSecretRef] = useState<string>(server?.secretRef ?? NO_SECRET_VALUE);
  const [tagsInput, setTagsInput] = useState((server?.tags ?? []).join(", "));
  const [secrets, setSecrets] = useState<SecretSummary[]>([]);
  const [secretsError, setSecretsError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void fetchSecrets(getToken())
      .then((payload) => {
        if (!cancelled) setSecrets(payload.secrets);
      })
      .catch((e: unknown) => {
        if (!cancelled) setSecretsError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [getToken]);

  const tags = useMemo(
    () =>
      tagsInput
        .split(",")
        .map((tag) => tag.trim())
        .filter((tag) => tag.length > 0),
    [tagsInput],
  );

  const canSave = name.trim().length > 0 && !saving;

  const handleConfigKeyChange = useCallback((index: number, key: string) => {
    setConfig((prev) => prev.map((row, i) => (i === index ? { ...row, key } : row)));
  }, []);

  const handleConfigValueChange = useCallback((index: number, value: string) => {
    setConfig((prev) => prev.map((row, i) => (i === index ? { ...row, value } : row)));
  }, []);

  const handleAddConfigRow = useCallback(() => {
    setConfig((prev) => [...prev, { key: "", value: "" }]);
  }, []);

  const handleRemoveConfigRow = useCallback((index: number) => {
    setConfig((prev) => prev.filter((_, i) => i !== index));
  }, []);

  const handleSave = useCallback(async () => {
    if (!canSave) return;
    setSaving(true);
    setError(null);
    try {
      const configRecord: Record<string, string> = {};
      for (const row of config) {
        const key = row.key.trim();
        if (!key) continue;
        configRecord[key] = row.value;
      }
      await onSave({
        name: name.trim(),
        providerId,
        config: configRecord,
        secretRef: secretRef ? secretRef : null,
        tags,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }, [canSave, name, providerId, config, secretRef, tags, onSave]);

  return (
    <div className="flex h-full min-h-0 w-full flex-col">
      <div className="flex items-center gap-2 border-b border-border px-4 py-3">
        <button
          type="button"
          onClick={onBack}
          aria-label="Back to servers"
          className="flex h-8 w-8 items-center justify-center rounded-full text-muted-foreground hover:bg-muted/70 hover:text-foreground"
        >
          <ChevronLeft className="h-4 w-4" />
        </button>
        <span className="text-[14px] font-semibold text-foreground">
          {server ? "Edit server" : "New server"}
        </span>
      </div>

      <div className="min-h-0 flex-1 overflow-auto p-4">
        <div className="mx-auto flex max-w-md flex-col gap-3.5">
          <label className="block">
            <span className="text-[11px] font-medium text-foreground/85">Name</span>
            <Input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="e.g. prod-web-01"
              className="mt-1 h-9 rounded-[10px] border-border/60 bg-muted/35 text-[13px]"
            />
          </label>

          <label className="block">
            <span className="text-[11px] font-medium text-foreground/85">Provider</span>
            <select
              value={providerId}
              onChange={(event) => setProviderId(event.target.value)}
              className="mt-1 h-9 w-full rounded-[10px] border border-border/60 bg-muted/35 px-2.5 text-[13px] text-foreground outline-none"
            >
              {PROVIDER_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>

          <div className="block">
            <span className="flex items-center justify-between gap-2 text-[11px] font-medium text-foreground/85">
              <span>Config</span>
              <button
                type="button"
                onClick={handleAddConfigRow}
                className="flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium text-muted-foreground hover:bg-muted/70 hover:text-foreground"
              >
                <Plus className="h-3 w-3" /> Add field
              </button>
            </span>
            <div className="mt-1 flex flex-col gap-1.5">
              {config.length === 0 ? (
                <span className="text-[11px] text-muted-foreground">
                  No config fields yet — e.g. host, port, username for an SSH server.
                </span>
              ) : (
                config.map((row, index) => (
                  <div key={index} className="flex items-center gap-1.5">
                    <Input
                      value={row.key}
                      onChange={(event) => handleConfigKeyChange(index, event.target.value)}
                      placeholder="key"
                      className="h-9 w-2/5 rounded-[10px] border-border/60 bg-muted/35 text-[13px]"
                    />
                    <Input
                      value={row.value}
                      onChange={(event) => handleConfigValueChange(index, event.target.value)}
                      placeholder="value"
                      className="h-9 flex-1 rounded-[10px] border-border/60 bg-muted/35 text-[13px]"
                    />
                    <button
                      type="button"
                      onClick={() => handleRemoveConfigRow(index)}
                      aria-label="Remove field"
                      className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-muted-foreground hover:bg-destructive/10 hover:text-destructive-text"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                ))
              )}
            </div>
          </div>

          <label className="block">
            <span className="text-[11px] font-medium text-foreground/85">Secret</span>
            <select
              value={secretRef}
              onChange={(event) => setSecretRef(event.target.value)}
              className="mt-1 h-9 w-full rounded-[10px] border border-border/60 bg-muted/35 px-2.5 text-[13px] text-foreground outline-none"
            >
              <option value={NO_SECRET_VALUE}>None</option>
              {secrets.map((secret) => (
                <option key={secret.id} value={secret.id}>
                  {secret.name}
                </option>
              ))}
            </select>
            <span className="mt-1 block text-[11px] leading-4 text-muted-foreground">
              {secretsError
                ? `Couldn't load secrets: ${secretsError}`
                : "Points at a secret by id — manage its value from the Secrets page."}
            </span>
          </label>

          <label className="block">
            <span className="text-[11px] font-medium text-foreground/85">Tags</span>
            <Input
              value={tagsInput}
              onChange={(event) => setTagsInput(event.target.value)}
              placeholder="comma, separated, tags"
              className="mt-1 h-9 rounded-[10px] border-border/60 bg-muted/35 text-[13px]"
            />
          </label>

          {/* Only for a saved server: notes are keyed by id (#223), and a record being created
              has no id yet. */}
          {server ? <ServerNotesPanel serverId={server.id} /> : null}

          {error ? <span className="text-[12px] text-destructive-text">{error}</span> : null}

          <div className="mt-1 flex items-center gap-2">
            <button
              type="button"
              onClick={() => void handleSave()}
              disabled={!canSave}
              className="flex h-9 items-center gap-1.5 rounded-full border border-border/45 bg-settings-surface px-3.5 text-[12.5px] font-medium text-foreground hover:bg-muted/70 disabled:cursor-not-allowed disabled:opacity-60"
            >
              <Save className="h-3.5 w-3.5" /> {saving ? "Saving…" : "Save"}
            </button>
            <button
              type="button"
              onClick={onBack}
              className="flex h-9 items-center rounded-full px-3.5 text-[12.5px] font-medium text-muted-foreground hover:bg-muted/70 hover:text-foreground"
            >
              Cancel
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
