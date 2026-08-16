import type { GatesPayload, GatesPolicy } from "@/lib/types";

/**
 * The level the Security section shows for remote work -- nanoinfraorg/nanoinfra#87.
 *
 * The gate policy holds five capability classes by three scope tiers by three execution
 * contexts. Settings -> Overview has one row for it, so one phrase has to answer "can an agent
 * touch a host". A phrase like that can lie.
 *
 * **The level comes from the weakest decision and never from an average.** `allow` is weaker
 * than `approve`, and `approve` is weaker than `deny`. An average would understate risk, which
 * is the one failure this row must not have. The row is a glance and a door: the matrix behind
 * the door stays the detail.
 *
 * This module is pure and it holds the only derivation. The component reads the gate block once
 * and renders what this function answers, so no second reading of the policy can disagree with
 * the first.
 */

/** The five states the row can show. Each one maps to one translated phrase. */
export type RemoteWorkLevel =
  /** An unattended context permits a remote command. No person is in the path at all. */
  | "unattendedAllow"
  /** The interactive context permits a remote command. Nobody has to answer first. */
  | "interactiveAllow"
  /** A decision waits for a person, and no decision permits a command on its own. */
  | "approve"
  /** Every context and every scope refuses a remote command. */
  | "deny"
  /** This gateway sends no gate policy, so no level can be derived from it. */
  | "unavailable";

/**
 * What qualifies the level.
 *
 * `grantsBypass` and `grantsInert` carry a count. The rest carry the kind alone.
 */
export type RemoteWorkClauseKind =
  /** How many standing grants skip an approval for recurring work. */
  | "grantsBypass"
  /** How many standing grants are declared while every decision refuses them. */
  | "grantsInert"
  /** A secret reaches a model with no person in the path. */
  | "credentialAllow"
  /** The level applies to the interactive context, and unattended work is refused. */
  | "interactiveOnly"
  /** The level applies to the unattended context, and interactive work is refused. */
  | "unattendedOnly"
  /** The level applies to both contexts. */
  | "everyContext"
  /** No context and no scope permits a remote command. */
  | "everyScopeDenied"
  /** The gateway sent no policy to summarise. */
  | "noPolicy";

export interface RemoteWorkClause {
  kind: RemoteWorkClauseKind;
  /** How many standing grants. The grant clauses carry it, and no other clause does. */
  count?: number;
}

export interface RemoteWorkSummary {
  level: RemoteWorkLevel;
  /** The caption, in reading order. The renderer translates each clause and joins them. */
  clauses: RemoteWorkClause[];
}

/** The scope tiers of `mutate.remote`, in the order nanoinfra/config/gates.py declares them. */
const REMOTE_SCOPES = ["host", "group", "all"] as const;

/** The two execution contexts the policy holds. */
const CONTEXTS = ["interactive", "unattended"] as const;

type PolicyContext = (typeof CONTEXTS)[number];

/**
 * Every `mutate.remote` decision of one context, or `null` when one of them is absent.
 *
 * An absent decision is unknown, and unknown must not read as refused. The level is the
 * security claim of this row, so a partial policy answers `unavailable` instead of a level the
 * payload does not support. #86 keeps a cosmetic field from taking a panel down; this field is
 * the claim itself, and the trade runs the other way.
 *
 * The runtime payload can hold any shape, so this reads defensively even though `GatesPolicy`
 * declares each field.
 */
function remoteDecisions(policy: GatesPolicy | undefined, context: PolicyContext): string[] | null {
  const remote = policy?.[context]?.["mutate.remote"] as Record<string, unknown> | undefined;
  if (!remote) return null;
  const decisions: string[] = [];
  for (const scope of REMOTE_SCOPES) {
    const decision = remote[scope];
    if (typeof decision !== "string" || decision === "") return null;
    decisions.push(decision);
  }
  return decisions;
}

/** True when this context permits something, which is any decision other than `deny`. */
function opens(decisions: string[]): boolean {
  return decisions.some((decision) => decision !== "deny");
}

/**
 * Which contexts the level applies to.
 *
 * A context with every decision denied refuses remote work, and the clause says so. The mock in
 * #87 reads "Interactive only, unattended refused" for the shipped default.
 */
function contextClause(interactive: string[], unattended: string[]): RemoteWorkClause {
  const interactiveOpen = opens(interactive);
  const unattendedOpen = opens(unattended);
  if (interactiveOpen && unattendedOpen) return { kind: "everyContext" };
  if (interactiveOpen) return { kind: "interactiveOnly" };
  if (unattendedOpen) return { kind: "unattendedOnly" };
  return { kind: "everyScopeDenied" };
}

/**
 * Derive the level and the caption of the remote-work row.
 *
 * Pass the gate block of the settings payload. A gateway that sends no block, or a block with a
 * decision missing, answers the `unavailable` level and renders the section all the same.
 */
export function remoteWorkSummary(gates: GatesPayload | null | undefined): RemoteWorkSummary {
  const policy = gates?.policy;
  const interactive = remoteDecisions(policy, "interactive");
  const unattended = remoteDecisions(policy, "unattended");
  if (!interactive || !unattended) {
    return { level: "unavailable", clauses: [{ kind: "noPolicy" }] };
  }

  const every = [...interactive, ...unattended];
  // The weakest decision, and no average. A value this build does not know is not `deny`, so it
  // cannot read as refused: it reads at the `approve` level, and the panel behind the row shows
  // the value the gateway actually sent.
  const level: RemoteWorkLevel = unattended.includes("allow")
    ? "unattendedAllow"
    : interactive.includes("allow")
      ? "interactiveAllow"
      : opens(every)
        ? "approve"
        : "deny";

  const clauses: RemoteWorkClause[] = [];

  // A standing grant is exactly what bypasses an approval for recurring work, so this clause is
  // not optional. A row that read "a person approves each remote command" while three grants run
  // unattended would be false.
  //
  // It reads first because the caption truncates. Of every clause here, this is the one that
  // must not be cut.
  const grants = Array.isArray(policy?.standingGrants) ? policy.standingGrants.length : 0;
  if (grants > 0) {
    // A grant answers `grant` and `approve`, and it never answers `deny`
    // (nanoinfra/gates/policy.py). With every decision denied a grant permits nothing, so the
    // caption says the grants match nothing rather than claiming they run.
    const bypasses = every.some((decision) => decision === "approve" || decision === "grant");
    clauses.push({ kind: bypasses ? "grantsBypass" : "grantsInert", count: grants });
  }

  // This decision hands a secret to a model with no person in the path. A reader of a security
  // summary must not have to open a panel to find it.
  const credentialAllow = CONTEXTS.some(
    (context) => policy?.[context]?.["credential.access"] === "allow",
  );
  if (credentialAllow) clauses.push({ kind: "credentialAllow" });

  clauses.push(contextClause(interactive, unattended));

  return { level, clauses };
}
