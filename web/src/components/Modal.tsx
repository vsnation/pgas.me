// One dialog implementation for the whole app: the wallet picker and the "How it works" panel.
// Escape closes it, a click on the backdrop closes it, and the first button inside takes focus.
import { useEffect, useRef, type ReactNode } from 'react';

export function Modal({
  title,
  onClose,
  children,
  testId,
  width,
  className,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  testId?: string;
  width?: number;
  /** Goes on the BACKDROP, so a dialog that needs its own shape (a full-screen sheet on a phone)
   *  can restyle the panel and the space around it without a second dialog implementation. */
  className?: string;
}) {
  const dialog = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    document.addEventListener('keydown', onKey);
    dialog.current?.querySelector<HTMLElement>('button')?.focus();
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);
  return (
    <div className={`modal-backdrop${className ? ` ${className}` : ''}`} onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
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
