import { useState, useEffect } from "react";
import {
  Dialog,
  DialogContent,
  DialogTitle,
} from "@/components/ui/dialog";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";
import {
  Settings,
  Plug,
  Bell,
  Newspaper,
  Zap,
  EyeOff,
  RefreshCw,
  MessageCircle,
  Brain,
  BarChart2,
  Sparkles,
} from "lucide-react";
import { GeneralTab } from "./GeneralTab";
import { IntegrationsTab } from "./IntegrationsTab";
import { NotificationsTab } from "./NotificationsTab";
import { DigestTab } from "./DigestTab";
import { NudgesTab } from "./NudgesTab";
import { ExcludedChannelsTab } from "./ExcludedChannelsTab";
import { ScannerTab } from "./ScannerTab";
import { ChatTab } from "./ChatTab";
import { MemoriesTab } from "./MemoriesTab";
import { UsageTab } from "./UsageTab";
import { LearningTab } from "./LearningTab";

export type SettingsTab = "general" | "integrations" | "notifications" | "digest" | "nudges" | "chat" | "scanner" | "excluded" | "memories" | "usage" | "learning";

interface SettingsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  initialTab?: SettingsTab;
}

type Tab = SettingsTab;

const tabs: { id: Tab; label: string; icon: React.ElementType }[] = [
  { id: "general", label: "General", icon: Settings },
  { id: "integrations", label: "Integrations", icon: Plug },
  { id: "notifications", label: "Notifications", icon: Bell },
  { id: "digest", label: "Digest & Review", icon: Newspaper },
  { id: "nudges", label: "Nudges", icon: Zap },
  { id: "chat", label: "Chat Channels", icon: MessageCircle },
  { id: "scanner", label: "Scanner", icon: RefreshCw },
  { id: "excluded", label: "Excluded Channels", icon: EyeOff },
  { id: "memories", label: "What Seny Knows", icon: Brain },
  { id: "usage", label: "Usage", icon: BarChart2 },
  { id: "learning", label: "What Seny Learned", icon: Sparkles },
];

export function SettingsDialog({ open, onOpenChange, initialTab }: SettingsDialogProps) {
  const [activeTab, setActiveTab] = useState<Tab>(initialTab ?? "general");

  // Sync activeTab when initialTab changes (e.g. opening to Integrations)
  useEffect(() => {
    if (initialTab && open) {
      setActiveTab(initialTab);
    }
  }, [initialTab, open]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent showCloseButton={false} className="flex flex-col max-w-2xl sm:max-w-3xl h-[80vh] max-h-[700px] p-0 gap-0 overflow-hidden">
        <div className="flex flex-1 min-h-0">
          {/* Sidebar tabs */}
          <div className="flex w-48 shrink-0 flex-col border-r border-border bg-[#111111] p-3">
            <div className="flex items-center justify-between px-2 py-3">
              <DialogTitle className="text-base font-semibold">
                Settings
              </DialogTitle>
              <button
                onClick={() => onOpenChange(false)}
                className="rounded-xs opacity-70 transition-opacity hover:opacity-100"
                aria-label="Close settings"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <nav className="flex flex-col gap-0.5">
              {tabs.map((tab) => (
                <button
                  key={tab.id}
                  onClick={() => setActiveTab(tab.id)}
                  className={cn(
                    "flex items-center gap-2 rounded-md px-2 py-1.5 text-sm text-muted-foreground transition-colors",
                    activeTab === tab.id
                      ? "bg-sidebar-accent text-foreground"
                      : "hover:bg-sidebar-accent/50 hover:text-foreground"
                  )}
                >
                  <tab.icon className="h-4 w-4" />
                  {tab.label}
                </button>
              ))}
            </nav>
          </div>

          {/* Content */}
          <div className="flex-1 overflow-y-auto py-6 pl-6 pr-8">
            {activeTab === "general" && <GeneralTab />}
            {activeTab === "integrations" && <IntegrationsTab />}
            {activeTab === "notifications" && <NotificationsTab />}
            {activeTab === "digest" && <DigestTab />}
            {activeTab === "nudges" && <NudgesTab />}
            {activeTab === "chat" && <ChatTab />}
            {activeTab === "scanner" && <ScannerTab />}
            {activeTab === "excluded" && <ExcludedChannelsTab />}
            {activeTab === "memories" && <MemoriesTab />}
            {activeTab === "usage" && <UsageTab />}
            {activeTab === "learning" && <LearningTab />}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
