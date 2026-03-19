import { useState, useCallback, type ComponentPropsWithoutRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { Check, Copy } from "lucide-react";
import { cn } from "@/lib/utils";

interface MarkdownRendererProps {
  content: string;
  className?: string;
}

/** Copy-to-clipboard button for code blocks. */
function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }, [text]);

  return (
    <button
      onClick={handleCopy}
      className="absolute right-2 top-2 rounded p-1 text-muted-foreground opacity-0 transition-opacity hover:text-foreground group-hover/code:opacity-100"
      aria-label="Copy code"
    >
      {copied ? (
        <Check className="h-3.5 w-3.5 text-green-400" />
      ) : (
        <Copy className="h-3.5 w-3.5" />
      )}
    </button>
  );
}

export function MarkdownRenderer({ content, className }: MarkdownRendererProps) {
  return (
    <div className={cn("prose-seny", className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={{
          // Code blocks with copy button
          pre({ children, ...props }: ComponentPropsWithoutRef<"pre">) {
            // Extract text content from children for copy button
            const codeText =
              typeof children === "object" && children !== null
                ? extractText(children)
                : String(children ?? "");

            return (
              <div className="group/code relative">
                <pre
                  {...props}
                  className="overflow-x-auto rounded-lg bg-[#1a1a1a] p-4 text-sm leading-relaxed"
                >
                  {children}
                </pre>
                <CopyButton text={codeText} />
              </div>
            );
          },

          // Inline code
          code({ className: codeClassName, children, ...props }: ComponentPropsWithoutRef<"code">) {
            // If it has a language class it's inside a <pre>, don't add inline styles
            const isBlock = codeClassName?.startsWith("hljs") || codeClassName?.startsWith("language-");
            if (isBlock) {
              return (
                <code className={codeClassName} {...props}>
                  {children}
                </code>
              );
            }
            return (
              <code
                className="rounded bg-[#1e1e1e] px-1.5 py-0.5 text-sm text-[#d4a445]"
                {...props}
              >
                {children}
              </code>
            );
          },

          // Links open in new tab
          a({ children, ...props }: ComponentPropsWithoutRef<"a">) {
            return (
              <a
                {...props}
                target="_blank"
                rel="noopener noreferrer"
                className="text-[#d4a445] underline decoration-[#d4a445]/30 underline-offset-2 transition-colors hover:decoration-[#d4a445]"
              >
                {children}
              </a>
            );
          },

          // Tables
          table({ children, ...props }: ComponentPropsWithoutRef<"table">) {
            return (
              <div className="overflow-x-auto">
                <table
                  {...props}
                  className="w-full border-collapse text-sm"
                >
                  {children}
                </table>
              </div>
            );
          },
          th({ children, ...props }: ComponentPropsWithoutRef<"th">) {
            return (
              <th
                {...props}
                className="border border-border px-3 py-2 text-left font-semibold"
              >
                {children}
              </th>
            );
          },
          td({ children, ...props }: ComponentPropsWithoutRef<"td">) {
            return (
              <td {...props} className="border border-border px-3 py-2">
                {children}
              </td>
            );
          },

          // Lists
          ul({ children, ...props }: ComponentPropsWithoutRef<"ul">) {
            return (
              <ul {...props} className="list-disc space-y-1 pl-6">
                {children}
              </ul>
            );
          },
          ol({ children, ...props }: ComponentPropsWithoutRef<"ol">) {
            return (
              <ol {...props} className="list-decimal space-y-1 pl-6">
                {children}
              </ol>
            );
          },

          // Headings
          h1({ children, ...props }: ComponentPropsWithoutRef<"h1">) {
            return (
              <h1 {...props} className="mb-2 mt-4 text-xl font-bold">
                {children}
              </h1>
            );
          },
          h2({ children, ...props }: ComponentPropsWithoutRef<"h2">) {
            return (
              <h2 {...props} className="mb-2 mt-3 text-lg font-semibold">
                {children}
              </h2>
            );
          },
          h3({ children, ...props }: ComponentPropsWithoutRef<"h3">) {
            return (
              <h3 {...props} className="mb-1 mt-2 text-base font-semibold">
                {children}
              </h3>
            );
          },

          // Paragraphs
          p({ children, ...props }: ComponentPropsWithoutRef<"p">) {
            return (
              <p {...props} className="mb-2 last:mb-0">
                {children}
              </p>
            );
          },

          // Blockquotes
          blockquote({ children, ...props }: ComponentPropsWithoutRef<"blockquote">) {
            return (
              <blockquote
                {...props}
                className="border-l-2 border-[#d4a445]/50 pl-4 italic text-muted-foreground"
              >
                {children}
              </blockquote>
            );
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

/** Recursively extract text from React children (for copy button). */
function extractText(node: unknown): string {
  if (typeof node === "string") return node;
  if (typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(extractText).join("");
  if (node && typeof node === "object" && "props" in (node as Record<string, unknown>)) {
    const props = (node as { props?: { children?: unknown } }).props;
    return extractText(props?.children);
  }
  return "";
}
