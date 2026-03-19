import { useState } from "react";
import { Bell, MessageSquare, Send, Key, Settings, LogOut } from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";
import { cn } from "@/lib/utils";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { DesktopTokenDialog } from "@/components/settings/DesktopTokenDialog";

interface AppHeaderProps {
  onOpenSlack?: () => void;
  onOpenTelegram?: () => void;
  onOpenSettings?: () => void;
  activeSlideOver?: "slack" | "telegram" | null;
}

export function AppHeader({
  onOpenSlack,
  onOpenTelegram,
  onOpenSettings,
  activeSlideOver,
}: AppHeaderProps) {
  const { logout } = useAuth();
  const [tokenDialogOpen, setTokenDialogOpen] = useState(false);

  return (
    <>
      <header className="flex h-12 shrink-0 items-center justify-between border-b border-border bg-[#111111] px-4">
        {/* Left: logo */}
        <span className="text-base font-semibold tracking-wide text-primary">
          Seny
        </span>

        {/* Right: integrations + bell + avatar */}
        <div className="flex items-center gap-2">
          <button
            onClick={onOpenSlack}
            className={cn(
              "rounded-md p-1.5 text-muted-foreground hover:text-foreground transition-colors",
              activeSlideOver === "slack" && "text-primary bg-sidebar-accent"
            )}
            aria-label="Slack"
            title="Slack"
          >
            <MessageSquare className="h-4 w-4" />
          </button>

          <button
            onClick={onOpenTelegram}
            className={cn(
              "rounded-md p-1.5 text-muted-foreground hover:text-foreground transition-colors",
              activeSlideOver === "telegram" && "text-primary bg-sidebar-accent"
            )}
            aria-label="Telegram"
            title="Telegram"
          >
            <Send className="h-4 w-4" />
          </button>

          <div className="mx-1 h-4 w-px bg-border" />

          <button
            className="rounded-md p-1.5 text-muted-foreground hover:text-foreground transition-colors"
            aria-label="Notifications"
          >
            <Bell className="h-4 w-4" />
          </button>

          {/* User menu */}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                className="flex h-7 w-7 items-center justify-center rounded-full bg-primary text-xs font-bold text-primary-foreground"
                aria-label="User menu"
              >
                S
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-48">
              <DropdownMenuItem onClick={onOpenSettings}>
                <Settings className="mr-2 h-4 w-4" />
                Settings
              </DropdownMenuItem>
              <DropdownMenuItem onClick={() => setTokenDialogOpen(true)}>
                <Key className="mr-2 h-4 w-4" />
                Desktop Token
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem onClick={logout}>
                <LogOut className="mr-2 h-4 w-4" />
                Log out
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </header>

      <DesktopTokenDialog
        open={tokenDialogOpen}
        onOpenChange={setTokenDialogOpen}
      />
    </>
  );
}
