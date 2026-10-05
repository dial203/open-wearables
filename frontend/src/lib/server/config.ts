import { apiGet } from './api';
import type { Features } from '$lib/settings/tabs';

/** Mirrors `ConfigResponse`. A backend older than a flag leaves it out. */
export type AppConfig = {
	outgoing_webhooks_enabled: boolean;
	data_lifecycle_enabled?: boolean;
	email_enabled?: boolean;
};

export const fetchFeatures = async (accessToken: string): Promise<Features> => {
	const config = await apiGet<AppConfig>('/api/v1/config', accessToken);
	return {
		lifecycle: config.data_lifecycle_enabled !== false,
		email: config.email_enabled !== false
	};
};
