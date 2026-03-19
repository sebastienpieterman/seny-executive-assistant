import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  Home,
  Mail,
  Calendar,
  CheckSquare,
  FileText,
  Brain,
  Newspaper,
  Inbox,
  Settings,
  Layers,
  Activity,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { TOKEN_KEY } from "@/lib/api";

interface NavItem {
  icon: React.ElementType;
  label: string;
  path: string;
}

const navItems: NavItem[] = [
  { icon: Home, label: "Home", path: "/" },
  { icon: Mail, label: "Mail", path: "/mail" },
  { icon: Calendar, label: "Calendar", path: "/calendar" },
  { icon: CheckSquare, label: "Tasks", path: "/tasks" },
  { icon: FileText, label: "Notes", path: "/notes" },
  { icon: Brain, label: "Brain", path: "/second-brain" },
  { icon: Layers, label: "Living Context", path: "/lcd" },
  { icon: Newspaper, label: "Digest", path: "/digest" },
  { icon: Inbox, label: "Actions", path: "/actions" },
  { icon: Activity, label: "Health", path: "/monitoring" },
];

interface SidebarNavProps {
  onOpenSettings?: () => void;
}

export function SidebarNav({ onOpenSettings }: SidebarNavProps) {
  const location = useLocation();
  const navigate = useNavigate();
  const [pendingCount, setPendingCount] = useState(0);

  function refreshPendingCount() {
    const token = localStorage.getItem(TOKEN_KEY);
    if (!token) return;
    fetch("/api/pending-actions/count", {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((r) => r.json())
      .then((data) => setPendingCount(data.count ?? 0))
      .catch(() => {}); // silent failure — badge is cosmetic
  }

  useEffect(() => {
    refreshPendingCount();
    window.addEventListener("pending-actions-changed", refreshPendingCount);
    return () => window.removeEventListener("pending-actions-changed", refreshPendingCount);
  }, []);

  function isActive(path: string) {
    if (path === "/") return location.pathname === "/";
    return location.pathname.startsWith(path);
  }

  return (
    <nav
      className={cn(
        // Hidden on mobile (MobileNav takes over), visible from md (tablet) up
        "hidden md:flex",
        // Base layout: column, border, background
        "h-full shrink-0 flex-col items-center border-r border-border bg-[#111111] py-3",
        // Tablet (md): icon rail only — 68px
        "md:w-[68px]",
        // Desktop (lg): wider rail with labels
        "lg:w-[68px]"
      )}
    >
      {/* Main nav */}
      <div className="flex flex-1 flex-col items-center gap-1">
        {navItems.map((item) => {
          const active = isActive(item.path);
          return (
            <button
              key={item.path}
              onClick={() => navigate(item.path)}
              className={cn(
                "flex w-14 min-h-[44px] flex-col items-center gap-0.5 rounded-lg py-2 text-muted-foreground transition-colors",
                active
                  ? "bg-sidebar-accent text-primary"
                  : "hover:bg-sidebar-accent/50 hover:text-foreground"
              )}
              aria-label={item.label}
            >
              <div className="relative">
                <item.icon className="h-5 w-5" />
                {item.path === "/actions" && pendingCount > 0 && (
                  <span className="absolute -top-1 -right-1 flex h-4 w-4 items-center justify-center rounded-full bg-destructive text-[10px] text-white font-bold">
                    {pendingCount > 9 ? "9+" : pendingCount}
                  </span>
                )}
              </div>
              <span className="text-[10px] font-medium leading-tight">
                {item.label}
              </span>
            </button>
          );
        })}
      </div>

      {/* Bottom: settings */}
      <div className="flex flex-col items-center gap-1">
        <button
          onClick={onOpenSettings}
          className="flex w-14 min-h-[44px] flex-col items-center gap-0.5 rounded-lg py-2 text-muted-foreground transition-colors hover:bg-sidebar-accent/50 hover:text-foreground"
          aria-label="Settings"
        >
          <Settings className="h-5 w-5" />
          <span className="text-[10px] font-medium leading-tight">
            Settings
          </span>
        </button>
      </div>
    </nav>
  );
}
