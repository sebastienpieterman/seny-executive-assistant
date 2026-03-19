import { BrowserRouter, Routes, Route } from "react-router-dom";
import { Toaster } from "sonner";
import { AuthProvider } from "@/contexts/AuthContext";
import { ProtectedRoute } from "@/components/ProtectedRoute";
import { AppLayout } from "@/components/layout/AppLayout";
import { LoginPage } from "@/pages/LoginPage";
import { RegisterPage } from "@/pages/RegisterPage";
import { SetupPage } from "@/pages/SetupPage";
import { HomePage } from "@/pages/HomePage";
import { MailPage } from "@/pages/MailPage";
import { CalendarPage } from "@/pages/CalendarPage";
import { TasksPage } from "@/pages/TasksPage";
import { NotesPage } from "@/pages/NotesPage";
import { SecondBrainPage } from "@/pages/SecondBrainPage";
import { SettingsPage } from "@/pages/SettingsPage";
import { DigestPage } from "@/pages/DigestPage";
import { ActionsPage } from "@/pages/ActionsPage";
import LCDPage from "@/pages/LCDPage";
import SystemHealthPage from "@/pages/SystemHealthPage";


function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/register" element={<RegisterPage />} />
          <Route path="/setup" element={<SetupPage />} />
          <Route
            element={
              <ProtectedRoute>
                <AppLayout />
              </ProtectedRoute>
            }
          >
            <Route index element={<HomePage />} />
            <Route path="mail" element={<MailPage />} />
            <Route path="calendar" element={<CalendarPage />} />
            <Route path="tasks" element={<TasksPage />} />
            <Route path="notes" element={<NotesPage />} />
            <Route path="second-brain" element={<SecondBrainPage />} />
            <Route path="digest" element={<DigestPage />} />
            <Route path="actions" element={<ActionsPage />} />
            <Route path="lcd" element={<LCDPage />} />
            <Route path="monitoring" element={<SystemHealthPage />} />
            <Route path="settings" element={<SettingsPage />} />

          </Route>
        </Routes>
        <Toaster
          theme="dark"
          position="bottom-right"
          toastOptions={{
            style: {
              background: "#1a1a1a",
              border: "1px solid #2a2a2a",
              color: "#e5e5e5",
            },
          }}
        />
      </AuthProvider>
    </BrowserRouter>
  );
}

export default App;
