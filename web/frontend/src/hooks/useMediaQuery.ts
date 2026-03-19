import { useState, useEffect } from "react";

/**
 * Hook that returns true when the given media query matches.
 * Used for responsive behavior that can't be handled by Tailwind alone
 * (e.g., conditional rendering of entire components).
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => {
    if (typeof window !== "undefined") {
      return window.matchMedia(query).matches;
    }
    return false;
  });

  useEffect(() => {
    const mql = window.matchMedia(query);
    const handler = (e: MediaQueryListEvent) => setMatches(e.matches);
    mql.addEventListener("change", handler);
    setMatches(mql.matches);
    return () => mql.removeEventListener("change", handler);
  }, [query]);

  return matches;
}

/** Convenience breakpoint hooks matching Tailwind defaults */
export function useIsMobile() {
  return !useMediaQuery("(min-width: 768px)");
}

export function useIsTablet() {
  const aboveMobile = useMediaQuery("(min-width: 768px)");
  const belowDesktop = !useMediaQuery("(min-width: 1024px)");
  return aboveMobile && belowDesktop;
}

export function useIsDesktop() {
  return useMediaQuery("(min-width: 1024px)");
}
