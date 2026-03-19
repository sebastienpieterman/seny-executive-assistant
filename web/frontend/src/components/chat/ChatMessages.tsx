import { useEffect, useRef, useState, useCallback } from "react";
import { MessageCircle } from "lucide-react";
import { MessageBubble, type ChatMessage } from "./MessageBubble";

interface ChatMessagesProps {
  messages: ChatMessage[];
  isLoading?: boolean;
}

export function ChatMessages({ messages, isLoading = false }: ChatMessagesProps) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);

  // Track whether user has scrolled up manually
  const handleScroll = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    setAutoScroll(atBottom);
  }, []);

  // Auto-scroll on new messages
  useEffect(() => {
    if (autoScroll && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [messages, isLoading, autoScroll]);

  if (messages.length === 0 && !isLoading) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 text-muted-foreground">
        <MessageCircle className="h-10 w-10 opacity-40" />
        <p className="text-sm">Start a conversation with Seny</p>
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      onScroll={handleScroll}
      className="flex-1 space-y-4 overflow-y-auto px-4 py-4"
    >
      {messages.map((msg, i) => (
        <MessageBubble
          key={`${msg.created_at}-${i}`}
          message={msg}
          animate={i === messages.length - 1}
        />
      ))}

      {isLoading && (
        <div className="flex items-start gap-3">
          <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-[#1e1e1e]">
            <span className="text-xs text-[#d4a445]">S</span>
          </div>
          <div className="rounded-xl bg-[#1a1a1a] px-4 py-3">
            <div className="flex gap-1.5">
              <span className="h-2 w-2 animate-bounce rounded-full bg-muted-foreground [animation-delay:0ms]" />
              <span className="h-2 w-2 animate-bounce rounded-full bg-muted-foreground [animation-delay:150ms]" />
              <span className="h-2 w-2 animate-bounce rounded-full bg-muted-foreground [animation-delay:300ms]" />
            </div>
          </div>
        </div>
      )}

      <div ref={bottomRef} />
    </div>
  );
}
