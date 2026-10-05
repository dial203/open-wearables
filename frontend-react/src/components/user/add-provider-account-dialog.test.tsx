// @vitest-environment node
import { describe, it, expect, vi } from 'vitest';
import { renderToString } from 'react-dom/server';

vi.mock('@/hooks/api/use-oauth-providers', () => ({
  useOAuthProviders: () => ({ data: [], isLoading: false }),
}));

const { AddProviderAccountDialog } =
  await import('./add-provider-account-dialog');

describe('AddProviderAccountDialog', () => {
  // The user page renders on the server first, where there is no window. A read of
  // window.location during render threw there, and React fell back to rendering the
  // whole page on the client.
  it('renders on the server', () => {
    expect(typeof window).toBe('undefined');
    expect(() =>
      renderToString(
        <AddProviderAccountDialog userId="user-1" connections={[]} />
      )
    ).not.toThrow();
  });
});
