import { useState, useCallback } from "react";
import { Outlet, useLocation } from "react-router-dom";
import { AppHeader } from "./AppHeader";
import { SidebarNav } from "./SidebarNav";
import { MobileNav } from "./MobileNav";
import { SlideOverPanel } from "./SlideOverPanel";
import { ChatWidget } from "@/components/chat/ChatWidget";
import type { WidgetState } from "@/components/chat/ChatWidget";
import { SlackPanel } from "@/components/integrations/SlackPanel";
import { TelegramPanel } from "@/components/integrations/TelegramPanel";
import { SettingsDialog } from "@/components/settings/SettingsDialog";
import type { SettingsTab } from "@/components/settings/SettingsDialog";
import { PrefetchProvider } from "@/contexts/PrefetchContext";
import { OfflineBanner } from "@/components/ui/ErrorState";
import { useOnlineStatus } from "@/hooks/useOnlineStatus";
import { cn } from "@/lib/utils";

type SlideOver = "slack" | "telegram" | null;

export function AppLayout() {
  const [chatState, setChatState] = useState<WidgetState>("minimized");
  const [slideOver, setSlideOver] = useState<SlideOver>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsTab, setSettingsTab] = useState<SettingsTab>("general");
  const isOnline = useOnlineStatus();
  const location = useLocation();

  const openSettings = useCallback((tab?: SettingsTab) => {
    if (tab) setSettingsTab(tab);
    else setSettingsTab("general");
    setSettingsOpen(true);
  }, []);

  function toggleSlideOver(panel: "slack" | "telegram") {
    setSlideOver((prev) => (prev === panel ? null : panel));
  }

  return (
    <PrefetchProvider>
    <div className="flex h-screen flex-col overflow-hidden">
      {!isOnline && <OfflineBanner />}
      <AppHeader
        onOpenSlack={() => toggleSlideOver("slack")}
        onOpenTelegram={() => toggleSlideOver("telegram")}
        onOpenSettings={() => setSettingsOpen(true)}
        activeSlideOver={slideOver}
      />
      <div className="flex flex-1 overflow-hidden">
        <SidebarNav onOpenSettings={() => setSettingsOpen(true)} />
        <main
          className={cn(
            "relative flex-1 overflow-auto transition-all duration-300",
            // Padding: smaller on mobile, larger on desktop
            "p-3 md:p-4 lg:p-6",
            // Add bottom padding on mobile for the MobileNav bar
            "pb-20 md:pb-4",
            chatState === "sidepanel" && "lg:mr-[420px]"
          )}
        >
          <div key={location.pathname} className="page-enter">
            <Outlet context={{ toggleSlideOver, openSettings }} />
          </div>
        </main>
      </div>

      {/* Mobile bottom navigation — only visible below md breakpoint */}
      <MobileNav
        onOpenSlack={() => toggleSlideOver("slack")}
        onOpenTelegram={() => toggleSlideOver("telegram")}
        onOpenSettings={() => setSettingsOpen(true)}
      />

      <ChatWidget onStateChange={setChatState} />

      {/* Slide-over panels for Slack and Telegram */}
      <SlideOverPanel open={slideOver === "slack"} onClose={() => setSlideOver(null)}>
        <SlackPanel />
      </SlideOverPanel>
      <SlideOverPanel open={slideOver === "telegram"} onClose={() => setSlideOver(null)}>
        <TelegramPanel />
      </SlideOverPanel>

      {/* Settings dialog */}
      <SettingsDialog open={settingsOpen} onOpenChange={setSettingsOpen} initialTab={settingsTab} />
    </div>
    </PrefetchProvider>
  );
}
