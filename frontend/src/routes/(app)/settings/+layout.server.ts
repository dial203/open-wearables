import { fetchFeatures } from '$lib/server/config';
import { requireToken } from '$lib/server/guard';
import { optional } from '$lib/server/optional';
import type { LayoutServerLoad } from './$types';

// Unreadable config shows the tab: its own page reports what is wrong.
export const load: LayoutServerLoad = async ({ locals }) => ({
	features: await optional(fetchFeatures(await requireToken(locals)), {
		lifecycle: true,
		email: true
	})
});
