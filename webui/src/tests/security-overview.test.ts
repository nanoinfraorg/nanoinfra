import { describe, expect, it } from "vitest";

import { remoteWorkSummary } from "@/lib/security-overview";
import type { GatesPayload, GatesPolicy, GatesScopePolicy } from "@/lib/types";

/**
 * The level the Security section shows for remote work -- nanoinfraorg/nanoinfra#87.
 *
 * One phrase summarises five capability classes by three scope tiers by three execution
 * contexts, so the phrase can lie. It is derived from the weakest decision and never from an
 * average, and that rule is what these tests hold. An average would understate risk, which is
 * the one failure this row must not have.
 *
 * The derivation is pure, so every row of the level table is a test with no rendering.
 */

function scopes(over: Partial<GatesScopePolicy> = {}): GatesScopePolicy {
  return { host: "deny", group: "deny", all: "deny", ...over };
}

/** The shipped defaults refuse every remote action, so each test widens what it needs. */
function policy(over: Partial<GatesPolicy> = {}): GatesPolicy {
  return {
    approvers: [],
    approvalPaths: ["webui"],
    interactive: {
      "mutate.remote": scopes(),
      "mutate.inventory": "deny",
      "credential.access": "deny",
    },
    unattended: {
      "mutate.remote": scopes(),
      "mutate.inventory": "deny",
      "credential.access": "deny",
    },
    standingGrants: [],
    audit: { retentionDays: 90, recordCommandText: false },
    ...over,
  };
}

function gates(over: Partial<GatesPolicy> = {}): GatesPayload {
  return {
    policy: policy(over),
    choices: {
      "mutate.remote": ["allow", "approve", "grant", "deny"],
      "mutate.inventory": ["allow", "deny"],
      "credential.access": ["allow", "approve", "grant", "deny"],
      all: ["deny"],
    },
  };
}

/** One standing grant, in the shape the gateway sends. */
function grant(id: string) {
  return { id, contexts: ["unattended"], hosts: ["web-1"], commands: ["systemctl restart web"] };
}

