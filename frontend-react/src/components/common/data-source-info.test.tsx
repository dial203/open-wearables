// @vitest-environment jsdom
import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';
import type { SourceMetadata, UserConnection } from '@/lib/api/types';

const connections: UserConnection[] = [
  {
    id: 'conn-garmin',
    user_id: 'user-1',
    provider: 'garmin',
    account_email: 'p01@lab.example.edu',
    account_index: 1,
    account_count: 1,
    status: 'active',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  },
  {
    id: 'conn-polar',
    user_id: 'user-1',
    provider: 'polar',
    account_email: null,
    account_index: 1,
    account_count: 1,
    status: 'active',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  },
];

vi.mock('@/hooks/api/use-health', () => ({
  useUserConnections: (userId: string, enabled = true) => ({
    data: userId && enabled ? connections : undefined,
  }),
}));

const idle = { mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false };
vi.mock('@/hooks/api/use-devices', () => ({
  useDevices: () => ({ data: { items: [], total: 0 } }),
  useSourceActivity: () => ({ data: undefined }),
  useLinkDataSource: () => idle,
  useCreateDevice: () => idle,
}));

const { DataSourceInfo } = await import('./data-source-info');

afterEach(cleanup);

// A model-less Garmin sleep source: what the Sleep tab shows as "Device info not available".
function garmin(overrides: Partial<SourceMetadata> = {}): SourceMetadata {
  return {
    provider: 'garmin',
    source: 'garmin',
    device: null,
    device_name: null,
    device_type: null,
    data_source_id: 'ds-garmin',
    user_connection_id: 'conn-garmin',
    device_id: null,
    ...overrides,
  };
}

describe('DataSourceInfo', () => {
  it('spells out the account e-mail when given the user', () => {
    render(<DataSourceInfo source={garmin()} userId="user-1" />);
    expect(screen.getByText('p01@lab.example.edu')).toBeTruthy();
  });

  it('says so when the account has no e-mail on file', () => {
    render(
      <DataSourceInfo
        source={garmin({
          provider: 'polar',
          source: 'polar',
          user_connection_id: 'conn-polar',
        })}
        userId="user-1"
      />
    );
    expect(screen.getByText('no e-mail on file')).toBeTruthy();
  });

  it('marks a per-source row tied to no account', () => {
    render(
      <DataSourceInfo
        source={garmin({ user_connection_id: null })}
        userId="user-1"
      />
    );
    expect(screen.getByText('no account')).toBeTruthy();
  });

  it('claims nothing about the account of a daily aggregate', () => {
    // Aggregates pool sources and carry neither id - no account is not the same as
    // not knowing.
    render(
      <DataSourceInfo
        source={garmin({ data_source_id: null, user_connection_id: null })}
        userId="user-1"
      />
    );
    expect(screen.queryByText('no account')).toBeNull();
    expect(screen.queryByText('Map device')).toBeNull();
  });

  it('offers to map an unattributed source to a device', () => {
    render(<DataSourceInfo source={garmin()} userId="user-1" />);
    expect(screen.getByText('Device info not available')).toBeTruthy();
    expect(screen.getByRole('button', { name: /map device/i })).toBeTruthy();
  });

  it('offers only a quiet correction once the source has a device', () => {
    render(
      <DataSourceInfo
        source={garmin({
          device_id: 'dev-1',
          device_display_name: 'Sub 01 Venu X1',
        })}
        userId="user-1"
      />
    );
    expect(screen.queryByText('Map device')).toBeNull();
    expect(screen.getByRole('button', { name: 'Change device' })).toBeTruthy();
  });

  it('opens the link dialog without toggling the row it sits in', () => {
    const onRowClick = vi.fn();
    render(
      <div onClick={onRowClick}>
        <DataSourceInfo source={garmin()} userId="user-1" />
      </div>
    );
    fireEvent.click(screen.getByRole('button', { name: /map device/i }));
    expect(screen.getByText('Link a data source')).toBeTruthy();
    // The dialog is portalled, but React still bubbles its events to this row.
    fireEvent.click(screen.getByText('Link a data source'));
    expect(onRowClick).not.toHaveBeenCalled();
  });

  it('renders as before without the user', () => {
    render(<DataSourceInfo source={garmin()} />);
    expect(screen.queryByText('p01@lab.example.edu')).toBeNull();
    expect(screen.queryByText('Map device')).toBeNull();
  });
});
