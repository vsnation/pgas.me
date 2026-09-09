// Light / dark, in one place.
//
// Three states, two of them stored: no stored choice means "follow the OS" and the OS is re-read
// whenever it changes; an explicit choice is remembered in localStorage and wins until it is
// changed again. Either way the RESOLVED theme is written to `data-theme` on <html> — the styles
// key off that attribute, and off `prefers-color-scheme` only for the moment before this runs.
export type ThemeChoice = 'light' | 'dark';

const KEY = 'pgas.theme.v1';

export function systemTheme(): ThemeChoice {
  try {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  } catch {
    return 'light';
  }
}

/** The user's explicit choice, or null while they are following the OS. */
export function storedTheme(): ThemeChoice | null {
  try {
    const v = localStorage.getItem(KEY);
    return v === 'light' || v === 'dark' ? v : null;
  } catch {
    return null; // storage unavailable: this session follows the OS
  }
}

export function applyTheme(t: ThemeChoice): void {
  document.documentElement.setAttribute('data-theme', t);
}

/** The theme in force right now: the stored choice when there is one, else the OS. */
export function currentTheme(): ThemeChoice {
  return storedTheme() ?? systemTheme();
}

/** Called once before the first render so the page never paints in the wrong theme. */
export function initTheme(): ThemeChoice {
  const t = currentTheme();
  applyTheme(t);
  return t;
}

export function setTheme(t: ThemeChoice): void {
  try {
    localStorage.setItem(KEY, t);
  } catch {
    // storage unavailable: the choice lives only for this page
  }
  applyTheme(t);
}

/** Fires on an OS change; the caller ignores it once the user has chosen for themselves. */
export function onSystemThemeChange(cb: (t: ThemeChoice) => void): () => void {
  let mq: MediaQueryList;
  try {
    mq = window.matchMedia('(prefers-color-scheme: dark)');
  } catch {
    return () => undefined;
  }
  const onChange = (e: MediaQueryListEvent) => cb(e.matches ? 'dark' : 'light');
  mq.addEventListener('change', onChange);
  return () => mq.removeEventListener('change', onChange);
}
