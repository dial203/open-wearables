import { apiClient } from '../client';
import { ApiError } from '../../errors/api-error';
import type { DeviceType } from '../types';

/**
 * Device registry: the physical units behind a user's data sources.
 *
 * A data source is the immutable ingest fingerprint — one row per
 * (user, provider, device_model, source) exactly as the provider reported it. A
 * device is the editable entity several data sources point at, which is how a
 * Garmin's activities and its sleep read as one watch, and how a ring arriving
 * both from its maker's API and relayed through Apple Health can be recognised as
 * one ring. Linking groups the sources; it never merges them.
 */

export type LabelSource = 'auto' | 'manual';

export interface DeviceIdentity {
  /** Ingest route that issued the claim (a provider key). */
  route: string;
  id_kind: string;
  id_value: string;
  /** `strong` distinguishes two same-model units; `weak` names an app or a model. */
  confidence: 'strong' | 'weak';
  first_seen_at: string | null;
  last_seen_at: string | null;
}

export interface DeviceDataSource {
  id: string;
  provider: string;
  source: string | null;
  device_model: string | null;
  device_type: string | null;
  original_source_name: string | null;
  /**
   * The connected account this source arrived through, or null for a one-time
   * import. A unit re-paired to a second account of the same provider reports
   * the identical provider/source/model triple down both, so this is the only
   * thing that separates the two stretches of data.
   */
  user_connection_id: string | null;
  /** Denormalised from the connection, so the row reads on its own. */
  account_label: string | null;
  account_email: string | null;
  account_type: string | null;
}

export interface Device {
  id: string;
  user_id: string;
  /** Brand derived from what the provider reported. Never edited — see brand_display. */
  brand: string | null;
  /** The provider's own model string, verbatim. Never edited — see model_display. */
  model_raw: string | null;
  /** Hand-set or derived marketing name. Wins over model_raw wherever the device is named. */
  model_display: string | null;
  /** Hand-set brand. Wins over brand wherever the device is named. */
  brand_display: string | null;
  /**
   * The phone that relayed this device's data, as the platform reported it. Set when a
   * third-party app wrote through HealthKit or Health Connect and the only model string
   * on the row named the phone rather than this unit. Provenance, not this unit's model.
   */
  host_model_raw: string | null;
  serial: string | null;
  firmware_version: string | null;
  /** Server-derived name: label, else model_display, else model_raw, else a description. */
  display_name: string;
  device_type: DeviceType | string;
  label: string | null;
  label_source: LabelSource;
  wear_location: string | null;
  notes: string | null;
  is_active: boolean;
  retired_at: string | null;
  first_seen_at: string | null;
  last_seen_at: string | null;
  created_at: string;
  updated_at: string;
  identities: DeviceIdentity[];
  data_sources: DeviceDataSource[];
}

/** One kind of thing a source reported in the window, and how much of it. */
export interface ActivityBucket {
  /** Event category, score category, or time-series code, as stored. */
  label: string;
  count: number;
  first_at: string;
  last_at: string;
}

/**
 * What a data source has reported lately, and which account it came through.
 *
 * The registry says where a stream came from; this says what is in it. For a source
 * whose name identifies nothing — "Bluetooth Device", a bare bundle id — the shape of
 * the data is the only evidence of what the hardware is and where it is worn.
 */
export interface SourceActivity {
  data_source_id: string;
  provider: string;
  source: string | null;
  device_model: string | null;
  device_id: string | null;
  user_connection_id: string | null;
  account_label: string | null;
  account_email: string | null;
  account_type: string | null;
  events: ActivityBucket[];
  scores: ActivityBucket[];
  metrics: ActivityBucket[];
  /** Null when the source reported nothing in the window — a device that came off. */
  last_seen_at: string | null;
}

export interface SourceActivityListResponse {
  items: SourceActivity[];
  total: number;
  window_days: number;
}

export interface DeviceListResponse {
  items: Device[];
  total: number;
}

export interface DeviceCreate {
  device_type: DeviceType | string;
  brand?: string | null;
  model_raw?: string | null;
  model_display?: string | null;
  brand_display?: string | null;
  serial?: string | null;
  firmware_version?: string | null;
  label?: string | null;
  wear_location?: string | null;
  notes?: string | null;
  reason?: string | null;
}

/**
 * Only fields that are our interpretation — never what the provider reported.
 *
 * `brand`, `model_raw` and `host_model_raw` are absent on purpose: they record what the
 * provider claimed, and editing them in place would erase the only evidence of it.
 * `brand_display` and `model_display` sit beside the claim and win wherever the device
 * is named, which is what makes a relayed stream nameable at all.
 */
export interface DeviceUpdate {
  label?: string | null;
  device_type?: DeviceType | string;
  wear_location?: string | null;
  notes?: string | null;
  model_display?: string | null;
  brand_display?: string | null;
  serial?: string | null;
  firmware_version?: string | null;
  reason?: string | null;
}

export interface DeviceHistoryEntry {
  id: string;
  device_id: string | null;
  data_source_id: string | null;
  action: string;
  field: string | null;
  old_value: string | null;
  new_value: string | null;
  actor: string | null;
  reason: string | null;
  meta: Record<string, unknown> | null;
  created_at: string;
}

export interface DeviceHistoryListResponse {
  items: DeviceHistoryEntry[];
  total: number;
}

export interface LinkProposal {
  id: string;
  device_a_id: string;
  device_b_id: string;
  score: string | number;
  evidence: Record<string, unknown> | null;
  status: 'pending' | 'accepted' | 'rejected';
  decided_at: string | null;
  decided_by: string | null;
  created_at: string;
}

