/**
 * The agent a channel binds for every message it delivers -- `channels.<name>.agent`.
 *
 * Named agents shipped with three ways to choose one -- the composer's picker, an automation's
 * binding, and `@agent:<name>` in the text -- and no way to say "everything arriving on Telegram
 * is answered by `sre`". On nine of the ten channels every turn was answered by the deployment
 * default unless the sender happened to type a mention.
 *
 * The control is `AgentSelectField`, the same one the automation binding uses: an operator who has
 * bound an agent to a job should recognise the one that binds an agent to a channel.
 *
 * **Its own section, not a row of the credentials form.** Everything in *Required setup* is a
 * credential the platform issued. This is a nanoinfra routing decision, and it is not required.
 * It is not in *Advanced* either: a default that decides who answers every message is not an
 * advanced field, and an operator who never opens that disclosure would never learn it exists.
 *
 * **The section disappears when the deployment names no agents**, which is every deployment until
 * an operator writes a roster -- the same rule the mention prefix follows, and the automation
 * picker. A menu whose only entry is the default is a control that cannot do anything.
 */
import {
  AgentSelectField,
  DEPLOYMENT_DEFAULT_AGENT,
} from "@/components/settings/AutomationAgentField";
import type { NamedAgentSummary } from "@/lib/api";

/** The channel whose agent is chosen per message. Mirrors `CHANNEL_AGENT_REFUSED` on the server. */
export const CHANNEL_AGENT_REFUSED = "websocket";

export function ChannelAgentField({
  channel,
  agents,
  value,
  onChange,
  tx,
}: {
  channel: string;
  agents: NamedAgentSummary[];
  /** The bound agent, or `DEPLOYMENT_DEFAULT_AGENT` for none. */
  value: string;
  onChange: (agent: string) => void;
  tx: (key: string, fallback: string) => string;
}) {
  if (agents.length === 0) return null;

  const title = (
    <div className="text-[13px] font-semibold text-foreground">
      {tx("settings.channels.agentSection", "Answering agent")}
    </div>
  );

  // The WebSocket channel has no such field: the composer picks per message, and the envelope
  // omits the key when it picks nothing -- so a channel-wide default would answer as that agent
  // for every turn where the operator chose nothing. Config refuses the field outright, and this
  // says so rather than leaving a gap that reads as a channel that forgot the feature.
  if (channel === CHANNEL_AGENT_REFUSED) {
    return (
      <section className="border-t border-border/60 px-4 py-3">
        {title}
        <p className="mt-1 text-[12.5px] leading-5 text-muted-foreground">
          {tx(
            "settings.channels.agentPerMessage",
            "Chosen for each message in the composer, so this channel binds no agent.",
          )}
        </p>
      </section>
    );
  }

  return (
    <section className="border-t border-border/60 px-4 py-3">
      {title}
      <p className="mt-1 mb-2.5 text-[12.5px] leading-5 text-muted-foreground">
        {tx(
          "settings.channels.agentHelp",
          "Every message from this channel is answered by this agent. A sender can still name another with @agent:<name>.",
        )}
      </p>
      <AgentSelectField
        agents={agents}
        value={value}
        onChange={onChange}
        label={tx("settings.channels.agentLabel", "Answers as")}
        defaultOptionLabel={tx("settings.channels.agentDefault", "Default agent")}
        help={tx(
          "settings.channels.agentDefaultHelp",
          "The deployment default answers when this channel names no agent.",
        )}
      />
    </section>
  );
}

export { DEPLOYMENT_DEFAULT_AGENT };
