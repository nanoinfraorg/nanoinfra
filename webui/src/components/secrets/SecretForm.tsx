import { useCallback, useState } from "react";
import { ChevronLeft, Save } from "lucide-react";

import { Input } from "@/components/ui/input";
import { ApiError, type SecretSummary } from "@/lib/api";
import type { SecretValues } from "@/hooks/useSecrets";

const KIND_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "password", label: "Password" },
  { value: "api_key", label: "API key" },
  { value: "ssh_key", label: "SSH key" },
  { value: "token", label: "Token" },
];

const PROVIDER_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "local", label: "Local" },
  { value: "postgres", label: "Postgres" },
];

interface SecretFormProps {
  secret: SecretSummary | null;
  onBack: () => void;
  onSave: (values: SecretValues) => Promise<void>;
}

/**
 * Create/edit form for a single secret. The backend's update is a full
 * replace (not a patch), and `value` is write-only and never returned by the
 * API — so this form always starts with an empty value field, even when
 * editing, and always requires a fresh value before it will save.
 */
export function SecretForm({ secret, onBack, onSave }: SecretFormProps) {
  const [name, setName] = useState(secret?.name ?? "");
  const [kind, setKind] = useState(secret?.kind ?? KIND_OPTIONS[0].value);
  const [providerId, setProviderId] = useState(secret?.providerId ?? PROVIDER_OPTIONS[0].value);
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSave = name.trim().length > 0 && value.trim().length > 0 && !saving;

  const handleSave = useCallback(async () => {
    if (!canSave) return;
    setSaving(true);
    setError(null);
    try {
      await onSave({ name: name.trim(), kind, providerId, value });
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // e.message carries the real backend detail (crypto.py distinguishes
        // "unset" from "set but not a valid Fernet key: <reason>") -- don't
        // collapse both cases into one generic string.
        setError(e.message || "Secrets isn't configured on this server (missing NANOINFRA_SECRETS_KEY).");
      } else {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setSaving(false);
    }
  }, [canSave, name, kind, providerId, value, onSave]);

  return (
    <div className="flex h-full min-h-0 w-full flex-col">
      <div className="flex items-center gap-2 border-b border-border px-4 py-3">
        <button
          type="button"
          onClick={onBack}
          aria-label="Back to secrets"
          className="flex h-8 w-8 items-center justify-center rounded-full text-muted-foreground hover:bg-muted/70 hover:text-foreground"
        >
          <ChevronLeft className="h-4 w-4" />
        </button>
        <span className="text-[14px] font-semibold text-foreground">
          {secret ? "Edit secret" : "New secret"}
        </span>
      </div>

      <div className="min-h-0 flex-1 overflow-auto p-4">
        <div className="mx-auto flex max-w-md flex-col gap-3.5">
          <label className="block">
            <span className="text-[11px] font-medium text-foreground/85">Name</span>
            <Input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="e.g. prod-db-password"
              className="mt-1 h-9 rounded-[10px] border-border/60 bg-muted/35 text-[13px]"
            />
          </label>

          <label className="block">
            <span className="text-[11px] font-medium text-foreground/85">Kind</span>
            <select
              value={kind}
              onChange={(event) => setKind(event.target.value)}
              className="mt-1 h-9 w-full rounded-[10px] border border-border/60 bg-muted/35 px-2.5 text-[13px] text-foreground outline-none"
            >
              {KIND_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
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

          <label className="block">
            <span className="text-[11px] font-medium text-foreground/85">
              {secret ? "New value" : "Value"}
            </span>
            <Input
              type="password"
              autoComplete="off"
              value={value}
              onChange={(event) => setValue(event.target.value)}
              placeholder={secret ? "Enter a new value to save" : "Enter secret value"}
              className="mt-1 h-9 rounded-[10px] border-border/60 bg-muted/35 text-[13px]"
            />
            <span className="mt-1 block text-[11px] leading-4 text-muted-foreground">
              {secret
                ? "Secret values are never displayed again once saved — updating always requires a fresh value."
                : "Stored encrypted server-side. It will never be shown again once saved."}
            </span>
          </label>

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
