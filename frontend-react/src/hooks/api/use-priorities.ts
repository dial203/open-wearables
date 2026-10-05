import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  priorityService,
  type ProviderPriorityBulkUpdate,
  type DeviceTypePriorityBulkUpdate,
  type RelayVisibility,
} from '@/lib/api/services/priority.service';
import { queryKeys } from '@/lib/query/keys';
import { toast } from 'sonner';
import { getErrorMessage } from '@/lib/errors/handler';

// ==================== Provider Priorities ====================

export function useProviderPriorities() {
  return useQuery({
    queryKey: queryKeys.priorities.providers(),
    queryFn: () => priorityService.getProviderPriorities(),
  });
}

export function useBulkUpdateProviderPriorities() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (data: ProviderPriorityBulkUpdate) =>
      priorityService.bulkUpdateProviderPriorities(data),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.priorities.all,
      });
      toast.success('Provider priorities updated successfully');
    },
    onError: (error) => {
      toast.error(`Failed to update priorities: ${getErrorMessage(error)}`);
    },
  });
}

// ==================== Device Type Priorities ====================

export function useDeviceTypePriorities() {
  return useQuery({
    queryKey: queryKeys.priorities.deviceTypes(),
    queryFn: () => priorityService.getDeviceTypePriorities(),
  });
}

export function useBulkUpdateDeviceTypePriorities() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (data: DeviceTypePriorityBulkUpdate) =>
      priorityService.bulkUpdateDeviceTypePriorities(data),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.priorities.all,
      });
      toast.success('Device type priorities updated successfully');
    },
    onError: (error) => {
      toast.error(
        `Failed to update device priorities: ${getErrorMessage(error)}`
      );
    },
  });
}

// ==================== User Data Sources ====================

export function useUserDataSources(userId: string) {
  return useQuery({
    queryKey: queryKeys.priorities.dataSources(userId),
    queryFn: () => priorityService.getUserDataSources(userId),
    enabled: !!userId,
  });
}

export function useUpdateDataSourceEnabled() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      userId,
      dataSourceId,
      isEnabled,
    }: {
      userId: string;
      dataSourceId: string;
      isEnabled: boolean;
    }) =>
      priorityService.updateDataSourceEnabled(userId, dataSourceId, {
        is_enabled: isEnabled,
      }),
    onSuccess: async (_data, variables) => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.priorities.dataSources(variables.userId),
      });
      toast.success(
        `Data source ${variables.isEnabled ? 'enabled' : 'disabled'}`
      );
    },
    onError: (error) => {
      toast.error(`Failed to update data source: ${getErrorMessage(error)}`);
    },
  });
}

export function useSetRelayVisibility() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      userId,
      dataSourceId,
      visibility,
    }: {
      userId: string;
      dataSourceId: string;
      visibility: RelayVisibility;
    }) =>
      priorityService.setRelayVisibility(userId, dataSourceId, {
        relay_visibility: visibility,
      }),
    onSuccess: async (_data, variables) => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.priorities.dataSources(variables.userId),
      });
      toast.success(
        variables.visibility === 'always'
          ? 'Source will be shown even where the direct connection covers it'
          : variables.visibility === 'never'
            ? 'Source hidden from reads'
            : 'Source follows the duplicate rule again'
      );
    },
    onError: (error) => {
      toast.error(`Failed to update data source: ${getErrorMessage(error)}`);
    },
  });
}
