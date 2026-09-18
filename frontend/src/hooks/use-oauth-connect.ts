import { useState, useCallback } from 'react';
import { API_CONFIG } from '@/lib/api/config';

export type OAuthConnectionState = 'idle' | 'connecting' | 'success' | 'error';

export interface UseOAuthConnectOptions {
  userId: string;
  /** OAuth callback URI (where user returns after provider OAuth) */
  redirectUri?: string;
  /** Developer's app redirect URL (passed through to success page) */
  redirectUrl?: string;
  onSuccess?: (providerId: string) => void;
  onError?: (error: string) => void;
}

/**
 * How this authorization relates to the accounts the user already has with the
 * provider. Omitted entirely, the backend behaves as it always did: it creates
 * a first account, or re-authorizes the one that exists.
 */
export interface OAuthConnectOptions {
  /** Add another account beside the ones already linked. */
  newAccount?: boolean;
  /** Re-authorize this specific account instead of adding one. */
  connectionId?: string;
  /**
   * What the account is for: personal, validation, reliability, monitoring,
   * testing, other. The structured field a study filters on.
   */
  accountType?: string;
  /** Operator-facing name for the account, e.g. "P01 arm A". Not asked of participants. */
  accountLabel?: string;
  /**
   * Login e-mail of the provider account. Worth passing for providers whose API
   * does not report one (Garmin, Polar, Suunto, Strava, Withings) - it is what
   * ties a data set back to the account it came from.
   */
  accountEmail?: string;
}

export interface UseOAuthConnectReturn {
  connectionState: OAuthConnectionState;
  connectingProvider: string | null;
  error: string | null;
  connect: (providerId: string, options?: OAuthConnectOptions) => Promise<void>;
  reset: () => void;
}

/**
 * Hook for handling OAuth connection flow to wearable providers.
 * Calls the backend authorize endpoint and redirects to provider's OAuth page.
 */
export function useOAuthConnect(
  options: UseOAuthConnectOptions
): UseOAuthConnectReturn {
  const { userId, redirectUri, redirectUrl, onSuccess, onError } = options;

  const [connectionState, setConnectionState] =
    useState<OAuthConnectionState>('idle');
  const [connectingProvider, setConnectingProvider] = useState<string | null>(
    null
  );
  const [error, setError] = useState<string | null>(null);

  const connect = useCallback(
    async (providerId: string, account?: OAuthConnectOptions) => {
      setConnectingProvider(providerId);
      setConnectionState('connecting');
      setError(null);

      try {
        const successParams = new URLSearchParams({ provider: providerId });
        if (redirectUrl) {
          successParams.set('redirect_url', redirectUrl);
        }

        const finalRedirectUri =
          redirectUri ||
          `${window.location.origin}/users/${userId}/pair/success?${successParams}`;

        const params = new URLSearchParams({
          user_id: userId,
          redirect_uri: finalRedirectUri,
        });
        if (account?.connectionId) {
          params.set('connection_id', account.connectionId);
        } else if (account?.newAccount) {
          params.set('new_account', 'true');
        }
        if (account?.accountType) {
          params.set('account_type', account.accountType);
        }
        if (account?.accountLabel) {
          params.set('account_label', account.accountLabel);
        }
        if (account?.accountEmail) {
          params.set('account_email', account.accountEmail);
        }

        const response = await fetch(
          `${API_CONFIG.baseUrl}/api/v1/oauth/${providerId}/authorize?${params}`
        );

        if (!response.ok) {
          const errorData = await response.json().catch(() => ({}));
          throw new Error(
            errorData.detail ||
              errorData.message ||
              'Failed to get authorization URL'
          );
        }

        const data = await response.json();

        onSuccess?.(providerId);

        window.location.href = data.authorization_url;
      } catch (err) {
        const errorMessage =
          err instanceof Error ? err.message : 'Connection failed';
        setError(errorMessage);
        setConnectionState('error');
        setConnectingProvider(null);
        onError?.(errorMessage);
      }
    },
    [userId, redirectUri, redirectUrl, onSuccess, onError]
  );

  const reset = useCallback(() => {
    setConnectionState('idle');
    setConnectingProvider(null);
    setError(null);
  }, []);

  return {
    connectionState,
    connectingProvider,
    error,
    connect,
    reset,
  };
}
