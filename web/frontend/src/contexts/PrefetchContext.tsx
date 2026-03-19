/**
 * Prefetch context — fires off mail & calendar API calls on mount
 * so data is ready instantly when the user navigates to those pages.
 */

import {
  createContext,
  useContext,
  useEffect,
  useState,
  useCallback,
  type ReactNode,
} from "react";
import { api } from "@/lib/api";
import type { EmailSummary } from "@/components/integrations/EmailItem";
import type { AgendaEvent } from "@/components/integrations/EventCard";

// ── Types ─────────────────────────────────────────────────────────

export interface MailAccount {
  email: string;
  created_at: string;
  provider: "gmail" | "outlook";
}

interface AgendaDay {
  date: string;
  label: string;
  events: AgendaEvent[];
}

// ── Context value ─────────────────────────────────────────────────

interface PrefetchData {
  // Mail
  mailAccounts: MailAccount[];
  mailEmails: EmailSummary[];
  mailLoading: boolean;
  mailNotConnected: boolean;
  mailReady: boolean;
  refreshMail: (silent?: boolean) => Promise<void>;
  setMailEmails: React.Dispatch<React.SetStateAction<EmailSummary[]>>;

  // Calendar
  calendarDays: AgendaDay[];
  calendarLoading: boolean;
  calendarNotConnected: boolean;
  calendarReady: boolean;
  refreshCalendar: (silent?: boolean) => Promise<void>;
}

const PrefetchContext = createContext<PrefetchData | null>(null);

// ── Provider ──────────────────────────────────────────────────────

export function PrefetchProvider({ children }: { children: ReactNode }) {
  // Mail state
  const [mailAccounts, setMailAccounts] = useState<MailAccount[]>([]);
  const [mailEmails, setMailEmails] = useState<EmailSummary[]>([]);
  const [mailLoading, setMailLoading] = useState(true);
  const [mailNotConnected, setMailNotConnected] = useState(false);
  const [mailReady, setMailReady] = useState(false);

  // Calendar state
  const [calendarDays, setCalendarDays] = useState<AgendaDay[]>([]);
  const [calendarLoading, setCalendarLoading] = useState(true);
  const [calendarNotConnected, setCalendarNotConnected] = useState(false);
  const [calendarReady, setCalendarReady] = useState(false);

  // ── Mail fetch ────────────────────────────────────────────────

  const fetchMail = useCallback(async (silent = false) => {
    if (!silent) setMailLoading(true);
    try {
      // Fetch Gmail and Microsoft accounts in parallel
      const [gmailAcctData, msAcctData] = await Promise.all([
        api.get<{ accounts: { email: string; created_at: string }[] }>("/api/email/accounts").catch(() => ({ accounts: [] })),
        api.get<{ accounts: { email: string; account_type: string; created_at: string }[] }>("/api/microsoft/accounts").catch(() => ({ accounts: [] })),
      ]);

      // Merge accounts with provider tag
      const allAccounts: MailAccount[] = [
        ...gmailAcctData.accounts.map((a) => ({ ...a, provider: "gmail" as const })),
        ...msAcctData.accounts.map((a) => ({ ...a, provider: "outlook" as const })),
      ];
      setMailAccounts(allAccounts);

      if (allAccounts.length === 0) {
        setMailNotConnected(true);
        if (!silent) setMailLoading(false);
        setMailReady(true);
        return;
      }

      // Fetch inboxes from both providers in parallel
      const inboxPromises: Promise<{ emails: EmailSummary[] }>[] = [];
      if (gmailAcctData.accounts.length > 0) {
        inboxPromises.push(
          api.get<{ emails: EmailSummary[] }>("/api/email/inbox/all?max_results=20").catch(() => ({ emails: [] })),
        );
      }
      if (msAcctData.accounts.length > 0) {
        inboxPromises.push(
          api.get<{ emails: EmailSummary[] }>("/api/microsoft/inbox/all?max_results=20").catch(() => ({ emails: [] })),
        );
      }

      const inboxResults = await Promise.all(inboxPromises);
      const allEmails = inboxResults.flatMap((r) => r.emails);

      // Sort by date descending
      allEmails.sort((a, b) => {
        try {
          return new Date(b.date).getTime() - new Date(a.date).getTime();
        } catch {
          return 0;
        }
      });

      setMailEmails(allEmails);
      setMailNotConnected(false);
    } catch {
      if (!silent) setMailNotConnected(true);
    } finally {
      if (!silent) setMailLoading(false);
      setMailReady(true);
    }
  }, []);

  // ── Calendar fetch ────────────────────────────────────────────

  const fetchCalendar = useCallback(async (silent = false) => {
    if (!silent) setCalendarLoading(true);
    try {
      const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
      const data = await api.get<{ days: AgendaDay[] }>(
        `/api/calendar/agenda/all?days=7&timezone=${encodeURIComponent(tz)}`,
      );
      setCalendarDays(data.days);
      setCalendarNotConnected(false);
    } catch {
      if (!silent) setCalendarNotConnected(true);
    } finally {
      if (!silent) setCalendarLoading(false);
      setCalendarReady(true);
    }
  }, []);

  // Fire both on mount + auto-refresh in background
  useEffect(() => {
    fetchMail();
    fetchCalendar();

    // Email: silent refresh every 90 seconds
    const mailInterval = setInterval(() => {
      fetchMail(true);
    }, 90_000);

    // Calendar: silent refresh every 5 minutes
    const calInterval = setInterval(() => {
      fetchCalendar(true);
    }, 5 * 60_000);

    return () => {
      clearInterval(mailInterval);
      clearInterval(calInterval);
    };
  }, [fetchMail, fetchCalendar]);

  return (
    <PrefetchContext.Provider
      value={{
        mailAccounts,
        mailEmails,
        mailLoading,
        mailNotConnected,
        mailReady,
        refreshMail: fetchMail,
        setMailEmails,

        calendarDays,
        calendarLoading,
        calendarNotConnected,
        calendarReady,
        refreshCalendar: fetchCalendar,
      }}
    >
      {children}
    </PrefetchContext.Provider>
  );
}

export function usePrefetch(): PrefetchData {
  const ctx = useContext(PrefetchContext);
  if (!ctx) {
    throw new Error("usePrefetch must be used within a PrefetchProvider");
  }
  return ctx;
}
