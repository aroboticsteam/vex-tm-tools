import type { ReactNode } from 'react';
import { X } from 'lucide-react';

export function Modal({
  open,
  onClose,
  title,
  children,
  wide = false,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  wide?: boolean;
}) {
  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center bg-black/70 p-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={`vmd-card max-h-[85vh] w-full ${wide ? 'max-w-3xl' : 'max-w-md'} overflow-y-auto`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-semibold text-vmd-textStrong">{title}</h2>
          <button onClick={onClose} className="text-vmd-textMuted hover:text-vmd-textStrong">
            <X size={18} />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}
