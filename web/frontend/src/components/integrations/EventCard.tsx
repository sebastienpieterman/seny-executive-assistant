import { cn } from "@/lib/utils";
import { MapPin, Video } from "lucide-react";

export interface AgendaEvent {
  id: string;
  summary: string;
  start: string;
  end: string;
  start_time: string;
  end_time: string;
  is_all_day: boolean;
  location?: string | null;
  has_video: boolean;
  video_link?: string | null;
  account?: string | null;
  calendar_id?: string | null;
  calendar_name?: string | null;
  calendar_color?: string | null;
}

interface EventCardProps {
  event: AgendaEvent;
  onClick?: () => void;
}

export function EventCard({ event, onClick }: EventCardProps) {
  const color = event.calendar_color || "#4285f4";

  return (
    <button
      onClick={onClick}
      className={cn(
        "w-full text-left rounded-lg px-3 py-2.5 transition-colors",
        "hover:bg-sidebar-accent/50",
        event.is_all_day && "bg-sidebar-accent/30",
      )}
      style={{ borderLeft: `3px solid ${color}` }}
    >
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-sm font-medium text-foreground truncate">
          {event.summary}
        </span>
      </div>

      <div className="mt-0.5 flex items-center gap-2 text-xs text-muted-foreground">
        {event.is_all_day ? (
          <span>All day</span>
        ) : (
          <span>
            {event.start_time} &ndash; {event.end_time}
          </span>
        )}
        {event.calendar_name && (
          <>
            <span className="text-muted-foreground/40">&middot;</span>
            <span className="truncate">{event.calendar_name}</span>
          </>
        )}
      </div>

      {event.location && (
        <div className="mt-1 flex items-center gap-1 text-xs text-muted-foreground/70">
          <MapPin className="h-3 w-3 shrink-0" />
          <span className="truncate">{event.location}</span>
        </div>
      )}

      {event.has_video && (
        <div className="mt-1 flex items-center gap-1 text-xs text-primary/80">
          <Video className="h-3 w-3 shrink-0" />
          <span>Video call</span>
        </div>
      )}
    </button>
  );
}
