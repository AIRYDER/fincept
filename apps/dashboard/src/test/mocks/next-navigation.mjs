/**
 * Test mock for next/navigation.
 *
 * The real next/navigation hooks (useRouter, usePathname, useSearchParams)
 * require Next.js AppRouter context which is not available when rendering
 * with renderToStaticMarkup outside of Next.js.  This mock returns benign
 * stubs so AppShell and its children render without crashing.
 */

export function useRouter() {
  return {
    push: () => undefined,
    replace: () => undefined,
    back: () => undefined,
    forward: () => undefined,
    refresh: () => undefined,
    prefetch: () => undefined,
  };
}

export function usePathname() {
  return "/";
}

export function useSearchParams() {
  return new URLSearchParams();
}
