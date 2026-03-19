import { useState } from "react";
import { Calendar, ChevronDown, ChevronUp, RefreshCw } from "lucide-react";
import { CalendarEventsSkeleton } from "@/components/ui/LoadingSkeletons";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { EventCard } from "./EventCard";
import { CalendarFilters } from "./CalendarFilters";
import { usePrefetch } from "@/contexts/PrefetchContext";

export function CalendarPanel() {
  const {
    calendarDays: days,
    calendarLoading: loading,
    calendarNotConnected: notConnected,
    refreshCalendar,
  } = usePrefetch();

  const [refreshing, setRefreshing] = useState(false);
  const [filtersOpen, setFiltersOpen] = useState(false);

  const handleRefresh = async () => {
    setRefreshing(true);
    await refreshCalendar();
    setRefreshing(false);
  };

  // Not connected
  if (notConnected) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-4 p-6 text-center">
        <Calendar className="h-12 w-12 text-muted-foreground/50" />
        <div>
          <h3 className="text-base font-medium text-foreground">
            Connect Calendar
          </h3>
          <p className="mt-1 text-sm text-muted-foreground">
            Link your Google or Outlook calendar to see your schedule here.
          </p>
        </div>
        <Button
          onClick={() => {
            window.location.href = "/api/email/connect";
          }}
        >
          Connect Calendar
        </Button>
      </div>
    );
  }

  // Loading
  if (loading) {
    return (
      <div className="flex h-full flex-col">
        <div className="border-b border-border px-4 py-3">
          <h2 className="text-base font-semibold">Calendar</h2>
        </div>
        <CalendarEventsSkeleton />
      </div>
    );
  }

  const todayStr = new Date().toLocaleDateString("en-US", {
    weekday: "long",
    month: "long",
    day: "numeric",
  });

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="border-b border-border px-4 py-3">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-base font-semibold">Calendar</h2>
            <p className="text-xs text-muted-foreground">{todayStr}</p>
          </div>
          <div className="flex items-center gap-1">
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8"
              onClick={() => setFiltersOpen(!filtersOpen)}
              title="Calendar filters"
            >
              {filtersOpen ? (
                <ChevronUp className="h-4 w-4" />
              ) : (
                <ChevronDown className="h-4 w-4" />
              )}
            </Button>
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8"
              onClick={handleRefresh}
              disabled={refreshing}
            >
              <RefreshCw
                className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`}
              />
            </Button>
          </div>
        </div>

        {/* Collapsible filters */}
        {filtersOpen && (
          <div className="mt-2 rounded-lg border border-border/50 bg-sidebar-accent/20">
            <CalendarFilters onVisibilityChange={handleRefresh} />
          </div>
        )}
      </div>

      {/* Agenda */}
      <ScrollArea className="flex-1">
        {days.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
            <Calendar className="mb-2 h-8 w-8 opacity-50" />
            <p className="text-sm">No upcoming events</p>
          </div>
        ) : (
          <div className="py-2">
            {days.map((day) => (
              <div key={day.date} className="mb-4">
                <div className="sticky top-0 z-10 bg-background px-4 py-1.5">
                  <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                    {day.label}
                  </h3>
                </div>
                <div className="space-y-1 px-2">
                  {day.events.map((event) => (
                    <EventCard
                      key={`${event.id}-${event.calendar_id}`}
                      event={event}
                    />
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </ScrollArea>
    </div>
  );
}
