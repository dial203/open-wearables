import type { UserConnection } from '@/lib/api/types';
import { accountTypeLabel } from '@/lib/api/types';

/** Everything needed to name one connected account on screen. */
export interface AccountDescriptor {
  id: string;
  provider: string;
  /** Classification label ("Validation"), or "Unclassified". */
  typeLabel: string;
  /** The raw classification, for colouring. */
  type: UserConnection['account_type'];
  /** Operator-set name, provider username, or e-mail — whichever exists. */
  name: string | null;
  email: string | null;
  /** True when the user holds more than one account with this provider. */
  hasSiblings: boolean;
  /** 1-based position among this provider's accounts. */
  index: number;
  count: number;
  /**
   * Device models this account reports, or the manually-set one when the
   * provider reports none. Most recent first.
   */
  devices: string[];
}

/**
 * Index a user's connections by id, so any view holding a `user_connection_id`
 * can name the account without another request.
 *
 * Views that show samples side by side need this: once a participant holds two
 * accounts with one provider, the provider name and the device model are
 * identical across both, and the account is the only thing that separates them.
 */
export function buildAccountMap(
  connections: UserConnection[] | undefined
): Map<string, AccountDescriptor> {
  const map = new Map<string, AccountDescriptor>();
  if (!connections) return map;

  const perProvider = new Map<string, number>();
  for (const c of connections) {
    perProvider.set(c.provider, (perProvider.get(c.provider) ?? 0) + 1);
  }

  for (const c of connections) {
    const count = c.account_count ?? perProvider.get(c.provider) ?? 1;
    const devices =
      c.observed_devices && c.observed_devices.length > 0
        ? c.observed_devices
        : c.device_label
          ? [c.device_label]
          : [];
    map.set(c.id, {
      id: c.id,
      provider: c.provider,
      typeLabel: accountTypeLabel(c.account_type),
      type: c.account_type ?? null,
      name: c.account_label || c.provider_username || c.account_email || null,
      email: c.account_email ?? null,
      hasSiblings: count > 1,
      index: c.account_index ?? 1,
      count,
      devices,
    });
  }
  return map;
}

/** Tailwind classes per classification, so a validation arm reads the same everywhere. */
export const ACCOUNT_TYPE_CLASSES: Record<string, string> = {
  personal: 'bg-sky-500/15 text-sky-400 border-sky-500/25',
  validation: 'bg-violet-500/15 text-violet-300 border-violet-500/25',
  reference: 'bg-rose-500/15 text-rose-300 border-rose-500/25',
  reliability: 'bg-amber-500/15 text-amber-300 border-amber-500/25',
  monitoring: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/25',
  testing: 'bg-zinc-500/15 text-zinc-400 border-zinc-500/25',
  other: 'bg-muted/50 text-muted-foreground border-border/60',
};

export const UNCLASSIFIED_CLASSES =
  'bg-muted/40 text-muted-foreground border-dashed border-border/60';
