// One dialog implementation for the whole app: the wallet picker and the "How it works" panel.
// Escape closes it, a click on the backdrop closes it, and the first button inside takes focus.
import { useEffect, useRef, type ReactNode } from 'react';

export function Modal({
  title,
  onClose,
  children,
  testId,
  width,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  testId?: string;
  width?: number;
}) {
  const dialog = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    document.addEventListener('keydown', onKey);
    dialog.current?.querySelector<HTMLElement>('button')?.focus();
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        ref={dialog}
        data-testid={testId}
        style={width ? { maxWidth: width } : undefined}
      >
        <div className="modal-head">
          <h2>{title}</h2>
          <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>
            Close
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}
