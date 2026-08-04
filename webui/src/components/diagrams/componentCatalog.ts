// Wire shapes for the dynamic component catalog served by the backend
// (`GET /api/webui/diagrams/catalog`, see `nanoinfra/diagrams/catalog.py`).
// Nothing here is hardcoded data anymore — see `useComponentCatalog.ts` for
// where the catalog actually gets fetched. `findComponentType`/`findProvider`
// stay as plain, parameterized lookups (not hooks) so non-component code
// like `diagramToText.ts` can use them too.

export type ProviderKind = "docker" | "api";

// How nanoinfra's agent actually operates a provider — orthogonal to `kind`
// (which describes deployment shape: self-hosted container vs. managed
// cloud API). `skillInstalled`/`skillEnabled` are computed live by the
// backend against the real Skills system; they're never part of a catalog
// file's own data.
export interface ProviderIntegration {
  type: "skill" | "api" | "internal";
  skillName?: string;
  skillInstalled?: boolean;
  skillEnabled?: boolean;
}

export interface ProviderField {
  key: string;
  label: string;
  placeholder?: string;
  kind: "text" | "secret";
  // When a node of this Component Type is connected to this one via an
  // edge (either direction), the Inspector shows that connection as this
  // field's value instead of free text — the diagram's own wiring becomes
  // the source of truth once it exists, rather than a second, disconnected
  // way to say the same thing.
  linkedComponentType?: string;
}

export interface ComponentProvider {
  id: string;
  label: string;
  kind: ProviderKind;
  fields: ProviderField[];
  integration?: ProviderIntegration;
}

export interface ComponentType {
  id: string;
  label: string;
  category: string;
  // Free-form server data now, not a closed union — see icons.ts for the
  // fallback icon shown when a key isn't in COMPONENT_ICONS.
  iconKey: string;
  providers: ComponentProvider[];
  // Replaces the old hardcoded `GROUP_COMPONENT_ID` sentinel comparison —
  // callers check this flag on the resolved type instead of a magic string.
  isGroup?: boolean;
}

/** GET /api/webui/diagrams/catalog response shape. */
export interface DiagramCatalogPayload {
  componentTypes: ComponentType[];
}

export function findComponentType(
  componentTypes: ComponentType[],
  id: string,
): ComponentType | undefined {
  return componentTypes.find((c) => c.id === id);
}

export function findProvider(
  componentTypes: ComponentType[],
  componentTypeId: string,
  providerId: string,
): ComponentProvider | undefined {
  return findComponentType(componentTypes, componentTypeId)?.providers.find((p) => p.id === providerId);
}
