import { useState, useCallback, useEffect } from "react";
import { MessageCircle, Maximize2, Minimize2, X, ChevronDown, ChevronUp, PanelRight, SquarePen } from "lucide-react";
import { toast } from "sonner";
import { api, TOKEN_KEY } from "@/lib/api";
import { ConversationList } from "@/components/chat/ConversationList";
import { ChatMessages } from "@/components/chat/ChatMessages";
import { ChatInput } from "@/components/chat/ChatInput";
import { ModelSelector } from "@/components/chat/ModelSelector";
import type { ChatMessage } from "@/components/chat/MessageBubble";
import { cn } from "@/lib/utils";

interface UploadResponse {
  file_name: string;
  file_type: string;
  text: string | null;
  image_b64: string | null;
  media_type: string | null;
  size_info: string;
  truncated: boolean;
  truncation_notice: string | null;
  needs_storage_prompt: boolean;
}

interface PendingFile {
  name: string;
  type: string;
  text: string | null;
  imageB64: string | null;
  mediaType: string | null;
  sizeInfo: string;
  truncated: boolean;
  needsStoragePrompt: boolean;
}

interface ConversationDetail {
  id: string;
  title: string | null;
  model: string | null;
  created_at: string;
  updated_at: string;
  messages: ChatMessage[];
}

interface ChatResponse {
  response: string;
  conversation_id: string;
  tokens_used: number;
  citations: unknown[];
  tools_used: string[];
  capture_info: unknown;
}

type WidgetState = "minimized" | "expanded" | "sidepanel" | "fullscreen";

interface ChatWidgetProps {
  onStateChange?: (state: WidgetState) => void;
}

export type { WidgetState };

