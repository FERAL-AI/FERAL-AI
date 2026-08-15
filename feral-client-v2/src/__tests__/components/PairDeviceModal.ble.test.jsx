/**
 * The Bluetooth tab must not claim to have paired anything.
 *
 * What it used to do: `navigator.bluetooth.requestDevice()` opens the
 * browser's device chooser and returns a handle scoped to this tab.
 * The tab then fired `onPaired({source:'ble', ...})`, which is
 * `Devices.jsx:245 handlePaired`: it closes the modal and refreshes the
 * paired list. So the user completed a gesture that says "paired",
 * watched the modal close, and got back a list that did not contain
 * their device, because nothing had been paired: no `gatt.connect()`,
 * no request to the brain, nothing persisted. The caveat line that
 * explained the limitation was unmounted by the same callback that
 * rendered it.
 *
 * The brain has no endpoint that could accept a browser-held BLE
 * handle (see the BLETab docstring for the trace through
 * api/routes/devices.py and api/server.py), so the fix is to stop
 * claiming. These tests pin that:
 *
 *   - `onPaired` is not called, and the modal does not close.
 *   - The tab and its result panel describe a radio check, not a pair.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import PairDeviceModal from '../../components/PairDeviceModal';

function setBluetooth(value) {
  if (value === undefined) {
    delete navigator.bluetooth;
    return;
  }
  Object.defineProperty(navigator, 'bluetooth', {
    value,
    configurable: true,
    writable: true,
  });
}

function renderModal(props = {}) {
  const onPaired = vi.fn();
  const onClose = vi.fn();
  const onTokenIssued = vi.fn();
  const utils = render(
    <MemoryRouter>
      <PairDeviceModal
        open
        onClose={onClose}
        onPaired={onPaired}
        onTokenIssued={onTokenIssued}
        {...props}
      />
    </MemoryRouter>,
  );
  return { ...utils, onPaired, onClose, onTokenIssued };
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({
    ok: true,
    status: 200,
    statusText: 'OK',
    json: () => Promise.resolve({ devices: [] }),
    text: () => Promise.resolve('{}'),
    headers: new Map(),
  })));
});

afterEach(() => {
  cleanup();
  setBluetooth(undefined);
  vi.unstubAllGlobals();
});

describe('PairDeviceModal: Bluetooth tab', () => {
  it('does not report a pair after the browser chooser returns a device', async () => {
    const requestDevice = vi.fn(async () => ({ id: 'ble-1', name: 'Polar H10' }));
    setBluetooth({ requestDevice });

    const { getByRole, onPaired, onClose } = renderModal();

    fireEvent.click(getByRole('tab', { name: /Bluetooth/i }));
    fireEvent.click(getByRole('button', { name: /BLE/i }));

    await waitFor(() => expect(requestDevice).toHaveBeenCalledTimes(1));
    // Let any promise chain the click started settle before asserting
    // the absence of a call.
    await Promise.resolve();
    await Promise.resolve();

    expect(onPaired).not.toHaveBeenCalled();
    // And the modal stays put, so whatever it says about the result is
    // actually readable instead of being unmounted a tick later.
    expect(onClose).not.toHaveBeenCalled();
  });

  it('describes the result as unregistered rather than paired', async () => {
    setBluetooth({ requestDevice: vi.fn(async () => ({ id: 'ble-1', name: 'Polar H10' })) });

    const { getByRole, getByTestId, findByTestId } = renderModal();
    fireEvent.click(getByRole('tab', { name: /Bluetooth/i }));
    fireEvent.click(getByRole('button', { name: /BLE/i }));

    const result = await findByTestId('pair-ble-result');
    expect(result.textContent).toMatch(/Polar H10/);
    expect(result.textContent).toMatch(/not registered with the brain/i);
    expect(result.textContent).toMatch(/not in your device list/i);
    // No form of "paired" anywhere in this tab.
    expect(getByTestId('pair-ble').textContent).not.toMatch(/paired/i);
  });

  it('labels the tab as a check, not as a pairing flow', () => {
    setBluetooth({ requestDevice: vi.fn() });
    const { getByRole } = renderModal();
    const tab = getByRole('tab', { name: /Bluetooth/i });
    expect(tab.textContent).toMatch(/check/i);
  });

  it('says up front that the scan does not add the device to FERAL', () => {
    setBluetooth({ requestDevice: vi.fn() });
    const { getByTestId } = renderModal();
    expect(getByTestId('pair-ble').textContent)
      .toMatch(/does not add the peripheral to FERAL/i);
  });

  it('reports nothing at all when the user cancels the chooser', async () => {
    const err = new Error('User cancelled');
    err.name = 'NotFoundError';
    const requestDevice = vi.fn(async () => { throw err; });
    setBluetooth({ requestDevice });

    const { getByRole, queryByTestId, onPaired } = renderModal();
    fireEvent.click(getByRole('tab', { name: /Bluetooth/i }));
    fireEvent.click(getByRole('button', { name: /BLE/i }));

    await waitFor(() => expect(requestDevice).toHaveBeenCalled());
    await Promise.resolve();

    expect(onPaired).not.toHaveBeenCalled();
    expect(queryByTestId('pair-ble-result')).toBeNull();
  });

  it('still explains itself when the browser has no Web Bluetooth', () => {
    setBluetooth(undefined);
    const { getByTestId } = renderModal();
    expect(getByTestId('pair-ble-unsupported').textContent)
      .toMatch(/Web Bluetooth isn't available/i);
  });
});
