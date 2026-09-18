import { createFileRoute } from '@tanstack/react-router';
import { motion, AnimatePresence } from 'motion/react';
import {
  ChevronRight,
  Check,
  AlertCircle,
  X,
  Lock,
  Loader2,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useOAuthConnect } from '@/hooks/use-oauth-connect';
import { useOAuthProviders } from '@/hooks/api/use-oauth-providers';
import { useUserConnections } from '@/hooks/api/use-health';
import { useMemo, useState } from 'react';
import { API_CONFIG } from '@/lib/api/config';
import { isAuthenticated } from '@/lib/auth/session';

export const Route = createFileRoute('/users/$userId/pair/')({
  component: PairWearablePage,
  validateSearch: (search: Record<string, unknown>) => ({
    redirect_url:
      typeof search.redirect_url === 'string' && search.redirect_url.length > 0
        ? search.redirect_url
        : undefined,
  }),
});

function PairWearablePage() {
  const { userId } = Route.useParams();
  const { redirect_url: redirectUrl } = Route.useSearch();

  const { connectionState, connectingProvider, error, connect, reset } =
    useOAuthConnect({ userId, redirectUrl });

  const { data: apiProviders, isLoading } = useOAuthProviders(true, true);
  const { data: connections } = useUserConnections(userId, isAuthenticated());

  // How many accounts are already linked per provider, not merely whether one
  // is: a second Whoop on the same person is a normal thing to want, so an
  // already-connected provider stays clickable and adds another account.
  const accountCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const connection of connections ?? []) {
      if (connection.status !== 'active') continue;
      counts.set(
        connection.provider,
        (counts.get(connection.provider) ?? 0) + 1
      );
    }
    return counts;
  }, [connections]);

  const displayProviders = useMemo(() => {
    if (!apiProviders) return [];
    return apiProviders.map((apiProvider) => {
      const linkedCount = accountCounts.get(apiProvider.provider) ?? 0;
      return {
        id: apiProvider.provider,
        name: apiProvider.name,
        description: linkedCount
          ? `${linkedCount} account${linkedCount > 1 ? 's' : ''} linked`
          : 'Connect your device',
        logoPath: apiProvider.icon_url
          ? `${API_CONFIG.baseUrl}${apiProvider.icon_url}`
          : '',
        isAvailable: apiProvider.is_enabled,
        linkedCount,
      };
    });
  }, [apiProviders, accountCounts]);

  const connectingProviderData = connectingProvider
    ? displayProviders.find((p) => p.id === connectingProvider)
    : null;

  // Tapping a provider opens a short step for the account details before the
  // redirect, rather than going straight to the provider. Two reasons: most
  // providers never tell us the e-mail of the account that authorized (Garmin,
  // Polar, Suunto, Strava, Withings do not), and this is the only moment the
  // person who knows it is present; and if they are wearing two of the same
  // brand, the label is what will tell the two apart afterwards.
  const [pendingProvider, setPendingProvider] = useState<{
    id: string;
    name: string;
    linkedCount: number;
  } | null>(null);
  const [accountEmail, setAccountEmail] = useState('');
  const [accountLabel, setAccountLabel] = useState('');

  const openAccountStep = (
    providerId: string,
    name: string,
    linkedCount: number
  ) => {
    if (connectingProvider !== null) return;
    setAccountEmail('');
    setAccountLabel('');
    setPendingProvider({ id: providerId, name, linkedCount });
  };

  const handleConnect = () => {
    if (!pendingProvider || connectingProvider !== null) return;
    connect(pendingProvider.id, {
      // Only meaningful for providers that report no user id of their own. For
      // the rest the callback recognises a second account from the provider's
      // identifier, which is what makes this page correct for a participant:
      // it is unauthenticated, so `linkedCount` is 0 for them whatever the
      // truth, and the flag alone could not be relied on.
      newAccount: pendingProvider.linkedCount > 0,
      accountEmail: accountEmail.trim() || undefined,
      accountLabel: accountLabel.trim() || undefined,
    });
  };

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-200 flex flex-col items-center justify-center p-6 relative overflow-hidden selection:bg-white/20">
      {/* Ambient Background Effect */}
      <div className="absolute top-0 left-0 w-full h-full bg-[radial-gradient(ellipse_80%_80%_at_50%_-20%,rgba(120,119,198,0.1),rgba(255,255,255,0))] pointer-events-none" />

      {/* Header */}
      <motion.div
        initial={{ opacity: 0, y: -10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4 }}
        className="relative z-10 text-center mb-14 space-y-3"
      >
        <h1 className="text-4xl font-medium text-white tracking-tight">
          Connect a device
        </h1>
        <p className="text-lg text-zinc-400">Select your wearable platform</p>
      </motion.div>

      {/* Error notification */}
      <AnimatePresence>
        {connectionState === 'error' && error && (
          <motion.div
            initial={{ opacity: 0, y: -10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -10 }}
            className="relative z-10 mb-6 p-4 rounded-xl bg-red-500/10 border border-red-500/30 flex items-center gap-3 max-w-4xl w-full"
          >
            <AlertCircle className="w-5 h-5 text-red-400 shrink-0" />
            <p className="text-sm text-red-300 flex-1">{error}</p>
            <Button
              variant="destructive"
              size="icon"
              onClick={reset}
              aria-label="Dismiss error"
            >
              <X className="w-5 h-5" />
            </Button>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Main content */}
      <AnimatePresence mode="wait">
        {connectionState === 'idle' && pendingProvider && (
          <motion.div
            key="account-step"
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            className="relative z-10 w-full max-w-lg rounded-2xl border border-white/10 bg-zinc-900/60 p-8"
          >
            <h2 className="text-2xl font-medium text-white">
              {pendingProvider.linkedCount > 0
                ? `Add another ${pendingProvider.name} account`
                : `Connect ${pendingProvider.name}`}
            </h2>
            <p className="mt-2 text-sm text-zinc-400">
              {pendingProvider.linkedCount > 0
                ? `${pendingProvider.linkedCount} ${pendingProvider.name} account${
                    pendingProvider.linkedCount > 1 ? 's are' : ' is'
                  } already linked. On the next screen, sign in to the account you want to add — signing in to one already linked just reconnects it.`
                : `You'll sign in to ${pendingProvider.name} on the next screen.`}
            </p>

            <div className="mt-6 space-y-5">
              <div className="space-y-1.5">
                <label
                  htmlFor="pair-account-email"
                  className="block text-sm font-medium text-zinc-300"
                >
                  Which {pendingProvider.name} account?
                </label>
                <input
                  id="pair-account-email"
                  type="email"
                  autoComplete="email"
                  value={accountEmail}
                  onChange={(e) => setAccountEmail(e.target.value)}
                  placeholder="the email you sign in with"
                  className="h-11 w-full rounded-lg border border-white/10 bg-zinc-950/60 px-3 text-sm text-zinc-100 placeholder:text-zinc-600 outline-none focus:border-white/25 focus:ring-2 focus:ring-white/10"
                />
                <p className="text-xs text-zinc-500">
                  Recorded so this data can always be traced back to the right
                  account.
                </p>
              </div>

              <div className="space-y-1.5">
                <label
                  htmlFor="pair-account-label"
                  className="block text-sm font-medium text-zinc-300"
                >
                  Label{' '}
                  <span className="font-normal text-zinc-500">(optional)</span>
                </label>
                <input
                  id="pair-account-label"
                  value={accountLabel}
                  onChange={(e) => setAccountLabel(e.target.value)}
                  placeholder="e.g. left wrist"
                  className="h-11 w-full rounded-lg border border-white/10 bg-zinc-950/60 px-3 text-sm text-zinc-100 placeholder:text-zinc-600 outline-none focus:border-white/25 focus:ring-2 focus:ring-white/10"
                />
                <p className="text-xs text-zinc-500">
                  If you wear more than one {pendingProvider.name} device, this
                  is what tells them apart.
                </p>
              </div>
            </div>

            <div className="mt-8 flex items-center justify-between gap-3">
              <Button
                variant="ghost"
                onClick={() => setPendingProvider(null)}
                className="text-zinc-400 hover:text-white"
              >
                Back
              </Button>
              <Button onClick={handleConnect}>
                Continue to {pendingProvider.name}
                <ChevronRight className="w-4 h-4" />
              </Button>
            </div>
          </motion.div>
        )}

        {connectionState === 'idle' && !pendingProvider && (
          <motion.div
            key="providers"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0, y: -10 }}
            className="relative z-10 grid grid-cols-1 md:grid-cols-2 gap-6 w-full max-w-4xl"
          >
            {isLoading ? (
              <div className="col-span-2 flex justify-center py-12">
                <Loader2 className="w-8 h-8 animate-spin text-zinc-500" />
              </div>
            ) : (
              displayProviders
                .filter((p) => p.isAvailable)
                .map((provider, index) => (
                  <motion.button
                    key={provider.id}
                    initial={{ opacity: 0, y: 10 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ delay: index * 0.05, duration: 0.3 }}
                    onClick={() =>
                      openAccountStep(
                        provider.id,
                        provider.name,
                        provider.linkedCount
                      )
                    }
                    className={`group relative flex flex-col items-center text-center p-10 rounded-2xl bg-zinc-900/40 border transition-all duration-300 ease-out outline-none focus:ring-2 focus:ring-white/20 ${
                      provider.linkedCount > 0
                        ? 'border-emerald-500/20 hover:bg-zinc-900/80'
                        : 'border-white/5 hover:bg-zinc-900/80 hover:border-white/10'
                    }`}
                  >
                    {/* Brand Logo */}
                    <div className="mb-8 flex items-center justify-center h-20 w-20 bg-white rounded-2xl shadow-lg shadow-black/20 group-hover:scale-105 transition-transform duration-300">
                      <img
                        src={provider.logoPath}
                        alt={`${provider.name} logo`}
                        className="w-14 h-14 object-contain"
                      />
                    </div>

                    {/* Text */}
                    <h3 className="text-xl font-medium text-white mb-3">
                      {provider.name}
                    </h3>
                    <p className="text-base text-zinc-500 max-w-xs leading-relaxed">
                      {provider.description}
                    </p>

                    {/* Connect indicator */}
                    <div className="mt-8 flex items-center gap-1.5 text-base font-medium transition-colors">
                      {provider.linkedCount > 0 ? (
                        <>
                          <Check className="w-4 h-4 text-emerald-400" />
                          <span className="text-emerald-400">Connected</span>
                          <span className="text-zinc-500">·</span>
                          <span className="text-zinc-300 group-hover:text-white">
                            Add another
                          </span>
                          <ChevronRight className="w-4 h-4 stroke-[1.5] text-zinc-300 group-hover:text-white" />
                        </>
                      ) : (
                        <>
                          <span className="text-zinc-200 group-hover:text-white">
                            Connect
                          </span>
                          <ChevronRight className="w-4 h-4 stroke-[1.5] text-zinc-200 group-hover:text-white" />
                        </>
                      )}
                    </div>
                  </motion.button>
                ))
            )}
          </motion.div>
        )}

        {connectionState === 'connecting' && connectingProviderData && (
          <motion.div
            key="connecting"
            initial={{ opacity: 0, scale: 0.95 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0 }}
            className="relative z-10 text-center py-12"
          >
            <div
              role="status"
              aria-live="polite"
              aria-label={`Connecting to ${connectingProviderData.name}`}
              className="w-12 h-12 mx-auto mb-4 border-2 border-white/30 border-t-white rounded-full animate-spin"
            />
            <p className="text-zinc-400">
              Connecting to {connectingProviderData.name}...
            </p>
          </motion.div>
        )}

        {connectionState === 'success' && (
          <motion.div
            key="success"
            initial={{ opacity: 0, scale: 0.9 }}
            animate={{ opacity: 1, scale: 1 }}
            transition={{ type: 'spring', stiffness: 200, damping: 15 }}
            className="relative z-10 text-center py-12"
          >
            <motion.div
              initial={{ scale: 0 }}
              animate={{ scale: 1 }}
              transition={{
                type: 'spring',
                stiffness: 200,
                damping: 12,
                delay: 0.1,
              }}
              className="w-16 h-16 mx-auto mb-4 rounded-full bg-green-500/20 flex items-center justify-center shadow-[0_0_30px_hsla(145,100%,50%,0.3)]"
            >
              <Check className="w-8 h-8 text-green-500" />
            </motion.div>
            <h2 className="text-xl font-medium text-white mb-2">Connected</h2>
            <p className="text-zinc-400 text-sm mb-6">
              Your device will start syncing shortly
            </p>
            <Button
              variant="ghost"
              onClick={reset}
              className="text-zinc-200 hover:text-white hover:bg-white/10"
            >
              Connect another device
            </Button>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Footer Security */}
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ delay: 0.3 }}
        className="mt-20 flex items-center gap-2 text-zinc-500 text-base font-normal opacity-80 hover:opacity-100 transition-opacity"
      >
        <Lock className="w-4 h-4 stroke-[1.5]" />
        <span>Your data is encrypted and secure</span>
      </motion.div>
    </div>
  );
}
