import { useState, useCallback, useEffect } from "react";
import { SquarePen, X } from "lucide-react";
import { toast } from "sonner";
import { api, TOKEN_KEY } from "@/lib/api";
import { ConversationList } from "@/components/chat/ConversationList";
import { ChatMessages } from "@/components/chat/ChatMessages";
import { ChatInput } from "@/components/chat/ChatInput";
import { ModelSelector } from "@/components/chat/ModelSelector";
import type { ChatMessage } from "@/components/chat/MessageBubble";

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
  truncationNotice: string | null;
  needsStoragePrompt: boolean;
}

export function ChatPage() {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [selectedModel, setSelectedModel] = useState<string | null>(null);
  const [listKey, setListKey] = useState(0); // force refresh conversation list

  // File upload state
  const [pendingFile, setPendingFile] = useState<PendingFile | null>(null);
  const [showStorageMenu, setShowStorageMenu] = useState(false);
  const [storageMenuFile, setStorageMenuFile] = useState<PendingFile | null>(null);
  const [uploading, setUploading] = useState(false);

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

  // ---------------------------------------------------------------------------
  // File upload handling
  // ---------------------------------------------------------------------------

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
        truncationNotice: data.truncation_notice,
        needsStoragePrompt: data.needs_storage_prompt,
      };

      setPendingFile(pending);

      if (data.needs_storage_prompt) {
        setStorageMenuFile(pending);
        setShowStorageMenu(true);
      }
    } catch (err) {
      toast.error(`Could not read file: ${err instanceof Error ? err.message : "Unknown error"}`);
      console.error("File upload error:", err);
    } finally {
      setUploading(false);
    }
  }, []);

  const handleStorageChoice = useCallback(
    async (mode: "ephemeral" | "silent" | "note") => {
      setShowStorageMenu(false);

      if (mode === "ephemeral") {
        // Keep pendingFile — it will be injected when user sends their next message
        return;
      }

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

        if (mode === "silent") {
          toast.success(
            `Saved silently. The content is still attached below — send your message to use it now.`
          );
        } else {
          toast.success("Saved as a note. The content is still attached — send your message to use it now.");
        }

        // Keep pendingFile so the content is still injected into the current chat message.
        // The file has been saved to storage AND will be used in the conversation.
        setStorageMenuFile(null);
      } catch (err) {
        toast.error("Could not save file");
        console.error("Save error:", err);
      }
    },
    [storageMenuFile]
  );

  // ---------------------------------------------------------------------------
  // Send message
  // ---------------------------------------------------------------------------

  const handleSend = useCallback(async () => {
    const text = input.trim();
    if (!text || isLoading) return;

    // Build the display message for the UI
    const displayContent = text;

    // Build the actual message content to send to the API
    let messageText = text;
    const extraBody: Record<string, unknown> = {};

    if (pendingFile) {
      if (pendingFile.imageB64) {
        // Image: send as multimodal fields
        extraBody.image_b64 = pendingFile.imageB64;
        extraBody.image_media_type = pendingFile.mediaType ?? "image/jpeg";
        extraBody.image_file_name = pendingFile.name;
      } else if (pendingFile.text) {
        // Text file: prepend content as context prefix
        messageText = `[File: ${pendingFile.name} — ${pendingFile.sizeInfo}]\n\n${pendingFile.text}\n\n---\n${text}`;
      }
    }

    // Optimistic: add user message immediately (show only their typed text)
    const userMsg: ChatMessage = {
      role: "user",
      content: displayContent,
      created_at: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, userMsg]);
    setInput("");
    setIsLoading(true);

    // Clear the pending file right after capturing it
    if (pendingFile) {
      setPendingFile(null);
    }

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

      // If this was a new conversation, update the selected ID
      if (!selectedId && data.conversation_id) {
        setSelectedId(data.conversation_id);
        // Also persist model to the new conversation if user changed it
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

      // Refresh conversation list (new conversation may have appeared)
      setListKey((k) => k + 1);
    } catch (err) {
      // Show error as assistant message
      const errorMsg: ChatMessage = {
        role: "assistant",
        content: `Something went wrong: ${err instanceof Error ? err.message : "Unknown error"}. Please try again.`,
        created_at: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, errorMsg]);
    } finally {
      setIsLoading(false);
    }
  }, [input, isLoading, selectedId, selectedModel, pendingFile]);

  const handleNewChat = useCallback(() => {
    setSelectedId(null);
    setMessages([]);
    setSelectedModel(null);
    setInput("");
    setPendingFile(null);
    setShowStorageMenu(false);
    setStorageMenuFile(null);
    setUploading(false);
  }, []);

  return (
    <div className="flex h-full">
      {/* Sidebar: conversation list */}
      <div className="hidden w-72 shrink-0 border-r border-border p-3 md:block">
        <ConversationList
          key={listKey}
          activeId={selectedId}
          onSelect={(id) => setSelectedId(id)}
        />
      </div>

      {/* Main chat area */}
      <div className="flex flex-1 flex-col">
        {/* Header bar with model selector */}
        <div className="flex items-center justify-between border-b border-border px-4 py-2">
          <div className="flex items-center gap-2">
            <h2 className="text-sm font-medium text-muted-foreground">
              {selectedId ? "Conversation" : "New Chat"}
            </h2>
            <button
              onClick={handleNewChat}
              className="flex items-center gap-1 rounded-md bg-[#d4a445] px-2 py-0.5 text-xs font-semibold text-[#0f0f0f] transition-colors hover:bg-[#d4a445]/90"
              aria-label="New chat"
            >
              <SquarePen className="h-3 w-3" />
              New Chat
            </button>
          </div>
          <ModelSelector
            conversationId={selectedId}
            selectedModel={selectedModel}
            onModelChange={setSelectedModel}
          />
        </div>

        {/* Messages */}
        <ChatMessages messages={messages} isLoading={isLoading} />

        {/* Input area */}
        <div className="relative border-t border-border px-4 py-3">
          {/* Storage menu overlay */}
          {showStorageMenu && storageMenuFile && (
            <div className="absolute bottom-full left-4 right-4 mb-2 rounded-xl border border-border bg-[#1a1a1a] p-4 shadow-lg z-10">
              <div className="flex items-start justify-between mb-3">
                <p className="text-sm text-foreground">
                  I&apos;ve read this {storageMenuFile.type} ({storageMenuFile.sizeInfo}).
                  What would you like to do?
                </p>
                <button
                  onClick={() => setShowStorageMenu(false)}
                  className="ml-2 shrink-0 text-muted-foreground hover:text-foreground transition-colors"
                  aria-label="Close storage menu"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
              <div className="flex flex-col gap-2">
                <button
                  onClick={() => handleStorageChoice("ephemeral")}
                  className="text-left rounded-lg border border-border px-3 py-2 text-sm hover:bg-[#242424] transition-colors"
                >
                  <span className="font-medium">1. Use it now only</span>
                  <span className="ml-1 text-muted-foreground">(no storage)</span>
                </button>
                <button
                  onClick={() => handleStorageChoice("silent")}
                  className="text-left rounded-lg border border-border px-3 py-2 text-sm hover:bg-[#242424] transition-colors"
                >
                  <span className="font-medium">2. Remember it silently</span>
                  <span className="ml-1 text-muted-foreground">
                    (searchable later, won&apos;t appear in Notes)
                  </span>
                </button>
                <button
                  onClick={() => handleStorageChoice("note")}
                  className="text-left rounded-lg border border-border px-3 py-2 text-sm hover:bg-[#242424] transition-colors"
                >
                  <span className="font-medium">3. Save as a note</span>
                  <span className="ml-1 text-muted-foreground">(appears in Notes tab)</span>
                </button>
              </div>
              <p className="mt-3 text-xs text-muted-foreground">
                Or just tell me what you want in plain language.
              </p>
            </div>
          )}

          <ChatInput
            value={input}
            onChange={setInput}
            onSend={handleSend}
            disabled={isLoading}
            uploading={uploading}
            onFileSelect={handleFileSelect}
            attachedFile={
              pendingFile
                ? {
                    name: pendingFile.name,
                    sizeInfo: pendingFile.sizeInfo,
                    truncated: pendingFile.truncated,
                  }
                : null
            }
            onClearFile={() => setPendingFile(null)}
          />
        </div>
      </div>
    </div>
  );
}
