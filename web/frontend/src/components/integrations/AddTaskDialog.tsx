import { useState } from "react";
import { api } from "@/lib/api";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Calendar } from "@/components/ui/calendar";
import { Plus, CalendarIcon } from "lucide-react";
import { cn } from "@/lib/utils";

interface AddTaskDialogProps {
  onTaskCreated: () => void;
}

export function AddTaskDialog({ onTaskCreated }: AddTaskDialogProps) {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [dueDate, setDueDate] = useState<Date | undefined>(undefined);
  const [dueTime, setDueTime] = useState("");
  const [priority, setPriority] = useState("medium");
  const [notes, setNotes] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [calendarOpen, setCalendarOpen] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!title.trim()) return;

    setSubmitting(true);
    try {
      // Build ISO date string from date + optional time
      let dueDateStr: string | undefined;
      if (dueDate) {
        const d = new Date(dueDate);
        if (dueTime) {
          const [h, m] = dueTime.split(":").map(Number);
          d.setHours(h, m, 0, 0);
        }
        dueDateStr = d.toISOString();
      }
      await api.post("/api/tasks", {
        title: title.trim(),
        due_date: dueDateStr,
        priority,
        description: notes.trim() || undefined,
      });
      // Reset and close
      setTitle("");
      setDueDate(undefined);
      setDueTime("");
      setPriority("medium");
      setNotes("");
      setOpen(false);
      onTaskCreated();
    } catch (err) {
      console.error("Failed to create task:", err);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="ghost" size="icon" className="h-8 w-8">
          <Plus className="h-4 w-4" />
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-[425px]">
        <DialogHeader>
          <DialogTitle>Add Task</DialogTitle>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4 mt-2">
          <div className="space-y-2">
            <Label htmlFor="task-title">Title</Label>
            <Input
              id="task-title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="What needs to be done?"
              autoFocus
            />
          </div>

          <div className="space-y-2">
            <Label>Due Date</Label>
            <div className="flex gap-2">
              <Popover open={calendarOpen} onOpenChange={setCalendarOpen}>
                <PopoverTrigger asChild>
                  <Button
                    variant="outline"
                    className={cn(
                      "flex-1 justify-start text-left font-normal",
                      !dueDate && "text-muted-foreground"
                    )}
                  >
                    <CalendarIcon className="mr-2 h-4 w-4" />
                    {dueDate
                      ? dueDate.toLocaleDateString("en-US", {
                          month: "short",
                          day: "numeric",
                          year: "numeric",
                        })
                      : "Pick a date"}
                  </Button>
                </PopoverTrigger>
                <PopoverContent className="w-auto p-0" align="start">
                  <Calendar
                    mode="single"
                    selected={dueDate}
                    onSelect={(day) => {
                      setDueDate(day);
                      setCalendarOpen(false);
                    }}
                    initialFocus
                  />
                </PopoverContent>
              </Popover>
              <div className="flex items-center gap-1">
                <Select
                  value={dueTime ? dueTime.split(":")[0] : ""}
                  onValueChange={(h) => {
                    const m = dueTime ? dueTime.split(":")[1] : "00";
                    setDueTime(`${h}:${m}`);
                  }}
                >
                  <SelectTrigger className="w-[70px]">
                    <SelectValue placeholder="Hr" />
                  </SelectTrigger>
                  <SelectContent className="max-h-[200px]">
                    {Array.from({ length: 24 }, (_, i) => {
                      const v = i.toString().padStart(2, "0");
                      const label = i === 0 ? "12 AM" : i < 12 ? `${i} AM` : i === 12 ? "12 PM" : `${i - 12} PM`;
                      return <SelectItem key={v} value={v}>{label}</SelectItem>;
                    })}
                  </SelectContent>
                </Select>
                <span className="text-muted-foreground">:</span>
                <Select
                  value={dueTime ? dueTime.split(":")[1] : ""}
                  onValueChange={(m) => {
                    const h = dueTime ? dueTime.split(":")[0] : "09";
                    setDueTime(`${h}:${m}`);
                  }}
                >
                  <SelectTrigger className="w-[70px]">
                    <SelectValue placeholder="Min" />
                  </SelectTrigger>
                  <SelectContent className="max-h-[200px]">
                    {["00", "05", "10", "15", "20", "25", "30", "35", "40", "45", "50", "55"].map((m) => (
                      <SelectItem key={m} value={m}>{m}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>
            {dueDate && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-6 px-2 text-xs text-muted-foreground"
                onClick={() => {
                  setDueDate(undefined);
                  setDueTime("");
                }}
              >
                Clear date
              </Button>
            )}
          </div>

          <div className="space-y-2">
            <Label>Priority</Label>
            <Select value={priority} onValueChange={setPriority}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="urgent">Urgent</SelectItem>
                <SelectItem value="high">High</SelectItem>
                <SelectItem value="medium">Medium</SelectItem>
                <SelectItem value="low">Low</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2">
            <Label htmlFor="task-notes">Notes (optional)</Label>
            <Textarea
              id="task-notes"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder="Additional details..."
              rows={3}
            />
          </div>

          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={!title.trim() || submitting}>
              {submitting ? "Creating..." : "Create Task"}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
