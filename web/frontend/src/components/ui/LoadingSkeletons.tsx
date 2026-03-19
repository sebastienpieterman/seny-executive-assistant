import { Skeleton } from "@/components/ui/skeleton";

/** Conversation list skeleton — 4 rows with title + timestamp shapes */
export function ConversationListSkeleton() {
  return (
    <div className="space-y-2 p-3">
      {[1, 2, 3, 4].map((i) => (
        <div key={i} className="flex items-center gap-3 rounded-lg px-3 py-2.5" style={{ animationDelay: `${i * 75}ms` }}>
          <div className="flex-1 space-y-2">
            <Skeleton className="h-3.5 w-3/4" />
            <Skeleton className="h-2.5 w-1/3" />
          </div>
          <Skeleton className="h-2.5 w-12 shrink-0" />
        </div>
      ))}
    </div>
  );
}

/** Chat messages skeleton — alternating left/right bubbles */
export function ChatMessagesSkeleton() {
  return (
    <div className="space-y-4 p-4">
      {/* Assistant message */}
      <div className="flex gap-3">
        <Skeleton className="h-7 w-7 shrink-0 rounded-full" />
        <div className="space-y-2">
          <Skeleton className="h-16 w-64 rounded-xl" />
          <Skeleton className="h-2.5 w-16" />
        </div>
      </div>
      {/* User message */}
      <div className="flex justify-end gap-3">
        <div className="space-y-2 flex flex-col items-end">
          <Skeleton className="h-10 w-48 rounded-xl" />
          <Skeleton className="h-2.5 w-12" />
        </div>
        <Skeleton className="h-7 w-7 shrink-0 rounded-full" />
      </div>
      {/* Assistant message */}
      <div className="flex gap-3">
        <Skeleton className="h-7 w-7 shrink-0 rounded-full" />
        <div className="space-y-2">
          <Skeleton className="h-24 w-72 rounded-xl" />
          <Skeleton className="h-2.5 w-20" />
        </div>
      </div>
    </div>
  );
}

/** Email list skeleton — rows with sender + subject shapes */
export function EmailListSkeleton() {
  return (
    <div className="divide-y divide-border">
      {[1, 2, 3, 4, 5].map((i) => (
        <div key={i} className="flex items-start gap-3 px-4 py-3" style={{ animationDelay: `${i * 75}ms` }}>
          <Skeleton className="mt-0.5 h-5 w-5 shrink-0 rounded" />
          <div className="flex-1 space-y-2">
            <div className="flex items-center justify-between">
              <Skeleton className="h-3.5 w-28" />
              <Skeleton className="h-2.5 w-14" />
            </div>
            <Skeleton className="h-3 w-4/5" />
            <Skeleton className="h-2.5 w-3/5" />
          </div>
        </div>
      ))}
    </div>
  );
}

/** Calendar events skeleton — cards grouped by day */
export function CalendarEventsSkeleton() {
  return (
    <div className="space-y-4 p-4">
      {[1, 2].map((day) => (
        <div key={day}>
          <Skeleton className="mb-2 h-3 w-24" />
          <div className="space-y-2">
            {[1, 2, 3].map((i) => (
              <div key={i} className="flex items-center gap-3 rounded-lg border border-border/50 p-3" style={{ animationDelay: `${(day * 3 + i) * 60}ms` }}>
                <Skeleton className="h-3 w-1 rounded-full" />
                <div className="flex-1 space-y-1.5">
                  <Skeleton className="h-3.5 w-3/5" />
                  <Skeleton className="h-2.5 w-2/5" />
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

/** Task list skeleton — rows with checkbox + title */
export function TaskListSkeleton() {
  return (
    <div className="py-1">
      {[1, 2, 3, 4, 5].map((i) => (
        <div key={i} className="flex items-start gap-3 px-4 py-2.5" style={{ animationDelay: `${i * 75}ms` }}>
          <Skeleton className="mt-0.5 h-4 w-4 shrink-0 rounded" />
          <div className="flex-1 space-y-1.5">
            <Skeleton className="h-3.5 w-4/5" />
            <div className="flex gap-2">
              <Skeleton className="h-2.5 w-12" />
              <Skeleton className="h-2.5 w-16" />
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

/** Notes list skeleton — cards with title + preview */
export function NotesListSkeleton() {
  return (
    <div className="space-y-1 p-1.5">
      {[1, 2, 3, 4].map((i) => (
        <div key={i} className="rounded-lg px-3 py-2.5" style={{ animationDelay: `${i * 75}ms` }}>
          <Skeleton className="h-3.5 w-3/4" />
          <Skeleton className="mt-1.5 h-2.5 w-full" />
          <div className="mt-2 flex gap-2">
            <Skeleton className="h-4 w-10 rounded-full" />
            <Skeleton className="h-4 w-14 rounded-full" />
          </div>
        </div>
      ))}
    </div>
  );
}
