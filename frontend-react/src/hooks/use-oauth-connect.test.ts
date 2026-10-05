// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useOAuthConnect } from './use-oauth-connect';

afterEach(() => {
  vi.unstubAllGlobals();
});

/** The redirect_uri the hook sent to the backend's authorize endpoint. */
async function sentRedirectUri(
  options: Parameters<typeof useOAuthConnect>[0]
): Promise<string | null> {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    // Same origin, so the hook's final navigation stays inside jsdom.
    json: async () => ({ authorization_url: '#authorize' }),
  });
  vi.stubGlobal('fetch', fetchMock);

  const { result } = renderHook(() => useOAuthConnect(options));
  await act(() => result.current.connect('garmin', { newAccount: true }));

  const url = new URL(fetchMock.mock.calls[0][0] as string);
  return url.searchParams.get('redirect_uri');
}

describe('useOAuthConnect', () => {
  it('resolves a redirect path against the current origin', async () => {
    const uri = await sentRedirectUri({
      userId: 'user-1',
      redirectPath: '/users/user-1',
    });
    expect(uri).toBe(`${window.location.origin}/users/user-1`);
  });

  it('prefers a full redirect URI when one is given', async () => {
    const uri = await sentRedirectUri({
      userId: 'user-1',
      redirectUri: 'https://app.example.org/back',
      redirectPath: '/users/user-1',
    });
    expect(uri).toBe('https://app.example.org/back');
  });

  it('falls back to the pairing success page', async () => {
    const uri = await sentRedirectUri({ userId: 'user-1' });
    expect(uri).toBe(
      `${window.location.origin}/users/user-1/pair/success?provider=garmin`
    );
  });
});