export interface LinkProposalListResponse {
  items: LinkProposal[];
  total: number;
}

function rethrow(error: unknown): never {
  if (error instanceof ApiError) throw error;
  throw ApiError.networkError((error as Error).message);
}

export const deviceService = {
  async list(
    userId: string,
    includeRetired = true
  ): Promise<DeviceListResponse> {
    try {
      return await apiClient.get<DeviceListResponse>(
        `/api/v1/users/${userId}/devices?include_retired=${includeRetired}`
      );
    } catch (error) {
      rethrow(error);
    }
  },

  /** What each of the user's sources reported in the last `days` days. */
  async sourceActivity(
    userId: string,
    days = 30
  ): Promise<SourceActivityListResponse> {
    try {
      return await apiClient.get<SourceActivityListResponse>(
        `/api/v1/users/${userId}/devices/source-activity?days=${days}`
      );
    } catch (error) {
      rethrow(error);
    }
  },

  async create(userId: string, data: DeviceCreate): Promise<Device> {
    try {
      return await apiClient.post<Device>(
        `/api/v1/users/${userId}/devices`,
        data
      );
    } catch (error) {
      rethrow(error);
    }
  },

  async update(
    userId: string,
    deviceId: string,
    data: DeviceUpdate
  ): Promise<Device> {
    try {
      return await apiClient.patch<Device>(
        `/api/v1/users/${userId}/devices/${deviceId}`,
        data
      );
    } catch (error) {
      rethrow(error);
    }
  },

  async setRetired(
    userId: string,
    deviceId: string,
    retired: boolean,
    effectiveAt?: string | null,
    reason?: string | null
  ): Promise<Device> {
    try {
      return await apiClient.post<Device>(
        `/api/v1/users/${userId}/devices/${deviceId}/retire`,
        { retired, effective_at: effectiveAt ?? null, reason: reason ?? null }
      );
    } catch (error) {
      rethrow(error);
    }
  },

  async link(
    userId: string,
    deviceId: string,
    dataSourceId: string,
    reason?: string | null
  ): Promise<Device> {
    try {
      return await apiClient.post<Device>(
        `/api/v1/users/${userId}/devices/${deviceId}/link`,
        { data_source_id: dataSourceId, reason: reason ?? null }
      );
    } catch (error) {
      rethrow(error);
    }
  },

  async unlink(
    userId: string,
    deviceId: string,
    dataSourceId: string,
    reason?: string | null
  ): Promise<Device> {
    try {
      return await apiClient.post<Device>(
        `/api/v1/users/${userId}/devices/${deviceId}/unlink`,
        { data_source_id: dataSourceId, reason: reason ?? null }
      );
    } catch (error) {
      rethrow(error);
    }
  },

  /** Irreversible: the absorbed device row is deleted. History keeps enough to rebuild it. */
  async merge(
    userId: string,
    keepDeviceId: string,
    absorbDeviceId: string,
    reason?: string | null
  ): Promise<Device> {
    try {
      return await apiClient.post<Device>(
        `/api/v1/users/${userId}/devices/${keepDeviceId}/merge`,
        { absorb_device_id: absorbDeviceId, reason: reason ?? null }
      );
    } catch (error) {
      rethrow(error);
    }
  },

  /** Returns the newly created device carrying the moved sources. */
  async split(
    userId: string,
    deviceId: string,
    dataSourceIds: string[],
    reason?: string | null
  ): Promise<Device> {
    try {
      return await apiClient.post<Device>(
        `/api/v1/users/${userId}/devices/${deviceId}/split`,
        { data_source_ids: dataSourceIds, reason: reason ?? null }
      );
    } catch (error) {
      rethrow(error);
    }
  },

  async history(
    userId: string,
    params?: { deviceId?: string; dataSourceId?: string; limit?: number }
  ): Promise<DeviceHistoryListResponse> {
    const query = new URLSearchParams();
    if (params?.deviceId) query.set('device_id', params.deviceId);
    if (params?.dataSourceId) query.set('data_source_id', params.dataSourceId);
    if (params?.limit) query.set('limit', String(params.limit));
    const suffix = query.toString() ? `?${query}` : '';
    try {
      return await apiClient.get<DeviceHistoryListResponse>(
        `/api/v1/users/${userId}/devices-history${suffix}`
      );
    } catch (error) {
      rethrow(error);
    }
  },

  async proposals(userId: string): Promise<LinkProposalListResponse> {
    try {
      return await apiClient.get<LinkProposalListResponse>(
        `/api/v1/users/${userId}/device-link-proposals`
      );
    } catch (error) {
      rethrow(error);
    }
  },

  async refreshProposals(userId: string): Promise<LinkProposalListResponse> {
    try {
      return await apiClient.post<LinkProposalListResponse>(
        `/api/v1/users/${userId}/device-link-proposals/refresh`,
        {}
      );
    } catch (error) {
      rethrow(error);
    }
  },

  /** Accepting merges b into a. Rejecting is permanent — the pair is never re-proposed. */
  async decideProposal(
    userId: string,
    proposalId: string,
    accepted: boolean,
    reason?: string | null
  ): Promise<LinkProposal> {
    try {
      return await apiClient.post<LinkProposal>(
        `/api/v1/users/${userId}/device-link-proposals/${proposalId}/decide`,
        { accepted, reason: reason ?? null }
      );
    } catch (error) {
      rethrow(error);
    }
  },
};
