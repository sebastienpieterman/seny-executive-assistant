import { useState, useEffect } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export type Category = "people" | "projects" | "ideas";

interface FieldDef {
  key: string;
  label: string;
  type: "text" | "textarea";
  placeholder?: string;
  required?: boolean;
}

function getFields(category: Category): FieldDef[] {
  switch (category) {
    case "people":
      return [
        { key: "name", label: "Name", type: "text", placeholder: "e.g., Sarah Johnson", required: true },
        { key: "relationship_type", label: "Relationship Type", type: "text", placeholder: "e.g., family, friend, colleague" },
        { key: "context", label: "Context", type: "text", placeholder: "How you know them, their role..." },
        { key: "notes", label: "Notes", type: "textarea", placeholder: "Additional details..." },
      ];
    case "projects":
      return [
        { key: "name", label: "Name", type: "text", placeholder: "Project name", required: true },
        { key: "status", label: "Status", type: "text", placeholder: "e.g., active, planning, on-hold" },
        { key: "next_action", label: "Next Action", type: "text", placeholder: "What's the next step?" },
        { key: "notes", label: "Notes", type: "textarea", placeholder: "Project details..." },
      ];
    case "ideas":
      return [
        { key: "name", label: "Title", type: "text", placeholder: "Idea title", required: true },
        { key: "summary", label: "Summary", type: "text", placeholder: "One-line summary" },
        { key: "notes", label: "Notes", type: "textarea", placeholder: "Elaborate on the idea..." },
        { key: "tags", label: "Tags", type: "text", placeholder: "e.g., business, tech, personal" },
      ];
    default:
      return [];
  }
}

interface AddItemDialogProps {
  open: boolean;
  initialCategory?: Category;
  onClose: () => void;
  onSubmit: (category: Category, data: Record<string, string>) => Promise<void>;
}

export function AddItemDialog({ open, initialCategory, onClose, onSubmit }: AddItemDialogProps) {
  const [category, setCategory] = useState<Category>(initialCategory || "people");
  const [formData, setFormData] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);

  // Reset form when dialog opens
  useEffect(() => {
    if (open) {
      setCategory(initialCategory || "people");
      setFormData({});
    }
  }, [open, initialCategory]);

  // Reset form data when category changes
  useEffect(() => {
    setFormData({});
  }, [category]);

  const fields = getFields(category);

  const handleSubmit = async () => {
    const nameField = fields.find(f => f.required);
    if (nameField && !formData[nameField.key]?.trim()) {
      return; // Don't submit without required field
    }

    setSubmitting(true);
    try {
      await onSubmit(category, formData);
      onClose();
    } finally {
      setSubmitting(false);
    }
  };

  const isValid = fields.some(f => f.required && formData[f.key]?.trim());

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="bg-[#1a1a1a] border-border sm:max-w-[500px]">
        <DialogHeader>
          <DialogTitle>Add New Entry</DialogTitle>
          <DialogDescription>
            Manually add a new entry to your Second Brain.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-4">
          {/* Category selector */}
          <div>
            <Label className="mb-1.5 block text-xs text-muted-foreground">Category</Label>
            <Select value={category} onValueChange={(v) => setCategory(v as Category)}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent className="bg-[#1a1a1a] border-border">
                <SelectItem value="people">People</SelectItem>
                <SelectItem value="projects">Projects</SelectItem>
                <SelectItem value="ideas">Ideas</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {/* Dynamic fields based on category */}
          {fields.map((f) => (
            <div key={f.key}>
              <Label className="mb-1.5 block text-xs text-muted-foreground">
                {f.label}{f.required && <span className="text-red-400 ml-1">*</span>}
              </Label>
              {f.type === "textarea" ? (
                <Textarea
                  value={formData[f.key] || ""}
                  onChange={(e) => setFormData((d) => ({ ...d, [f.key]: e.target.value }))}
                  placeholder={f.placeholder}
                  rows={3}
                />
              ) : (
                <Input
                  value={formData[f.key] || ""}
                  onChange={(e) => setFormData((d) => ({ ...d, [f.key]: e.target.value }))}
                  placeholder={f.placeholder}
                />
              )}
            </div>
          ))}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={handleSubmit} disabled={!isValid || submitting}>
            {submitting ? "Adding..." : "Add Entry"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
