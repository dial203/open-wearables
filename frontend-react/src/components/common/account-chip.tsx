import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { providerLabel } from '@/components/common/source-badge';
import { cn } from '@/lib/utils';
import {
  ACCOUNT_TYPE_CLASSES,
  UNCLASSIFIED_CLASSES,
  type AccountDescriptor,
} from '@/lib/utils/account';

/**
 * Names the provider account a row of data arrived through.
 *
 * Shown only where it would otherwise be ambiguous — see `hasSiblings`. One
 * account with a provider makes the chip pure noise, because the provider badge
 * beside it already says everything the chip would.
 */
export function AccountChip({
  account,
  className = '',
}: {
  account: AccountDescriptor;
  className?: string;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          className={cn(
            'shrink-0 rounded border px-1.5 py-0.5 text-[10px] font-medium',
            account.type
              ? (ACCOUNT_TYPE_CLASSES[account.type] ?? UNCLASSIFIED_CLASSES)
              : UNCLASSIFIED_CLASSES,
            className
          )}
        >
          {account.name ?? `${account.typeLabel} ${account.index}`}
        </span>
      </TooltipTrigger>
      <TooltipContent>
        <div className="space-y-0.5">
          <div>
            {providerLabel(account.provider)} account {account.index} of{' '}
            {account.count}
          </div>
          <div className="text-muted-foreground">{account.typeLabel}</div>
          {account.email && (
            <div className="text-muted-foreground">{account.email}</div>
          )}
          {account.devices.length > 0 && (
            <div className="text-muted-foreground">
              {account.devices.join(', ')}
            </div>
          )}
        </div>
      </TooltipContent>
    </Tooltip>
  );
}
