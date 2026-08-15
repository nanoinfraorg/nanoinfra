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
const PUBLIC_KEY_PREFIXES = ["ssh-", "ecdsa-", "sk-"];

/**
 * Say what is wrong with an ssh_key value, or null when nothing is.
 *
 * An operator pasted a public key, then pasted a private key whose newlines a single-line input
 * had replaced with spaces. Both stored fine and both answered `Permission denied` from the host,
 * which reads as a server problem. The store refuses each one now, and this repeats the check here
 * so the operator reads it before the save rather than after a failed action.
 */
export function privateKeyProblem(kind: string, value: string): string | null {
  if (kind !== "ssh_key") return null;
  const text = value.trim();
  if (text.length === 0) return null;
  const firstWord = text.split(/\s/)[0] ?? "";
  if (PUBLIC_KEY_PREFIXES.some((prefix) => firstWord.startsWith(prefix))) {
    return "This is a public key. An ssh_key secret holds the private half, so paste the key that starts with -----BEGIN.";
  }
  if (!text.startsWith("-----BEGIN")) {
    return "A private key starts with -----BEGIN. Paste the whole file, including both marker lines.";
  }
  if (!text.includes("\n")) {
    return "This key holds no line breaks, so no ssh client can parse it. Paste it again, or read it from the file with cat.";
  }
  return null;
}

export function SecretForm({ secret, onBack, onSave }: SecretFormProps) {
  const [name, setName] = useState(secret?.name ?? "");
  const [kind, setKind] = useState(secret?.kind ?? KIND_OPTIONS[0].value);
  const [providerId, setProviderId] = useState(secret?.providerId ?? PROVIDER_OPTIONS[0].value);
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The same rules the store enforces, checked before the request leaves. An operator must not
  // learn about a collapsed paste from a `Permission denied` on a host hours later.
  const needsMultiline = kind === "ssh_key";
  const valueProblem = privateKeyProblem(kind, value);
  const canSave =
    name.trim().length > 0 && value.trim().length > 0 && !saving && valueProblem === null;

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
            {/*
              A textarea, and not a single-line input. An operator pasted an SSH private key into
              the old input twice. The first time the paste could not fit, so they stored the
              public half. The second time the input replaced every newline with a space, and no
              ssh client can parse that. Both values answered `Permission denied` from the host,
              which reads as a server problem rather than a form problem.

              The value is not masked while it is typed. A key an operator pastes from their own
              file is theirs to see, and masking cost more than it bought: it hid the collapse.
              The value still never returns from the server once saved.
            */}
            <textarea
              autoComplete="off"
              spellCheck={false}
              rows={needsMultiline ? 8 : 2}
              value={value}
              onChange={(event) => setValue(event.target.value)}
              placeholder={
                needsMultiline
                  ? "-----BEGIN OPENSSH PRIVATE KEY-----\n...\n-----END OPENSSH PRIVATE KEY-----"
                  : secret
                    ? "Enter a new value to save"
                    : "Enter secret value"
              }
              className="mt-1 w-full rounded-[10px] border border-border/60 bg-muted/35 px-2.5 py-2 font-mono text-[12px] leading-5 text-foreground outline-none"
            />
            {valueProblem ? (
              <span className="mt-1 block text-[11px] leading-4 text-destructive-text">
                {valueProblem}
              </span>
            ) : null}
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