export function ChatWidget({ onStateChange }: ChatWidgetProps) {
  const [widgetState, _setWidgetState] = useState<WidgetState>("minimized");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [selectedModel, setSelectedModel] = useState<string | null>(null);
  const [listKey, setListKey] = useState(0);
  const [showConversations, setShowConversations] = useState(false);
  const [pendingFile, setPendingFile] = useState<PendingFile | null>(null);
  const [uploading, setUploading] = useState(false);
  const [showStorageMenu, setShowStorageMenu] = useState(false);
  const [storageMenuFile, setStorageMenuFile] = useState<PendingFile | null>(null);

  const changeState = useCallback((state: WidgetState) => {
    _setWidgetState(state);
    onStateChange?.(state);
  }, [onStateChange]);

  // Load messages when conversation changes
  useEffect(() => {
    if (!selectedId) {
      setMessages([]);
      setSelectedModel(null);
      return;
    }

    api
      .get<ConversationDetail>(`/api/conversations/${selectedId}`)
      .then((data) => {
        setMessages(data.messages);
        setSelectedModel(data.model);
      })
      .catch(() => {
        setMessages([]);
      });
  }, [selectedId]);

  const handleFileSelect = useCallback(async (file: File) => {
    const formData = new FormData();
    formData.append("file", file);
    const token = localStorage.getItem(TOKEN_KEY);
    setUploading(true);
    try {
      const response = await fetch("/api/upload/", {
        method: "POST",
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        body: formData,
      });
      if (response.status === 401) {
        localStorage.removeItem(TOKEN_KEY);
        window.location.href = "/login";
        return;
      }
      if (!response.ok) {
        const text = await response.text();
        throw new Error(text);
      }
      const data: UploadResponse = await response.json();
      const pending: PendingFile = {
        name: data.file_name,
        type: data.file_type,
        text: data.text,
        imageB64: data.image_b64,
        mediaType: data.media_type,
        sizeInfo: data.size_info,
        truncated: data.truncated,
        needsStoragePrompt: data.needs_storage_prompt,
      };
      setPendingFile(pending);
      if (data.needs_storage_prompt) {
        setStorageMenuFile(pending);
        setShowStorageMenu(true);
      }
    } catch (err) {
      toast.error(`Could not read file: ${err instanceof Error ? err.message : "Unknown error"}`);
    } finally {
      setUploading(false);
    }
  }, []);

  const handleStorageChoice = useCallback(async (mode: "ephemeral" | "silent" | "note") => {
    setShowStorageMenu(false);
    if (mode === "ephemeral") return;
    const fileToSave = storageMenuFile;
    if (!fileToSave) return;
    try {
      await api.post("/api/upload/save", {
        text: fileToSave.text ?? "",
        title: fileToSave.name,
        mode: mode === "note" ? "note" : "silent",
        tags: [],
        truncated: fileToSave.truncated,
      });
      toast.success(
        mode === "silent"
          ? "Saved silently. The content is still attached — send your message to use it now."
          : "Saved as a note. The content is still attached — send your message to use it now."
      );
      setStorageMenuFile(null);
    } catch {
      toast.error("Could not save file");
    }
  }, [storageMenuFile]);

  const handleSend = useCallback(async () => {
    const text = input.trim();
    if (!text || isLoading) return;

    let messageText = text;
    const extraBody: Record<string, unknown> = {};
    if (pendingFile) {
      if (pendingFile.imageB64) {
        extraBody.image_b64 = pendingFile.imageB64;
        extraBody.image_media_type = pendingFile.mediaType ?? "image/jpeg";
        extraBody.image_file_name = pendingFile.name;
      } else if (pendingFile.text) {
        messageText = `[File: ${pendingFile.name} — ${pendingFile.sizeInfo}]\n\n${pendingFile.text}\n\n---\n${text}`;
      }
    }

    const userMsg: ChatMessage = {
      role: "user",
      content: text,
      created_at: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, userMsg]);
    setInput("");
    setIsLoading(true);
    if (pendingFile) setPendingFile(null);

    try {
      const body: Record<string, unknown> = {
        message: messageText,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
        ...extraBody,
      };
      if (selectedId) {
        body.conversation_id = selectedId;
      }
      if (selectedModel) {
        body.model = selectedModel;
      }

      const data = await api.post<ChatResponse>("/api/chat", body);

      if (!selectedId && data.conversation_id) {
        setSelectedId(data.conversation_id);
        if (selectedModel) {
          api
            .patch(`/api/conversations/${data.conversation_id}/model`, {
              model: selectedModel,
            })
            .catch(() => {});
        }
      }

      const assistantMsg: ChatMessage = {
        role: "assistant",
        content: data.response,
        created_at: new Date().toISOString(),
        tools_used: data.tools_used,
      };
      setMessages((prev) => [...prev, assistantMsg]);
      setListKey((k) => k + 1);
    } catch (err) {
      const errorMsg: ChatMessage = {
        role: "assistant",
        content: `Something went wrong: ${err instanceof Error ? err.message : "Unknown error"}. Please try again.`,
        created_at: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, errorMsg]);
    } finally {
      setIsLoading(false);
    }
  }, [input, isLoading, selectedId, selectedModel]);

  const handleNewChat = useCallback(() => {
    setSelectedId(null);
    setMessages([]);
    setSelectedModel(null);
    setInput("");
    setShowConversations(false);
    setPendingFile(null);
    setShowStorageMenu(false);
    setStorageMenuFile(null);
    setUploading(false);
  }, []);

  const handleSelectConversation = useCallback((id: string) => {
    setSelectedId(id);
    setShowConversations(false);
  }, []);

  // Minimized: floating button
  if (widgetState === "minimized") {
    return (
      <button
        onClick={() => changeState("expanded")}
        className="fixed bottom-20 right-4 z-50 flex h-14 w-14 items-center justify-center rounded-full bg-[#d4a445] text-[#0f0f0f] shadow-lg transition-all hover:scale-105 hover:bg-[#d4a445]/90 md:bottom-4"
        aria-label="Open chat"
      >
        <MessageCircle className="h-6 w-6" />
      </button>
    );
  }

  const isFullscreen = widgetState === "fullscreen";
  const isSidePanel = widgetState === "sidepanel";

  return (
    <div
      className={cn(
        "fixed z-50 flex flex-col overflow-hidden border border-border bg-[#0f0f0f] shadow-xl transition-all duration-300",
        isFullscreen
          ? "inset-0 rounded-none"
          : isSidePanel
            ? "right-0 top-12 bottom-0 w-[420px] rounded-none border-r-0 border-t-0 border-b-0"
            : "bottom-4 right-4 h-[600px] w-[420px] rounded-2xl"
      )}
    >
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border px-3 py-2">
        <div className="flex items-center gap-2">
          <span className="flex h-6 w-6 items-center justify-center rounded-full bg-[#d4a445] text-xs font-bold text-[#0f0f0f]">
            S
          </span>
          <span className="text-sm font-medium text-foreground">Seny</span>
          <button
            onClick={handleNewChat}
            className="ml-3 flex items-center gap-1 rounded-md bg-[#d4a445] px-2 py-0.5 text-xs font-semibold text-[#0f0f0f] transition-colors hover:bg-[#d4a445]/90"
            aria-label="New chat"
          >
            <SquarePen className="h-3 w-3" />
            New Chat
          </button>
        </div>
        <div className="flex items-center gap-1">
          <ModelSelector
            conversationId={selectedId}
            selectedModel={selectedModel}
            onModelChange={setSelectedModel}
          />
          <button
            onClick={() => changeState(isSidePanel ? "expanded" : "sidepanel")}
            className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-[#1e1e1e] hover:text-foreground"
            aria-label={isSidePanel ? "Floating panel" : "Side panel"}
          >
            <PanelRight className="h-4 w-4" />
          </button>
          <button
            onClick={() => changeState(isFullscreen ? "expanded" : "fullscreen")}
            className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-[#1e1e1e] hover:text-foreground"
            aria-label={isFullscreen ? "Exit full screen" : "Full screen"}
          >
            {isFullscreen ? (
              <Minimize2 className="h-4 w-4" />
            ) : (
              <Maximize2 className="h-4 w-4" />
            )}
          </button>
          <button
            onClick={() => changeState("minimized")}
            className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-[#1e1e1e] hover:text-foreground"
            aria-label="Minimize chat"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      </div>

      {/* Conversation selector (collapsible) */}
      <div className="border-b border-border">
        <button
          onClick={() => setShowConversations((v) => !v)}
          className="flex w-full items-center justify-between px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-[#1a1a1a]"
        >
          <span>{selectedId ? "Switch conversation" : "New Chat"}</span>
          {showConversations ? (
            <ChevronUp className="h-3 w-3" />
          ) : (
            <ChevronDown className="h-3 w-3" />
          )}
        </button>
        {showConversations && (
          <div className={cn("overflow-y-auto border-t border-border px-2 pb-2", (isFullscreen || isSidePanel) ? "max-h-64" : "max-h-48")}>
            <ConversationList
              key={listKey}
              activeId={selectedId}
              onSelect={handleSelectConversation}
            />
          </div>
        )}
      </div>

      {/* Messages */}
      <ChatMessages messages={messages} isLoading={isLoading} />

      {/* Input */}
      <div className="relative border-t border-border px-3 py-2">
        {showStorageMenu && storageMenuFile && (
          <div className="absolute bottom-full left-3 right-3 mb-2 rounded-xl border border-border bg-[#1a1a1a] p-3 shadow-lg z-10">
            <p className="text-xs text-foreground mb-2">
              I&apos;ve read this {storageMenuFile.type} ({storageMenuFile.sizeInfo}). What would you like to do?
            </p>
            <div className="flex flex-col gap-1.5">
              <button onClick={() => handleStorageChoice("ephemeral")} className="text-left rounded-lg border border-border px-2.5 py-1.5 text-xs hover:bg-[#242424] transition-colors">
                <span className="font-medium">Use it now only</span> <span className="text-muted-foreground">(no storage)</span>
              </button>
              <button onClick={() => handleStorageChoice("silent")} className="text-left rounded-lg border border-border px-2.5 py-1.5 text-xs hover:bg-[#242424] transition-colors">
                <span className="font-medium">Remember silently</span> <span className="text-muted-foreground">(searchable later)</span>
              </button>
              <button onClick={() => handleStorageChoice("note")} className="text-left rounded-lg border border-border px-2.5 py-1.5 text-xs hover:bg-[#242424] transition-colors">
                <span className="font-medium">Save as a note</span> <span className="text-muted-foreground">(appears in Notes)</span>
              </button>
            </div>
          </div>
        )}
        <ChatInput
          value={input}
          onChange={setInput}
          onSend={handleSend}
          disabled={isLoading}
          uploading={uploading}
          onFileSelect={handleFileSelect}
          attachedFile={pendingFile ? { name: pendingFile.name, sizeInfo: pendingFile.sizeInfo, truncated: pendingFile.truncated } : null}
          onClearFile={() => setPendingFile(null)}
        />
      </div>
    </div>
  );
}
