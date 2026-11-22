/** In-app confirmation dialog (never window.confirm/alert). */

export interface ConfirmOptions {
  title: string;
  message?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
}

interface Pending extends ConfirmOptions {
  resolve: (ok: boolean) => void;
}

export const dialogState = $state<{ current: Pending | null }>({ current: null });

export function confirmDialog(opts: ConfirmOptions): Promise<boolean> {
  dialogState.current?.resolve(false);
  return new Promise((resolve) => {
    dialogState.current = { ...opts, resolve };
  });
}

export function closeDialog(ok: boolean): void {
  const c = dialogState.current;
  dialogState.current = null;
  c?.resolve(ok);
}
