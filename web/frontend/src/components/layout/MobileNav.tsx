import { useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  Home,
  Mail,
  Calendar,
  CheckSquare,
  FileText,
  Brain,
  Layers,
  MoreHorizontal,
  Settings,
  MessageSquare,
  Send,
  Newspaper,
  Activity,
} from "lucide-react";
import { cn } from "@/lib/utils";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";

/** Primary tabs shown in the bottom bar */
const primaryItems = [
  { icon: Home, label: "Home", path: "/" },
  { icon: Calendar, label: "Calendar", path: "/calendar" },
  { icon: CheckSquare, label: "Tasks", path: "/tasks" },
  { icon: FileText, label: "Notes", path: "/notes" },
] as const;

/** Items in the "More" sheet */
const moreItems = [
  { icon: Mail, label: "Mail", path: "/mail" },
  { icon: Brain, label: "Brain", path: "/second-brain" },
  { icon: Layers, label: "Living Context", path: "/lcd" },
  { icon: Newspaper, label: "Digest", path: "/digest" },
  { icon: Activity, label: "Health", path: "/monitoring" },
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
              <item.icon className="h-5 w-5" />
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
          <MoreHorizontal className="h-5 w-5" />
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
                  <item.icon className="h-5 w-5" />
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
              <MessageSquare className="h-5 w-5" />
              Slack
            </button>
            <button
              onClick={() => {
                onOpenTelegram?.();
                setMoreOpen(false);
              }}
              className="flex min-h-[44px] items-center gap-3 rounded-lg px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-sidebar-accent/50 hover:text-foreground"
            >
              <Send className="h-5 w-5" />
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
              <Settings className="h-5 w-5" />
              Settings
            </button>
          </div>
        </SheetContent>
      </Sheet>
    </>
  );
}
