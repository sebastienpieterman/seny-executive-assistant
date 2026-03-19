import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Checkbox } from "@/components/ui/checkbox";
import { Button } from "@/components/ui/button";
import { Loader2, RefreshCw } from "lucide-react";

interface CalendarPreference {
  id: string;
  name: string;
  is_primary: boolean;
  is_visible: boolean;
  access_role?: string | null;
  color?: string | null;
  account?: string | null;
}

interface CalendarFiltersProps {
  onVisibilityChange: () => void;
}

export function CalendarFilters({ onVisibilityChange }: CalendarFiltersProps) {
  const [calendars, setCalendars] = useState<CalendarPreference[]>([]);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);

  const fetchPreferences = useCallback(async () => {
    try {
      const data = await api.get<{ calendars: CalendarPreference[] }>(
        "/api/calendar/preferences",
      );
      setCalendars(data.calendars);
    } catch (err) {
      console.error("Failed to load calendar preferences:", err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchPreferences();
  }, [fetchPreferences]);

  async function handleToggle(calendarId: string, visible: boolean) {
    // Optimistic update
    setCalendars((prev) =>
      prev.map((c) => (c.id === calendarId ? { ...c, is_visible: visible } : c)),
    );

    try {
      await api.put(
        `/api/calendar/preferences/${encodeURIComponent(calendarId)}/visibility`,
        { visible },
      );
      onVisibilityChange();
    } catch (err) {
      console.error("Failed to toggle visibility:", err);
      // Revert
      setCalendars((prev) =>
        prev.map((c) =>
          c.id === calendarId ? { ...c, is_visible: !visible } : c,
        ),
      );
    }
  }

  async function handleSync() {
    setSyncing(true);
    try {
      await api.post("/api/calendar/preferences/sync");
      await fetchPreferences();
    } catch (err) {
      console.error("Failed to sync calendars:", err);
    } finally {
      setSyncing(false);
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-4">
        <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
      </div>
    );
  }

  // Group by account
  const byAccount: Record<string, CalendarPreference[]> = {};
  for (const cal of calendars) {
    const key = cal.account || "Unknown";
    if (!byAccount[key]) byAccount[key] = [];
    byAccount[key].push(cal);
  }

  return (
    <div className="space-y-3 px-4 py-3">
      {Object.entries(byAccount).map(([account, cals]) => (
        <div key={account}>
          <p className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-muted-foreground/70">
            {account}
          </p>
          <div className="space-y-1">
            {cals.map((cal) => (
              <label
                key={cal.id}
                className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 hover:bg-sidebar-accent/50"
              >
                <Checkbox
                  checked={cal.is_visible}
                  onCheckedChange={(checked) =>
                    handleToggle(cal.id, checked === true)
                  }
                  className="h-4 w-4"
                />
                <span
                  className="h-2.5 w-2.5 shrink-0 rounded-full"
                  style={{ backgroundColor: cal.color || "#4285f4" }}
                />
                <span className="truncate text-sm text-foreground/80">
                  {cal.name}
                </span>
              </label>
            ))}
          </div>
        </div>
      ))}

      <Button
        variant="ghost"
        size="sm"
        className="w-full text-xs"
        onClick={handleSync}
        disabled={syncing}
      >
        <RefreshCw className={`mr-1.5 h-3 w-3 ${syncing ? "animate-spin" : ""}`} />
        Sync from Google
      </Button>
    </div>
  );
}
