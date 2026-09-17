import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import {
  deviceService,
  type DeviceCreate,
  type DeviceUpdate,
} from '@/lib/api/services/device.service';
import { queryKeys } from '@/lib/query/keys';

/**
 * Device registry hooks.
 *
 * Every mutation invalidates the device list, its history and the proposal queue
 * together: an edit writes an audit entry, and a merge or split changes which
 * devices exist, so refreshing only the list would leave the history drawer and the
 * proposal queue showing a state that no longer holds.
 */

export function useDevices(userId: string, includeRetired = true) {
  return useQuery({
    queryKey: queryKeys.devices.list(userId, includeRetired),
    queryFn: () => deviceService.list(userId, includeRetired),
    enabled: !!userId,
  });
}

export function useDeviceHistory(
  userId: string,
  deviceId?: string,
  enabled = true
) {
  return useQuery({
    queryKey: queryKeys.devices.history(userId, deviceId),
    queryFn: () => deviceService.history(userId, { deviceId, limit: 200 }),
    enabled: !!userId && enabled,
  });
}

export function useLinkProposals(userId: string) {
  return useQuery({
    queryKey: queryKeys.devices.proposals(userId),
    queryFn: () => deviceService.proposals(userId),
    enabled: !!userId,
  });
}

function useDeviceMutation<TArgs, TResult>(
  userId: string,
  fn: (args: TArgs) => Promise<TResult>,
  successMessage: (args: TArgs, result: TResult) => string
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: (result, args) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.devices.all });
      // Attribution feeds the data-source list too, which shows which device each
      // source belongs to.
      queryClient.invalidateQueries({
        queryKey: queryKeys.priorities.dataSources(userId),
      });
      toast.success(successMessage(args, result));
    },
    onError: (error: Error) => toast.error(error.message),
  });
}

export function useCreateDevice(userId: string) {
  return useDeviceMutation(
    userId,
    (data: DeviceCreate) => deviceService.create(userId, data),
    () => 'Device created'
  );
}

export function useUpdateDevice(userId: string) {
  return useDeviceMutation(
    userId,
    ({ deviceId, data }: { deviceId: string; data: DeviceUpdate }) =>
      deviceService.update(userId, deviceId, data),
    () => 'Device updated'
  );
}

export function useRetireDevice(userId: string) {
  return useDeviceMutation(
    userId,
    ({
      deviceId,
      retired,
      effectiveAt,
      reason,
    }: {
      deviceId: string;
      retired: boolean;
      effectiveAt?: string | null;
      reason?: string | null;
    }) =>
      deviceService.setRetired(userId, deviceId, retired, effectiveAt, reason),
    ({ retired }) => (retired ? 'Device retired' : 'Device reactivated')
  );
}

export function useLinkDataSource(userId: string) {
  return useDeviceMutation(
    userId,
    ({
      deviceId,
      dataSourceId,
      reason,
    }: {
      deviceId: string;
      dataSourceId: string;
      reason?: string | null;
    }) => deviceService.link(userId, deviceId, dataSourceId, reason),
    () => 'Data source linked'
  );
}

export function useUnlinkDataSource(userId: string) {
  return useDeviceMutation(
    userId,
    ({
      deviceId,
      dataSourceId,
      reason,
    }: {
      deviceId: string;
      dataSourceId: string;
      reason?: string | null;
    }) => deviceService.unlink(userId, deviceId, dataSourceId, reason),
    () => 'Data source unlinked'
  );
}

export function useMergeDevices(userId: string) {
  return useDeviceMutation(
    userId,
    ({
      keepDeviceId,
      absorbDeviceId,
      reason,
    }: {
      keepDeviceId: string;
      absorbDeviceId: string;
      reason?: string | null;
    }) => deviceService.merge(userId, keepDeviceId, absorbDeviceId, reason),
    () => 'Devices merged'
  );
}

export function useSplitDevice(userId: string) {
  return useDeviceMutation(
    userId,
    ({
      deviceId,
      dataSourceIds,
      reason,
    }: {
      deviceId: string;
      dataSourceIds: string[];
      reason?: string | null;
    }) => deviceService.split(userId, deviceId, dataSourceIds, reason),
    () => 'Device split'
  );
}

export function useRefreshProposals(userId: string) {
  return useDeviceMutation(
    userId,
    () => deviceService.refreshProposals(userId),
    (_args, result) =>
      result.total === 0
        ? 'No cross-route matches found'
        : `${result.total} suggestion${result.total === 1 ? '' : 's'} to review`
  );
}

export function useDecideProposal(userId: string) {
  return useDeviceMutation(
    userId,
    ({
      proposalId,
      accepted,
      reason,
    }: {
      proposalId: string;
      accepted: boolean;
      reason?: string | null;
    }) => deviceService.decideProposal(userId, proposalId, accepted, reason),
    ({ accepted }) =>
      accepted ? 'Devices linked and merged' : 'Suggestion dismissed'
  );
}
