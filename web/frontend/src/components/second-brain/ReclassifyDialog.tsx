import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useState } from "react";

const CATEGORIES = [
  { value: "people", label: "People" },
  { value: "projects", label: "Projects" },
  { value: "ideas", label: "Ideas" },
  { value: "admin", label: "Admin" },
];

interface ReclassifyDialogProps {
  open: boolean;
  currentCategory: string;
  onConfirm: (targetCategory: string) => void;
  onCancel: () => void;
}

export function ReclassifyDialog({ open, currentCategory, onConfirm, onCancel }: ReclassifyDialogProps) {
  const available = CATEGORIES.filter((c) => c.value !== currentCategory);
  const [target, setTarget] = useState(available[0]?.value || "");

  return (
    <AlertDialog open={open} onOpenChange={(o) => !o && onCancel()}>
      <AlertDialogContent className="bg-[#1a1a1a] border-border">
        <AlertDialogHeader>
          <AlertDialogTitle>Reclassify Item</AlertDialogTitle>
          <AlertDialogDescription>
            Move this item to a different category:
          </AlertDialogDescription>
        </AlertDialogHeader>
        <Select value={target} onValueChange={setTarget}>
          <SelectTrigger className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {available.map((c) => (
              <SelectItem key={c.value} value={c.value}>{c.label}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        <AlertDialogFooter>
          <AlertDialogCancel onClick={onCancel}>Cancel</AlertDialogCancel>
          <AlertDialogAction onClick={() => onConfirm(target)}>Move</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
