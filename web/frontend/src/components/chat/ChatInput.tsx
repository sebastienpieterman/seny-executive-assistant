import { useRef, useCallback, useEffect, type KeyboardEvent, type ChangeEvent } from "react";
import { Send, Paperclip, File as FileIcon, X } from "lucide-react";
import { cn } from "@/lib/utils";

interface AttachedFileInfo {
  name: string;
  sizeInfo: string;
  truncated: boolean;
}

interface ChatInputProps {
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  onFileSelect?: (file: File) => void;
  attachedFile?: AttachedFileInfo | null;
  onClearFile?: () => void;
  disabled?: boolean;
  uploading?: boolean;
}

const MAX_ROWS = 6;
const LINE_HEIGHT = 24;

const ACCEPTED_FILE_TYPES =
  ".pdf,.docx,.pptx,.csv,.txt,.md,.html,.htm,.png,.jpg,.jpeg,.webp,.gif";

export function ChatInput({
  value,
  onChange,
  onSend,
  onFileSelect,
  attachedFile,
  onClearFile,
  disabled = false,
  uploading = false,
}: ChatInputProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Stable ref so event listeners always call the latest callback
  const onFileSelectRef = useRef(onFileSelect);
  useEffect(() => { onFileSelectRef.current = onFileSelect; });

  // Core strategy: attach the native "change" listener at mousedown to the
  // exact input element the user is about to click. The closure keeps a
  // hard reference to that element, so even if React re-renders / remounts
  // the component while the picker is open, the listener still fires on the
  // original element when the user selects a file.
  const handleMouseDown = useCallback(() => {
    if (disabled || uploading) return;

    const input = fileInputRef.current;
    if (!input) return;

    let fired = false;

    const handleChange = () => {
      if (fired) return;
      fired = true;
      clearTimeout(cleanup);
      input.removeEventListener("change", handleChange);

      const file = input.files?.[0];
      if (file) {
        try { input.value = ""; } catch (_) { /* ignore */ }
        onFileSelectRef.current?.(file);
      }
    };

    input.addEventListener("change", handleChange);

    const cleanup = window.setTimeout(() => {
      input.removeEventListener("change", handleChange);
    }, 10 * 60 * 1000);
  }, [disabled, uploading]);

  // Secondary: React synthetic onChange (works in normal browser environments)
  const handleFileChange = useCallback((e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) {
      e.target.value = "";
      onFileSelectRef.current?.(file);
    }
  }, []);

  const handleChange = useCallback(
    (e: ChangeEvent<HTMLTextAreaElement>) => {
      onChange(e.target.value);
      const el = e.target;
      el.style.height = "auto";
      el.style.height = `${Math.min(el.scrollHeight, MAX_ROWS * LINE_HEIGHT)}px`;
    },
    [onChange]
  );

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        if (value.trim() && !disabled) {
          onSend();
          if (textareaRef.current) {
            textareaRef.current.style.height = "auto";
          }
        }
      }
    },
    [value, disabled, onSend]
  );

  const canSend = value.trim().length > 0 && !disabled;

  return (
    <div className="flex flex-col rounded-xl border border-border bg-[#1a1a1a] px-3 py-2 focus-within:ring-1 focus-within:ring-[#d4a445]/50">
      {/* Uploading indicator */}
      {uploading && (
        <div className="flex items-center gap-2 rounded-lg bg-[#d4a445]/10 border border-[#d4a445]/30 px-3 py-2 mb-2">
          <div className="h-3 w-3 shrink-0 animate-spin rounded-full border-2 border-[#d4a445] border-t-transparent" />
          <span className="text-xs font-medium text-[#d4a445]">Reading file...</span>
        </div>
      )}

      {/* File attached badge */}
      {!uploading && attachedFile && (
        <div className="flex items-center gap-2 rounded-lg bg-[#d4a445]/10 border border-[#d4a445]/30 px-3 py-2 mb-2">
          <FileIcon className="h-4 w-4 shrink-0 text-[#d4a445]" />
          <div className="flex flex-col min-w-0 flex-1">
            <span className="text-xs font-semibold text-[#d4a445] truncate">
              {attachedFile.name}
            </span>
            <span className="text-xs text-[#d4a445]/70">
              {attachedFile.sizeInfo}
              {attachedFile.truncated ? " · truncated" : " · ready to send"}
            </span>
          </div>
          <button
            onClick={onClearFile}
            className="shrink-0 rounded p-0.5 text-[#d4a445]/60 hover:text-[#d4a445] transition-colors"
            aria-label="Remove attached file"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}

      {/* Input row */}
      <div className="flex items-end gap-2">
        {/* Paperclip: visible icon behind opacity-0 file input overlay.
            The user's actual click lands on the <input> directly — the only
            reliable way to open the picker in restricted environments.
            File detection is via the native listener added in handleMouseDown. */}
        <div
          className="relative h-8 w-8 shrink-0"
          onMouseDown={handleMouseDown}
        >
          <div
            className={cn(
              "flex h-full w-full items-center justify-center rounded-lg transition-colors",
              disabled || uploading ? "opacity-40" : "",
              attachedFile
                ? "text-[#d4a445]"
                : "text-muted-foreground hover:text-foreground"
            )}
          >
            <Paperclip className="h-4 w-4" />
          </div>
          <input
            ref={fileInputRef}
            type="file"
            accept={ACCEPTED_FILE_TYPES}
            onChange={handleFileChange}
            disabled={disabled || uploading}
            className="absolute inset-0 h-full w-full cursor-pointer opacity-0 disabled:cursor-not-allowed"
            aria-label="Attach file"
          />
        </div>

        <textarea
          ref={textareaRef}
          value={value}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          disabled={disabled}
          rows={1}
          placeholder="Message Seny..."
          className="flex-1 resize-none bg-transparent text-sm leading-6 text-foreground placeholder:text-muted-foreground focus:outline-none disabled:opacity-50"
          style={{ maxHeight: `${MAX_ROWS * LINE_HEIGHT}px` }}
        />

        <button
          onClick={() => {
            if (canSend) {
              onSend();
              if (textareaRef.current) {
                textareaRef.current.style.height = "auto";
              }
            }
          }}
          disabled={!canSend}
          className={cn(
            "flex h-8 w-8 shrink-0 items-center justify-center rounded-lg transition-colors",
            canSend
              ? "bg-[#d4a445] text-[#0f0f0f] hover:bg-[#d4a445]/90"
              : "text-muted-foreground opacity-40"
          )}
          aria-label="Send message"
        >
          <Send className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}
