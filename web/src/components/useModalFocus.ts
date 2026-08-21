import {
  type KeyboardEvent,
  type RefObject,
  useLayoutEffect,
  useRef,
} from "react";

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "textarea:not([disabled])",
  "select:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

function focusableElements(container: HTMLElement) {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
}

export function useModalFocus(
  onClose: () => void,
  initialFocusRef: RefObject<HTMLElement | null>,
  busy = false,
  returnFocusFallbackRef?: RefObject<HTMLElement | null>,
) {
  const dialogRef = useRef<HTMLElement>(null);
  const closeRef = useRef(onClose);
  const busyRef = useRef(busy);
  closeRef.current = onClose;
  busyRef.current = busy;

  useLayoutEffect(() => {
    const returnFocus =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const dialog = dialogRef.current;
    const initialFocus = initialFocusRef.current ?? (dialog ? focusableElements(dialog)[0] : null);
    initialFocus?.focus();

    return () => {
      queueMicrotask(() => {
        if (returnFocus?.isConnected) {
          returnFocus.focus();
        } else {
          returnFocusFallbackRef?.current?.focus();
        }
      });
    };
  }, [initialFocusRef, returnFocusFallbackRef]);

  function onKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.key === "Escape" && !busyRef.current) {
      event.preventDefault();
      event.stopPropagation();
      closeRef.current();
      return;
    }
    if (event.key !== "Tab") {
      return;
    }

    const focusable = focusableElements(event.currentTarget);
    if (focusable.length === 0) {
      event.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  return { dialogRef, onKeyDown };
}
