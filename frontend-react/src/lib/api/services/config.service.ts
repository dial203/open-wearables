import { apiClient } from '../client';
import { API_ENDPOINTS } from '../config';

export interface AppConfig {
  outgoing_webhooks_enabled: boolean;
  /**
   * Whether reads leave out an aggregator's copy of a brand that is also connected
   * directly (Garmin or Oura arriving through Apple Health, say). Instance-wide,
   * set with RELAY_DEDUP_ENABLED in the backend environment. Older backends do not
   * send it, so treat `undefined` as on, which is the backend default.
   */
  relay_dedup_enabled?: boolean;
}

export const configService = {
  async get(): Promise<AppConfig> {
    return apiClient.get<AppConfig>(API_ENDPOINTS.config);
  },
};
