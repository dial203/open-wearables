import { useMemo, useState } from 'react';
import { Plus } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useOAuthProviders } from '@/hooks/api/use-oauth-providers';
import { useOAuthConnect } from '@/hooks/use-oauth-connect';
import type { UserConnection } from '@/lib/api/types';

interface AddProviderAccountDialogProps {
  userId: string;
  /** The user's existing connections, used to show how many accounts each provider already has. */
  connections: UserConnection[];
}

/**
 * Link another provider account to this user.
 *
 * The distinction this dialog exists for: connecting a provider the user is
 * already connected to used to mean "re-authorize", because a user could only
 * have one account per provider. It can now also mean "add a second one", and
 * only the person clicking knows which they meant - so the flow is explicit,
 * and carries the name and e-mail that will identify the new account from the
 * moment it lands rather than leaving a second unlabelled row behind.
 */
export function AddProviderAccountDialog({
  userId,
  connections,
}: AddProviderAccountDialogProps) {
  const [open, setOpen] = useState(false);
  const [provider, setProvider] = useState<string>('');
  const [label, setLabel] = useState('');
  const [email, setEmail] = useState('');

  const { data: apiProviders, isLoading } = useOAuthProviders(true, true);
  const { connect, connectionState } = useOAuthConnect({
    userId,
    // Back to this user's page, so an operator linking three accounts in a row
    // is not bounced to the participant-facing pairing screen each time.
    redirectUri: `${window.location.origin}/users/${userId}`,
  });

  const accountCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const connection of connections) {
      if (connection.status !== 'active') continue;
      counts.set(
        connection.provider,
        (counts.get(connection.provider) ?? 0) + 1
      );
    }
    return counts;
  }, [connections]);

  const existingCount = provider ? (accountCounts.get(provider) ?? 0) : 0;

  const handleSubmit = () => {
    if (!provider) return;
    // newAccount is what stops the callback re-pointing an account the user
    // already has. The backend still refuses to duplicate an account it
    // recognises by its provider user id, so this cannot create a twin of one.
    connect(provider, {
      newAccount: true,
      accountLabel: label.trim() || undefined,
      accountEmail: email.trim() || undefined,
    });
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <Plus className="h-4 w-4" />
          Add account
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Link a provider account</DialogTitle>
          <DialogDescription>
            Connects another account for this user. A user may hold several
            accounts with the same provider — two watches worn at once, say —
            and each keeps its own tokens, its own sync and its own data.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label htmlFor="add-account-provider">Provider</Label>
            <select
              id="add-account-provider"
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm outline-none focus:ring-1 focus:ring-ring"
            >
              <option value="">
                {isLoading ? 'Loading…' : 'Choose a provider'}
              </option>
              {(apiProviders ?? []).map((p) => {
                const count = accountCounts.get(p.provider) ?? 0;
                return (
                  <option key={p.provider} value={p.provider}>
                    {p.name}
                    {count > 0
                      ? ` — ${count} account${count > 1 ? 's' : ''} linked`
                      : ''}
                  </option>
                );
              })}
            </select>
            {existingCount > 0 && (
              <p className="text-[11px] text-muted-foreground">
                This will be account {existingCount + 1}. Sign in to the{' '}
                <em>other</em> account when the provider asks — signing in to
                the one already linked re-authorizes it instead of adding one.
              </p>
            )}
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="add-account-label">Account name</Label>
            <Input
              id="add-account-label"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="e.g. P01 left wrist"
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="add-account-email">Account e-mail</Label>
            <Input
              id="add-account-email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="e.g. p01.left@lab.example.edu"
            />
            <p className="text-[11px] text-muted-foreground">
              Some providers report the account e-mail themselves and it will be
              filled in for you; for the rest this is the only record of which
              login the data came from.
            </p>
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button
            disabled={!provider || connectionState === 'connecting'}
            onClick={handleSubmit}
          >
            {connectionState === 'connecting'
              ? 'Redirecting…'
              : 'Continue to provider'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
