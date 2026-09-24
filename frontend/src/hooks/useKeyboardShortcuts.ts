import { useEffect } from 'react';

/**
 * Keyboard shortcuts for tab navigation and help.
 * @param onTabChange - callback to switch tabs (1–4)
 * @param onHelp - callback to toggle help overlay
 * @param enabled - when false, shortcuts are not registered (e.g. full-screen schedule editor)
 */
export function useKeyboardShortcuts(
  onTabChange: (tab: string) => void,
  onHelp: () => void,
  enabled = true,
) {
  useEffect(() => {
    if (!enabled) return;

    const handler = (e: KeyboardEvent) => {
      // Ignore when typing in an input, textarea, or select
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;

      switch (e.key) {
        case '1':
          onTabChange('upload');
          break;
        case '2':
          onTabChange('deployments');
          break;
        case '3':
          onTabChange('qa');
          break;
        case '4':
          onTabChange('students');
          break;
        case '?':
          onHelp();
          break;
      }
    };

    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [onTabChange, onHelp, enabled]);
}
