import type { ComponentType } from "react";

import type { ChannelPresentation } from "@/components/settings/channels/catalog";
import type {
  NanoinfraFeatureInfo,
  NanoinfraFeaturesPayload,
} from "@/lib/types";

export type ChannelPluginPanelProps = {
  token: string;
  feature: NanoinfraFeatureInfo;
  actionKey: string | null;
  chatAppsDocsUrl?: string;
  showBrandLogos: boolean;
  onAction: (action: "enable" | "disable", name: string) => void;
  onFeaturesUpdate: (payload: NanoinfraFeaturesPayload) => void;
};

export type ChannelPluginConnectFlowProps = {
  token: string;
  feature: NanoinfraFeatureInfo;
  idleLabel?: string;
  connectRequestId?: number;
  onFeaturesUpdate: (payload: NanoinfraFeaturesPayload) => void;
};

export type ChannelUiContribution = {
  presentation: ChannelPresentation;
  aliases?: Record<string, Partial<ChannelPresentation>>;
  Panel?: ComponentType<ChannelPluginPanelProps>;
  ConnectFlow?: ComponentType<ChannelPluginConnectFlowProps>;
  canConnectBeforeConfigured?: boolean;
};

export type RegisteredChannelUiContribution = {
  channel: string;
  webui: string;
  contribution: ChannelUiContribution;
};