describe("remoteWorkSummary", () => {
  it("reads an unattended allow as an agent that runs remote commands alone", () => {
    const summary = remoteWorkSummary(gates({
      interactive: {
        "mutate.remote": scopes({ host: "approve", group: "approve" }),
        "mutate.inventory": "allow",
        "credential.access": "approve",
      },
      unattended: {
        "mutate.remote": scopes({ host: "allow" }),
        "mutate.inventory": "deny",
        "credential.access": "deny",
      },
    }));

    expect(summary).toEqual({
      level: "unattendedAllow",
      clauses: [{ kind: "everyContext" }],
    });
  });

  it("reads an interactive allow as a remote command that runs without asking", () => {
    const summary = remoteWorkSummary(gates({
      interactive: {
        "mutate.remote": scopes({ host: "allow", group: "approve" }),
        "mutate.inventory": "allow",
        "credential.access": "approve",
      },
    }));

    expect(summary).toEqual({
      level: "interactiveAllow",
      clauses: [{ kind: "interactiveOnly" }],
    });
  });

  it("reads an approve with no allow as a person who answers each remote command", () => {
    const summary = remoteWorkSummary(gates({
      interactive: {
        "mutate.remote": scopes({ host: "approve", group: "approve" }),
        "mutate.inventory": "allow",
        "credential.access": "approve",
      },
    }));

    expect(summary).toEqual({
      level: "approve",
      clauses: [{ kind: "interactiveOnly" }],
    });
  });

  it("reads deny in every context and scope as a refusal", () => {
    expect(remoteWorkSummary(gates())).toEqual({
      level: "deny",
      clauses: [{ kind: "everyScopeDenied" }],
    });
  });

  it("reads a payload with no gate block as a gateway that offers no gates", () => {
    expect(remoteWorkSummary(undefined)).toEqual({
      level: "unavailable",
      clauses: [{ kind: "noPolicy" }],
    });
    expect(remoteWorkSummary(null)).toEqual({
      level: "unavailable",
      clauses: [{ kind: "noPolicy" }],
    });
  });

  it("takes the weakest decision and never the average of the matrix", () => {
    // Five denials and one allow. An average reads almost refused. The weakest decision is
    // the allow, and the level says so.
    const summary = remoteWorkSummary(gates({
      interactive: {
        "mutate.remote": scopes({ host: "allow" }),
        "mutate.inventory": "deny",
        "credential.access": "deny",
      },
    }));

    expect(summary.level).toBe("interactiveAllow");
  });

  it("prefers the unattended allow when both contexts hold one", () => {
    // An unattended remote command has no human at all, so it is the worse state and it wins.
    const summary = remoteWorkSummary(gates({
      interactive: {
        "mutate.remote": scopes({ host: "allow" }),
        "mutate.inventory": "allow",
        "credential.access": "approve",
      },
      unattended: {
        "mutate.remote": scopes({ group: "allow" }),
        "mutate.inventory": "deny",
        "credential.access": "deny",
      },
    }));

    expect(summary.level).toBe("unattendedAllow");
  });

  it("does not read a grant decision as a refusal", () => {
    // `grant` means "allow when a standing grant matches". It is not `deny`, so it cannot read
    // as refused.
    const summary = remoteWorkSummary(gates({
      unattended: {
        "mutate.remote": scopes({ host: "grant" }),
        "mutate.inventory": "deny",
        "credential.access": "deny",
      },
    }));

    expect(summary.level).toBe("approve");
  });

  it("names how many standing grants skip an approval, before every other clause", () => {
    // The clause is not optional. A row that read "a person approves each remote command"
    // while three grants run unattended would be false. The caption truncates, so this clause
    // reads first: it is the one item that must not be cut.
    const summary = remoteWorkSummary(gates({
      interactive: {
        "mutate.remote": scopes({ host: "approve", group: "approve" }),
        "mutate.inventory": "allow",
        "credential.access": "approve",
      },
      standingGrants: [grant("nightly"), grant("patch"), grant("restart")],
    }));

    expect(summary).toEqual({
      level: "approve",
      clauses: [
        { kind: "grantsBypass", count: 3 },
        { kind: "interactiveOnly" },
      ],
    });
  });

  it("says a grant matches nothing when every decision is deny", () => {
    // A grant answers `grant` and `approve`, and it never answers `deny`. With every decision
    // denied a grant permits nothing, so a caption that claimed it runs would overstate.
    const summary = remoteWorkSummary(gates({ standingGrants: [grant("nightly")] }));

    expect(summary).toEqual({
      level: "deny",
      clauses: [
        { kind: "grantsInert", count: 1 },
        { kind: "everyScopeDenied" },
      ],
    });
  });

  it("carries an unattended credential allow into the caption", () => {
    const summary = remoteWorkSummary(gates({
      unattended: {
        "mutate.remote": scopes(),
        "mutate.inventory": "deny",
        "credential.access": "allow",
      },
    }));

    expect(summary.clauses).toEqual([
      { kind: "credentialAllow" },
      { kind: "everyScopeDenied" },
    ]);
  });

  it("carries an interactive credential allow into the caption", () => {
    const summary = remoteWorkSummary(gates({
      interactive: {
        "mutate.remote": scopes({ host: "approve" }),
        "mutate.inventory": "allow",
        "credential.access": "allow",
      },
    }));

    expect(summary.clauses).toEqual([
      { kind: "credentialAllow" },
      { kind: "interactiveOnly" },
    ]);
  });

  it("says which contexts the level applies to", () => {
    const unattendedOnly = remoteWorkSummary(gates({
      unattended: {
        "mutate.remote": scopes({ host: "approve" }),
        "mutate.inventory": "deny",
        "credential.access": "deny",
      },
    }));
    expect(unattendedOnly.clauses).toEqual([{ kind: "unattendedOnly" }]);

    const both = remoteWorkSummary(gates({
      interactive: {
        "mutate.remote": scopes({ host: "approve" }),
        "mutate.inventory": "allow",
        "credential.access": "approve",
      },
      unattended: {
        "mutate.remote": scopes({ host: "grant" }),
        "mutate.inventory": "deny",
        "credential.access": "deny",
      },
    }));
    expect(both.clauses).toEqual([{ kind: "everyContext" }]);
  });

  it("refuses to claim a level when a decision the level covers is absent", () => {
    // An absent decision is unknown, and unknown must not read as refused. #86 keeps a
    // cosmetic field from taking a panel down; this field is the security claim itself, so
    // the honest answer is that the level is not available.
    const missingContext = gates();
    delete (missingContext.policy as { unattended?: unknown }).unattended;
    expect(remoteWorkSummary(missingContext).level).toBe("unavailable");

    const missingClass = gates();
    delete (missingClass.policy.interactive as { "mutate.remote"?: unknown })["mutate.remote"];
    expect(remoteWorkSummary(missingClass).level).toBe("unavailable");

    const missingScope = gates();
    delete (missingScope.policy.unattended["mutate.remote"] as { group?: unknown }).group;
    expect(remoteWorkSummary(missingScope).level).toBe("unavailable");
  });

  it("survives a policy that is absent from the gate block", () => {
    const empty = { choices: gates().choices } as GatesPayload;
    expect(remoteWorkSummary(empty).level).toBe("unavailable");
  });

  it("counts no grant when the gateway sends no grant list", () => {
    const noGrants = gates();
    delete (noGrants.policy as { standingGrants?: unknown }).standingGrants;

    expect(remoteWorkSummary(noGrants)).toEqual({
      level: "deny",
      clauses: [{ kind: "everyScopeDenied" }],
    });
  });
});
