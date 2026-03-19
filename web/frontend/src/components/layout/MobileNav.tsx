import { useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { cn } from "@/lib/utils";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";

/** Primary tabs shown in the bottom bar */
const primaryItems = [
  { emoji: "🏠", label: "Home", path: "/" },
  { emoji: "📅", label: "Calendar", path: "/calendar" },
  { emoji: "✅", label: "Tasks", path: "/tasks" },
  { emoji: "📝", label: "Notes", path: "/notes" },
] as const;

/** Items in the "More" sheet */
const moreItems = [
  { emoji: "📧", label: "Mail", path: "/mail" },
  { emoji: "🧠", label: "Brain", path: "/second-brain" },
  { emoji: "📊", label: "Living Context", path: "/lcd" },
  { emoji: "📋", label: "Digest", path: "/digest" },
  { emoji: "💓", label: "Health", path: "/monitoring" },
] as const;

interface MobileNavProps {
  onOpenSlack?: () => void;
  onOpenTelegram?: () => void;
  onOpenSettings?: () => void;
}

export function MobileNav({
  onOpenSlack,
  onOpenTelegram,
  onOpenSettings,
}: MobileNavProps) {
  const location = useLocation();
  const navigate = useNavigate();
  const [moreOpen, setMoreOpen] = useState(false);

  function isActive(path: string) {
    if (path === "/") return location.pathname === "/";
    return location.pathname.startsWith(path);
  }

  // Check if any "more" item is active — if so, highlight the More button
  const moreActive = moreItems.some((item) => isActive(item.path));

  return (
    <>
      {/* Bottom navigation bar */}
      <nav className="fixed inset-x-0 bottom-0 z-40 flex h-16 items-center justify-around border-t border-border bg-[#111111] md:hidden">
        {primaryItems.map((item) => {
          const active = isActive(item.path);
          return (
            <button
              key={item.path}
              onClick={() => navigate(item.path)}
              className={cn(
                "flex min-h-[44px] min-w-[44px] flex-col items-center justify-center gap-0.5 rounded-lg px-3 py-1 text-muted-foreground transition-colors",
                active
                  ? "text-primary"
                  : "hover:text-foreground"
              )}
              aria-label={item.label}
            >
              <span className="text-xl leading-none">{item.emoji}</span>
              <span className="text-[10px] font-medium leading-tight">
                {item.label}
              </span>
            </button>
          );
        })}

        {/* More button */}
        <button
          onClick={() => setMoreOpen(true)}
          className={cn(
            "flex min-h-[44px] min-w-[44px] flex-col items-center justify-center gap-0.5 rounded-lg px-3 py-1 text-muted-foreground transition-colors",
            moreActive
              ? "text-primary"
              : "hover:text-foreground"
          )}
          aria-label="More"
        >
          <span className="text-xl leading-none">•••</span>
          <span className="text-[10px] font-medium leading-tight">More</span>
        </button>
      </nav>

      {/* "More" sheet */}
      <Sheet open={moreOpen} onOpenChange={setMoreOpen}>
        <SheetContent side="bottom" className="rounded-t-2xl pb-8">
          <SheetHeader>
            <SheetTitle>More</SheetTitle>
          </SheetHeader>
          <div className="flex flex-col gap-1 px-2">
            {/* Page nav items */}
            {moreItems.map((item) => {
              const active = isActive(item.path);
              return (
                <button
                  key={item.path}
                  onClick={() => {
                    navigate(item.path);
                    setMoreOpen(false);
                  }}
                  className={cn(
                    "flex min-h-[44px] items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors",
                    active
                      ? "bg-sidebar-accent text-primary"
                      : "text-muted-foreground hover:bg-sidebar-accent/50 hover:text-foreground"
                  )}
                >
                  <span className="text-xl leading-none">{item.emoji}</span>
                  {item.label}
                </button>
              );
            })}

            {/* Divider */}
            <div className="my-2 h-px bg-border" />

            {/* Integration shortcuts */}
            <button
              onClick={() => {
                onOpenSlack?.();
                setMoreOpen(false);
              }}
              className="flex min-h-[44px] items-center gap-3 rounded-lg px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-sidebar-accent/50 hover:text-foreground"
            >
              <span className="text-xl leading-none">💼</span>
              Slack
            </button>
            <button
              onClick={() => {
                onOpenTelegram?.();
                setMoreOpen(false);
              }}
              className="flex min-h-[44px] items-center gap-3 rounded-lg px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-sidebar-accent/50 hover:text-foreground"
            >
              <span className="text-xl leading-none">✈️</span>
              Telegram
            </button>

            {/* Divider */}
            <div className="my-2 h-px bg-border" />

            {/* Settings */}
            <button
              onClick={() => {
                onOpenSettings?.();
                setMoreOpen(false);
              }}
              className="flex min-h-[44px] items-center gap-3 rounded-lg px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-sidebar-accent/50 hover:text-foreground"
            >
              <span className="text-xl leading-none">⚙️</span>
              Settings
            </button>
          </div>
        </SheetContent>
      </Sheet>
    </>
  );
}
